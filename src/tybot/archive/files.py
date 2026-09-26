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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from ..pii_screen import ScreenMetadata
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

# **변환 단계에서는 자르지 않는다**(2026-09-14 오너 결정).
# 자세한 이유는 `convert.MAX_LINES` 주석 참조 — 여기서 자르면 아카이브에 영구히
# 없어진다. 문서 변환과 **같은 규칙·같은 기본값**을 쓴다. 두 경로가 갈리면
# 「csv 는 되는데 xlsx 는 안 된다」 같은 설명할 수 없는 차이가 생긴다.
def _text_limit(name: str) -> int:
    """상한 환경변수. **`0` 은 무제한**(`convert._limit` 와 같은 규칙)."""
    from .convert import _limit

    return _limit(name)


MAX_TEXT_BYTES = _text_limit("TYBOT_TEXT_MAX_BYTES")
# 텍스트 파일에서 원문에 넣는 줄 수.
#
# 200 이던 것을 올렸다(2026-09-07). `csv`·`tsv` 가 여기로 오는데, 표는 **뒤에 합계가
# 있어서** 앞 200줄만 남으면 정작 필요한 값이 빠진다. 문서 변환(`convert.MAX_LINES`)과
# 같은 이유·같은 값으로 맞춘다 — 두 경로가 갈리면 「csv 는 되는데 xlsx 는 안 된다」
# 같은 설명할 수 없는 차이가 생긴다.
MAX_TEXT_LINES = _text_limit("TYBOT_TEXT_MAX_LINES")
# 접을 때 남길 머리와 꼬리. 꼬리가 합계다. 상한을 걸었을 때만 쓴다.
TEXT_FOLD_HEAD = _text_limit("TYBOT_TEXT_FOLD_HEAD")
TEXT_FOLD_TAIL = _text_limit("TYBOT_TEXT_FOLD_TAIL")
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
    # Slack 이 말하는 **파일이 올라온 시각**(epoch 초). 0 이면 모른다.
    # 수집 시각으로 대신하지 않는다 — 몇 년 전 문서가 「오늘 올라온 것」 이 된다.
    created: int = 0

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
            created=_epoch(f.get("created") or f.get("timestamp")),
        )

    @property
    def is_text(self) -> bool:
        return self.filetype in TEXT_EXTS or self.mimetype in TEXT_MIMES

    @property
    def is_convertible(self) -> bool:
        return can_convert(self.filetype)

    def describe(self, state: str | None = None, *, file_id: str = "") -> str:
        """원문에 남기는 한 줄 설명. 본문을 못 넣는 경우에도 흔적은 남는다.

        `file_id` 를 주면 뒤에 붙인다. **이 줄과 첨부 정본을 잇는 유일한 좌표다** —
        없으면 이름으로 맞춰야 하고, 같은 이름이 두 번 올라온 채널에서는 그 맞춤이
        틀린다(`attachment_trace` 가 같은 문제를 겪었다).

        옛 줄에는 없다. 그래서 새 reader 는 ID 가 있으면 ID 로, 없으면 이름으로
        맞춘다 — 그 다리는 옛 줄이 사라지면 저절로 걷힌다.
        """
        kb = max(1, self.size // 1024)
        if state is None:
            state = "본문 수집" if self.is_text else ("변환" if self.is_convertible else "미변환")
        text = f"[첨부:{state}] {self.name} ({self.filetype or self.mimetype or '?'}, {kb}KB)"
        if self.permalink:
            text += f" · <{self.permalink}|원본 파일>"
        if file_id:
            text += f" · id:{file_id}"
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


def _epoch(value) -> int:
    """Slack 이 준 epoch 초. 읽지 못하면 **0 — 모른다는 뜻**이다."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


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
    # 원문에 남길 **참조 줄**. 「이 파일이 여기 있었다」 만 말한다.
    #
    # 첨부 본문을 raw 에 복제하지 않기로 했다(분리 결정 §3). 그래도 참조는 남아야
    # 한다 — 없으면 사람이 원문을 읽을 때 파일이 있었다는 사실 자체가 사라지고,
    # 첨부 정본과 그 메시지를 잇는 좌표도 끊긴다.
    reference_lines: list[str] = field(default_factory=list)
    # 변환 본문 줄. **정본 문서로 가고 raw 에는 안 들어간다**(분리 뒤).
    #
    # `lines` 는 당분간 둘을 이어 붙인 값이다. 한 번에 바꾸면 옛 호출부가 조용히
    # 본문을 잃는데, 그 손실은 오류가 아니라 「첨부가 검색에 안 잡힘」 으로 나타난다.
    body_lines: list[str] = field(default_factory=list)
    # 파일이 올라온 시각(epoch 초). 0 이면 모른다 — 호출부가 수집 시각으로
    # 채우지 않고 **모른다는 사실을 그대로** 다뤄야 한다.
    created: int = 0


# 파일 줄을 원문에 적을 때 쓰는 시각과 화자.
#
# 예전에는 **수집 시각**을 그대로 썼다. `writer.ingest()` 가 그 값으로 저장 경로
# (`raw/<날짜>.md`)를 정하므로, 몇 년 전 문서가 「오늘 올라온 원문」 이 되어 그날
# 요약에 들어갔다(2026-09-17 실측). 기간 요약은 시각으로 묶으므로 이게 틀리면
# 기간 자체가 무의미해진다.
#
# 시각을 모르면 **모른다고 적는다.** 아는 척한 시각보다 낫다.
FILE_SPEAKER = "채널 파일"
FILE_SPEAKER_UNKNOWN = "채널 파일(올린 시각 미상)"


def staged_line_time(item: StagedAttachmentResult, *, fallback: datetime):
    """`(그 줄에 적을 시각, 화자)`.

    Slack 이 파일 생성 시각을 준 경우에만 그 값을 쓴다. 못 주면 `fallback` 을 쓰되
    화자에 그 사실을 적어 사람이 오늘 올라온 것으로 읽지 않게 한다.
    """
    if item.created:
        return datetime.fromtimestamp(item.created, tz=UTC), FILE_SPEAKER
    return fallback, FILE_SPEAKER_UNKNOWN


@dataclass(frozen=True)
class AttachmentStorage:
    """첨부 원본을 검색 가능한 원문 아카이브 밖에 격리하는 위치."""

    staging_dir: Path
    objects_dir: Path
    # 재처리 큐가 읽을 좌표. 경로에서 되짚을 수도 있지만, `_safe_component()` 를
    # 거친 뒤라 원래 값과 다를 수 있다 — 되짚은 값으로 큐 키를 만들면 같은 파일이
    # 두 작업이 된다. 기본값을 둔 이유는 기존 호출부 호환뿐이다.
    workspace: str = ""
    channel_id: str = ""


def _converted_state(coverage) -> str:
    """변환은 됐는데 **다 읽었는가**.

    `coverage` 가 없거나(구형 경로) 개수를 모르면 예전처럼 `succeeded` 다 —
    모르는 것을 `partial` 로 단정하면 멀쩡한 문서가 전부 미확인으로 보인다.
    """
    if coverage is None:
        return "succeeded"
    from .convert import PARTIAL

    return "partial" if coverage.state == PARTIAL else "succeeded"


def queue_retry(
    storage: AttachmentStorage,
    *,
    file_id: str,
    original_sha256: str,
    error_code: str,
    retryable: bool,
) -> None:
    """다시 해 볼 만한 실패를 재처리 큐에 올린다.

    **수집을 막지 않는다.** 큐가 없거나 DB 가 죽어도 첨부 수집 자체는 끝나야
    한다 — 재처리는 나중에 할 수 있지만 놓친 원본은 되돌릴 수 없다(Slack
    백필은 분당 1요청 제한이라 사실상 복구가 안 된다).

    좌표를 모르면 올리지 않는다. 모르는 채로 올리면 워커가 열 파일을 찾지 못하고,
    그 작업은 네 번 실패한 뒤 사람에게 넘어간다 — 잡음만 늘린다.
    """
    if not retryable or not storage.workspace or not storage.channel_id:
        return
    try:
        from ..conversion_queue import enqueue

        enqueue(
            workspace=storage.workspace,
            channel_id=storage.channel_id,
            file_id=file_id,
            original_sha256=original_sha256,
            error_code=error_code,
            retryable=retryable,
        )
    except Exception as exc:  # noqa: BLE001 - 큐 장애가 수집을 막으면 안 된다
        logger.warning("재처리 큐에 올리지 못했다 file=%s: %s", file_id, exc)


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
        workspace=workspace,
        channel_id=channel_id,
    )


def staged_dir(storage: AttachmentStorage, file_id: str) -> Path:
    """그 파일의 staging 디렉터리. **중복 방지 키가 곧 이 경로다.**

    `stage_attachments()` 가 쓰는 규칙과 **같은 함수**를 지나야 한다. 파일 목록
    경로(B-46)가 자기 나름의 키를 만들면 같은 파일이 두 벌 들어오는데, 그때
    원문에는 같은 내용이 두 번 적히고 우리는 그것을 두 근거로 센다.
    """
    return storage.staging_dir / _safe_component(file_id)


def already_staged(storage: AttachmentStorage, file_id: str) -> bool:
    """이미 한 번 처리한 파일인가.

    **성공했는가가 아니다.** 실패했더라도 기록은 있고, 재시도는 큐의 몫이다
    (`conversion_queue`). 여기서 「실패했으니 다시 받자」 로 판단하면 같은 파일을
    매 수집마다 다시 내려받는다.
    """
    return (staged_dir(storage, file_id) / "metadata.json").exists()


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
    if not MAX_TEXT_LINES or len(lines) <= MAX_TEXT_LINES:
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
    truncated = bool(MAX_TEXT_BYTES) and len(raw) > MAX_TEXT_BYTES
    body = raw[:MAX_TEXT_BYTES] if MAX_TEXT_BYTES else raw
    text = body.decode("utf-8", errors="replace")
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
        # 상한이 없으면 끝까지 읽는다. **0 을 그대로 넘기지 않는다** —
        # 0 은 무제한이라는 뜻이지 `read(1)` 이 아니다.
        raw = resp.read(MAX_TEXT_BYTES + 1) if MAX_TEXT_BYTES else resp.read()
    # files:read 누락 시 Slack 은 로그인 페이지를 200 으로 돌려준다.
    if "text/html" in ctype and not f.is_text:
        raise DownloadError(f"{f.name}: HTML 응답 - files:read 스코프 또는 토큰 확인")
    if raw[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise DownloadError(f"{f.name}: 로그인 페이지가 내려왔다 - files:read 스코프 확인")
    truncated = bool(MAX_TEXT_BYTES) and len(raw) > MAX_TEXT_BYTES
    body = raw[:MAX_TEXT_BYTES] if MAX_TEXT_BYTES else raw
    text = body.decode("utf-8", errors="replace")
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
        # **중복 방지 키는 한 함수에서만 나온다.** 파일 목록 경로(B-46)가 같은
        # `staged_dir()` 로 「이미 봤나」 를 판단한다.
        staged = staged_dir(storage, file_id)
        objects = storage.objects_dir / file_id
        state = "unsupported"
        error: str | None = None
        extracted: str | None = None
        object_path: Path | None = None
        digest: str | None = None
        original_retained = False
        error_code = ""
        retryable = False
        coverage = None

        try:
            if not bot_token:
                raise DownloadError(f"{f.name}: 토큰이 없어 원본을 가져오지 못했습니다")
            raw = download_bytes(f, bot_token)

            digest = hashlib.sha256(raw).hexdigest()
            objects.mkdir(parents=True, exist_ok=True)
            object_path = objects / _safe_component(f.name)
            object_path.write_bytes(raw)
            original_retained = True

            if f.is_text:
                extracted = _decode_text(raw, f.size)
            elif f.is_convertible:
                # `convert` 를 **모듈 수준 이름 그대로** 부른다. 다른 함수로
                # 바꾸면 이 이름을 갈아 끼우던 테스트가 조용히 무력해진다 —
                # 실제로 그랬다(`test_image_is_ocr_converted_...`).
                from .convert import collect_coverage

                with collect_coverage() as coverage:
                    extracted = "\n".join(convert(f.filetype, raw))
            if extracted is not None:
                state = "converted"
        except (DownloadError, ConvertError, OSError) as exc:
            state = "download_or_extract_failed"
            error = str(exc)
            from .external_convert import failure_details

            error_code, retryable = failure_details(
                exc, default="conversion_failed" if original_retained else "download_or_store_failed",
            )
            own_warnings.append(error)
            logger.warning(
                "첨부 처리 실패 file=%s original=%s: %s",
                f.name, "retained" if original_retained else "missing", exc,
            )
        except Exception as exc:
            state = "download_or_extract_failed"
            error = f"{f.name}: 예상하지 못한 오류 {exc.__class__.__name__}: {exc}"
            own_warnings.append(error)
            logger.exception("첨부 격리 저장 중 예외 %s", f.name)

        screen_result = None
        if extracted is not None:
            # **파일명과 추출문 전체를 함께** 본다. 한 줄만 보면 "등기부등본 제출
            # 일정" 이라고 적힌 일정표가 등기부등본 원문과 구별되지 않는다 —
            # 그래서 비민감 일정표가 통째로 막혔다(설계 §2.1).
            #
            # 막을 때는 여전히 **첨부 단위**다. 한 줄만 빼고 나머지를 넣으면 금지
            # 문서가 부분 수집된다.
            from ..pii_screen import screen_document

            screen_result = screen_document(
                extracted,
                filename=f.name,
                coverage_state=(coverage.state if coverage is not None else ""),
            )
            for finding in screen_result.findings:
                if finding.severity == "notice":
                    own_warnings.append(f"{f.name}: {finding.label}({finding.code})")
            if screen_result.blocked:
                state = "pii_refused"
                error = f"{f.name}: 수집 제외 대상({screen_result.reason})"
                own_warnings.append(error)
                extracted = None

        try:
            from .convert import conversion_stamp

            staged.mkdir(parents=True, exist_ok=True)
            metadata = {
                "schema_version": 1,
                "status": state,
                "original_state": "retained" if original_retained else "missing",
                # **「본문이 나왔다」 와 「다 읽었다」 는 다르다.** 10쪽 중 3쪽만
                # 읽혀도 예전에는 `succeeded` 였고, 그 답에 우리 출처가 붙었다.
                "conversion_state": (
                    "blocked" if state == "pii_refused" else
                    _converted_state(coverage) if state == "converted" else
                    "failed" if original_retained and error else
                    "unsupported" if state == "unsupported" else "pending"
                ),
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
                "error_code": "pii_refused" if state == "pii_refused" else error_code,
                # 판정 근거는 **코드만** 남긴다. OCR 본문·번호 일부·사람 이름을
                # 감사 metadata 에 복제하지 않는다(설계 §2.4).
                **(ScreenMetadata.of(screen_result).to_json()
                   if screen_result is not None else {}),
                "retryable": retryable if state != "pii_refused" else False,
                "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
                # --- 변환기 신원과 변환 시각. **정본 revision 이 여기서 나온다**
                # (`attachment_doc.revision_for`). 재변환 경로도 같은 함수를 쓴다 —
                # 한쪽만 적으면 재변환본이 옛 신원으로 계산돼 같은 경로에 겹친다.
                **conversion_stamp(
                    f.filetype, coverage, produced=state == "converted"
                ),
                # 얼마나 읽었나. **모르는 값은 `null`** 이다 — 0 이나 100% 로
                # 만들면 모르는 것을 안다고 적는 셈이다.
                **(coverage.to_json() if coverage is not None else {}),
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
            queue_retry(
                storage,
                file_id=file_id,
                original_sha256=digest or "",
                error_code=str(metadata["error_code"] or ""),
                retryable=bool(metadata["retryable"]),
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
        # 참조와 본문을 **만드는 자리에서** 가른다. 뒤에서 문자열을 보고 나누면
        # 표식이 바뀔 때마다 그 파싱이 조용히 어긋난다.
        own_reference = [f.describe(label, file_id=str(f.id or file_id))]
        own_body: list[str] = []
        if extracted is not None:
            tag = "첨부본문" if f.is_text else "첨부추출"
            extracted_lines = [line.strip() for line in extracted.splitlines() if line.strip()]
            # **`0` 은 무제한이다.** `[:0]` 으로 읽으면 본문이 통째로 사라진다 —
            # 상한을 없애는 변경에서 가장 조용한 실패가 여기였다.
            truncated = bool(MAX_TEXT_LINES) and len(extracted_lines) > MAX_TEXT_LINES
            kept = extracted_lines[:MAX_TEXT_LINES] if MAX_TEXT_LINES else extracted_lines
            for line in kept:
                own_body.append(f"[{tag}:{f.name}] {line}")
            if truncated:
                own_body.append(f"[{tag}:{f.name}] …(이하 생략, 원본 링크에서 확인)")
        own_lines = [*own_reference, *own_body]

        from ..attachment_trace import line_hash

        results.append(StagedAttachmentResult(
            file_id=str(f.id or file_id),
            name=f.name,
            lines=own_lines,
            reference_lines=own_reference,
            body_lines=own_body,
            line_hashes=[line_hash(ln) for ln in own_lines],
            metadata_path=staged / "metadata.json",
            warnings=list(own_warnings),
            extracted=extracted is not None,
            created=f.created,
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
