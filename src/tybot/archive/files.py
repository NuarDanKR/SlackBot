"""Slack 첨부 파일 처리.

원칙:
- 텍스트 파일(txt/md/csv/json)은 그대로 넣는다.
- **업무 문서(xlsx/docx/pptx/pdf/hwpx)는 변환해서 넣는다** — 실사용자가 올리는 건 이쪽이고,
  이걸 놓치면 맥락의 대부분이 빠진다. 변환 규칙은 `convert.py` 참조.
- 변환본은 `[첨부추출:파일명]` 으로 표시한다. 사람이 자동 변환본임을 알 수 있어야 한다.
- 스캔 PDF·구형 hwp는 서버 변환기가 있을 때 처리하고, OCR 사용 여부를 본문에 표시한다.
- 다운로드에는 `files:read` 스코프와 봇 토큰 Bearer 헤더가 **둘 다** 필요하다.
  헤더가 없으면 파일 대신 로그인 HTML 이 200 으로 내려온다(조용한 고장) - 그래서 검증한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from .convert import ConvertError, can_convert, convert

logger = logging.getLogger("tybot.files")

# 본문을 원문에 넣어도 되는 형식
TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "log", "ini", "conf"}
TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "application/json"}
# 변환도 안 되는 형식 - 목록만 남긴다. PNG/JPEG/WebP는 kordoc OCR 대상이다.
UNCONVERTED_EXTS = {
    "xls",  # 구형 Excel은 안전한 변환 경로가 아직 없다
    "gif", "bmp", "tif", "tiff",
    "dwg", "dxf",  # 도면
    "zip", "7z", "rar",  # 압축
}

MAX_TEXT_BYTES = 256 * 1024  # 원문에 넣는 텍스트 상한
# 텍스트 파일에서 원문에 넣는 줄 수.
#
# 200 이던 것을 올렸다(2026-09-07). `csv`·`tsv` 가 여기로 오는데, 표는 **뒤에 합계가
# 있어서** 앞 200줄만 남으면 정작 필요한 값이 빠진다. 문서 변환(`convert.MAX_LINES`)과
# 같은 이유·같은 값으로 맞춘다 — 두 경로가 갈리면 「csv 는 되는데 xlsx 는 안 된다」
# 같은 설명할 수 없는 차이가 생긴다.
MAX_TEXT_LINES = 20_000
# 접을 때 남길 머리와 꼬리. 꼬리가 합계다.
TEXT_FOLD_HEAD = 12_000
TEXT_FOLD_TAIL = 4_000
DOWNLOAD_TIMEOUT = 20


class DownloadError(RuntimeError):
    """파일을 받지 못했거나 받은 것이 파일이 아니다."""


@dataclass(frozen=True)
class SlackFile:
    id: str
    name: str
    filetype: str
    size: int
    url_private_download: str | None
    mimetype: str = ""
    permalink: str = ""

    @classmethod
    def from_event(cls, f: dict) -> SlackFile:
        name = str(f.get("name") or f.get("title") or f.get("id") or "unnamed")
        reported = str(f.get("filetype") or "").lower()
        suffix = Path(name).suffix.lstrip(".").lower()
        # Slack은 구형 HWP를 흔히 `binary`로 보고한다. 파일명 확장자가 우리가 실제로
        # 처리하는 형식이면 그것을 우선해야 HWP가 변환기에 도달한다.
        filetype = suffix if suffix in TEXT_EXTS or can_convert(suffix) else reported
        return cls(
            id=str(f.get("id", "")),
            name=name,
            filetype=filetype,
            size=int(f.get("size") or 0),
            url_private_download=f.get("url_private_download") or f.get("url_private"),
            mimetype=str(f.get("mimetype") or ""),
            permalink=str(f.get("permalink") or f.get("permalink_public") or ""),
        )

    @property
    def is_text(self) -> bool:
        return self.filetype in TEXT_EXTS or self.mimetype in TEXT_MIMES

    @property
    def is_convertible(self) -> bool:
        return can_convert(self.filetype)

    def describe(self, state: str | None = None) -> str:
        """원문에 남기는 한 줄 설명. 본문을 못 넣는 경우에도 흔적은 남는다."""
        kb = max(1, self.size // 1024)
        if state is None:
            state = "본문 수집" if self.is_text else ("변환" if self.is_convertible else "미변환")
        text = f"[첨부:{state}] {self.name} ({self.filetype or self.mimetype or '?'}, {kb}KB)"
        if self.permalink:
            text += f" · <{self.permalink}|원본 파일>"
        return text


@dataclass(frozen=True)
class AttachmentOrigin:
    """이 첨부가 온 자리. Slack 메시지와 답변 근거를 잇는 좌표다.

    설계: `docs/design/document-pipeline-trace-and-report-summary.md` §5

    인자를 문자열로 계속 늘리지 않는다. 실시간 수집과 정기 백필이 **같은 구조**를
    써야 하고, 하나가 빠뜨리면 그 경로로 들어온 첨부만 추적이 끊긴다.
    """

    workspace: str = ""
    channel_id: str = ""
    message_ts: str = ""
    thread_ts: str = ""


@dataclass
class StagedAttachmentResult:
    """첨부 하나의 처리 결과. **어느 줄이 어느 파일에서 왔는지** 담는다.

    `stage_files()` 가 `list[str]` 만 돌려주면 writer 이후에 그 연결을 되찾을 수
    없다. 그래서 파일 ID 와 줄 지문을 함께 돌려준다 — 원문 반영을 확인하는 유일한
    근거다(§6).

    본문은 담지 않는다. `line_hashes` 는 「그 줄이 있었다」 만 확인하는 지문이다.
    """

    file_id: str
    name: str
    lines: list[str]
    line_hashes: list[str]
    metadata_path: Path
    warnings: list[str]
    extracted: bool = False


@dataclass(frozen=True)
class AttachmentStorage:
    """첨부 원본을 검색 가능한 원문 아카이브 밖에 격리하는 위치."""

    staging_dir: Path
    objects_dir: Path


def attachment_storage(
    archive_root: Path | str, workspace: str, channel_id: str
) -> AttachmentStorage:
    """ARCHIVE_DIR의 형제인 staging/objects 아래 채널별 저장 위치를 만든다."""
    archive = Path(archive_root)
    safe_ws = _safe_component(workspace)
    safe_channel = _safe_component(channel_id)
    suffix = Path("workspaces") / safe_ws / "channels" / safe_channel / "attachments"
    return AttachmentStorage(
        staging_dir=archive.parent / "staging" / suffix,
        objects_dir=archive.parent / "objects" / suffix,
    )


def _safe_component(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("._")
    return safe or "unnamed"


def _fold_lines(lines: list[str]) -> tuple[list[str], bool]:
    """상한을 넘으면 **가운데를 접는다.** 뒤를 자르지 않는다.

    표는 머리(헤더)와 꼬리(합계)가 둘 다 필요하다. 앞에서 잘라 내면 헤더는 남고
    합계가 사라지는데, 사람이 묻는 값은 대개 합계다.

    접었으면 그 사실을 줄에 적으므로, 호출부는 「이하 생략」 을 또 붙이지 않는다
    (같은 말을 두 번 하면 어느 쪽이 진짜 상한인지 알 수 없다).
    """
    if len(lines) <= MAX_TEXT_LINES:
        return lines, False
    dropped = len(lines) - TEXT_FOLD_HEAD - TEXT_FOLD_TAIL
    return (
        [
            *lines[:TEXT_FOLD_HEAD],
            f"…(가운데 {dropped}줄 생략, 총 {len(lines)}줄)",
            *lines[-TEXT_FOLD_TAIL:],
        ],
        True,
    )


def _decode_text(raw: bytes, declared_size: int) -> str:
    truncated = len(raw) > MAX_TEXT_BYTES
    text = raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    lines, folded = _fold_lines(lines)
    truncated = truncated and not folded
    out = "\n".join(lines)
    if truncated:
        out += f"\n…(이하 생략, 원본 {max(1, declared_size // 1024)}KB)"
    return out


def download_bytes(f: SlackFile, bot_token: str, limit: int | None = None) -> bytes:
    """원본 바이트를 가져온다. 로그인 HTML 이 오면 실패로 처리한다."""
    if not f.url_private_download:
        raise DownloadError(f"{f.name}: 다운로드 URL 없음")
    req = Request(f.url_private_download, headers={"Authorization": f"Bearer {bot_token}"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        raw = resp.read() if limit is None else resp.read(limit + 1)
    if limit is not None and len(raw) > limit:
        raise DownloadError(f"{f.name}: {limit // 1024 // 1024}MB 제한 초과")
    if raw[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise DownloadError(f"{f.name}: 로그인 페이지가 내려왔다 - files:read 스코프 확인")
    return raw


def download_text(f: SlackFile, bot_token: str) -> str:
    """텍스트 파일 본문을 가져온다. 실패는 예외로 올린다(조용히 넘기지 않는다)."""
    if not f.url_private_download:
        raise DownloadError(f"{f.name}: 다운로드 URL 없음")
    req = Request(f.url_private_download, headers={"Authorization": f"Bearer {bot_token}"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read(MAX_TEXT_BYTES + 1)
    # files:read 누락 시 Slack 은 로그인 페이지를 200 으로 돌려준다.
    if "text/html" in ctype and not f.is_text:
        raise DownloadError(f"{f.name}: HTML 응답 - files:read 스코프 또는 토큰 확인")
    if raw[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise DownloadError(f"{f.name}: 로그인 페이지가 내려왔다 - files:read 스코프 확인")
    truncated = len(raw) > MAX_TEXT_BYTES
    text = raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    lines, folded = _fold_lines(lines)
    truncated = truncated and not folded
    out = "\n".join(lines)
    if truncated:
        out += f"\n…(이하 생략, 원본 {max(1, f.size // 1024)}KB)"
    return out


def stage_attachments(
    files: list[dict],
    bot_token: str | None,
    storage: AttachmentStorage,
    *,
    origin: AttachmentOrigin | None = None,
) -> list[StagedAttachmentResult]:
    """원본을 격리 저장하고 **파일별로** 원문 줄과 줄 지문을 돌려준다.

    원본은 ArchiveStore 밖에 격리한다. 변환 텍스트는 호출자가 기존 민감정보 검사를
    적용한 뒤 아카이브에 기록하며, 답변 경로는 원본 바이트를 외부 모델에 보내지 않는다.

    파일별로 나눠 돌려주는 이유는 하나다 — `writer.ingest()` 뒤에 **그 파일의 줄이
    원문에 실제로 들어갔는지** 확인해야 하기 때문이다. 줄을 한 덩어리로 합치면 그
    연결이 사라지고, 그때부터 `converted` 를 「답변 가능」 으로 오해하게 된다(§6).
    """
    results: list[StagedAttachmentResult] = []
    for item in files or []:
        f = SlackFile.from_event(item)
        # 경고도 파일별로 모은다. 한 리스트에 섞으면 어느 파일의 경고인지 사라진다.
        own_warnings: list[str] = []
        file_id = _safe_component(f.id or hashlib.sha256(f.name.encode()).hexdigest()[:16])
        staged = storage.staging_dir / file_id
        objects = storage.objects_dir / file_id
        state = "unsupported"
        error: str | None = None
        extracted: str | None = None
        object_path: Path | None = None
        digest: str | None = None

        try:
            if not bot_token:
                raise DownloadError(f"{f.name}: 토큰이 없어 원본을 가져오지 못했습니다")
            raw = download_bytes(f, bot_token)

            digest = hashlib.sha256(raw).hexdigest()
            objects.mkdir(parents=True, exist_ok=True)
            object_path = objects / _safe_component(f.name)
            object_path.write_bytes(raw)

            if f.is_text:
                extracted = _decode_text(raw, f.size)
            elif f.is_convertible:
                extracted = "\n".join(convert(f.filetype, raw))
            if extracted is not None:
                state = "converted"
        except (DownloadError, ConvertError, OSError) as exc:
            state = "download_or_extract_failed"
            error = str(exc)
            own_warnings.append(error)
            logger.warning("첨부 격리 저장 실패 %s: %s", f.name, exc)
        except Exception as exc:
            state = "download_or_extract_failed"
            error = f"{f.name}: 예상하지 못한 오류 {exc.__class__.__name__}: {exc}"
            own_warnings.append(error)
            logger.exception("첨부 격리 저장 중 예외 %s", f.name)

        if extracted is not None:
            # 한 줄만 거부하고 나머지를 넣으면 금지 문서가 부분 수집된다. 첨부 단위로 막는다.
            from .writer import screen

            refused = next((reason for line in extracted.splitlines() if (reason := screen(line))), None)
            if refused:
                state = "pii_refused"
                error = f"{f.name}: 수집 제외 대상({refused})"
                own_warnings.append(error)
                extracted = None

        try:
            staged.mkdir(parents=True, exist_ok=True)
            metadata = {
                "schema_version": 1,
                "status": state,
                "slack_file_id": f.id,
                "name": f.name,
                "filetype": f.filetype,
                "mimetype": f.mimetype,
                "permalink": f.permalink or None,
                "declared_size": f.size,
                "sha256": digest,
                "object_path": str(object_path) if object_path else None,
                "extracted": extracted is not None,
                "error": error,
                "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
                # --- 추적 좌표(§5). 없는 값은 넣지 않는다 — 구형 metadata 와
                # 구별되어야 하고, 빈 문자열은 「모른다」 를 「없다」 로 바꾼다.
                **({"origin_message_ts": origin.message_ts}
                   if origin and origin.message_ts else {}),
                **({"origin_thread_ts": origin.thread_ts}
                   if origin and origin.thread_ts else {}),
                # 원문 반영은 writer 이후에야 알 수 있다. 여기서는 아직 모른다.
                "archive_state": "pending",
                "archive_line_hashes": [],
                "archived_at": None,
                "archive_error_code": None,
            }
            (staged / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if extracted is not None:
                (staged / "extracted.md").write_text(
                    "<!-- 로컬 변환본. 아카이브 기록 시 PII 검사 적용 -->\n"
                    f"# {f.name}\n\n"
                    f"{extracted.rstrip()}\n",
                    encoding="utf-8",
                )
            else:
                (staged / "extracted.md").unlink(missing_ok=True)
        except OSError as exc:
            warning = f"{f.name}: 검수 메타데이터 저장 실패: {exc}"
            own_warnings.append(warning)
            logger.warning(warning)

        if extracted is not None:
            label = "자동변환"
        elif state == "unsupported":
            label = "미지원"
        elif state == "pii_refused":
            label = "수집제외"
        else:
            label = "처리실패"
        own_lines = [f.describe(label)]
        if extracted is not None:
            tag = "첨부본문" if f.is_text else "첨부추출"
            body_lines = [line.strip() for line in extracted.splitlines() if line.strip()]
            truncated = len(body_lines) > MAX_TEXT_LINES
            for line in body_lines[:MAX_TEXT_LINES]:
                own_lines.append(f"[{tag}:{f.name}] {line}")
            if truncated:
                own_lines.append(f"[{tag}:{f.name}] …(이하 생략, 원본 링크에서 확인)")

        from ..attachment_trace import line_hash

        results.append(StagedAttachmentResult(
            file_id=str(f.id or file_id),
            name=f.name,
            lines=own_lines,
            line_hashes=[line_hash(ln) for ln in own_lines],
            metadata_path=staged / "metadata.json",
            warnings=list(own_warnings),
            extracted=extracted is not None,
        ))
    return results


def stage_files(
    files: list[dict],
    bot_token: str | None,
    storage: AttachmentStorage,
    *,
    origin: AttachmentOrigin | None = None,
) -> tuple[list[str], list[str]]:
    """`stage_attachments` 의 평면 반환. 기존 호출부를 위해 남긴다.

    파일↔줄 연결이 필요하면 `stage_attachments` 를 쓴다 — 이 함수는 그 연결을 버린다.
    """
    results = stage_attachments(files, bot_token, storage, origin=origin)
    lines = [ln for r in results for ln in r.lines]
    warnings = [w for r in results for w in r.warnings]
    return lines, warnings


def file_lines(files: list[dict], bot_token: str | None) -> tuple[list[str], list[str]]:
    """첨부 목록 → (원문에 넣을 줄들, 경고 메시지들).

    반환되는 줄은 수집기가 그대로 원문 라인 본문으로 쓴다.
    """
    lines: list[str] = []
    warnings: list[str] = []
    for raw in files or []:
        f = SlackFile.from_event(raw)

        if not (f.is_text or f.is_convertible):
            lines.append(f.describe("미변환"))
            continue
        if not bot_token:
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: 토큰이 없어 본문을 가져오지 못했습니다")
            continue

        try:
            if f.is_text:
                body = download_text(f, bot_token).splitlines()
                tag = "첨부본문"
            else:
                data = download_bytes(f, bot_token)
                body = convert(f.filetype, data)
                tag = "첨부추출"
        except (DownloadError, ConvertError) as e:
            # 변환 못 한 것은 목록에 남긴다 - 색인의 「변환하지 못한 것」이 된다.
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: {e}")
            logger.warning("첨부 처리 실패 %s: %s", f.name, e)
            continue
        except Exception as e:
            lines.append(f.describe("미변환"))
            warnings.append(f"{f.name}: 예상치 못한 오류 {e.__class__.__name__}: {e}")
            logger.exception("첨부 처리 중 예외 %s", f.name)
            continue

        lines.append(f.describe("본문 수집" if f.is_text else "변환"))
        for ln in body:
            if ln.strip():
                lines.append(f"[{tag}:{f.name}] {ln.strip()}")
    return lines, warnings
