"""첨부 자동 변환 결과와 과거 검수 메타데이터를 읽는다.

첨부는 수집 시 로컬에서 자동 변환한다. 답변 엔진은 원본 바이트를 외부 모델에 보내지
않으며, 이 모듈의 과거 승인 상태는 기존 메타데이터와 운영 도구의 호환을 위해 유지한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tybot.attachment_review")

PENDING = "pending_review"
APPROVED = "approved"
REJECTED = "rejected"
FAILED = "failed"
CONVERTED = "converted"
UNSUPPORTED = "unsupported"
DOWNLOAD_OR_EXTRACT_FAILED = "download_or_extract_failed"
PII_REFUSED = "pii_refused"

STATES = (
    PENDING,
    APPROVED,
    REJECTED,
    FAILED,
    CONVERTED,
    UNSUPPORTED,
    DOWNLOAD_OR_EXTRACT_FAILED,
    PII_REFUSED,
)
FAILURE_STATES = frozenset({FAILED, UNSUPPORTED, DOWNLOAD_OR_EXTRACT_FAILED})


@dataclass(frozen=True)
class Attachment:
    """검수 대상 하나. 본문은 담지 않는다 — 경로만 들고 필요할 때 읽는다."""

    workspace: str
    channel_id: str
    file_id: str
    name: str
    filetype: str
    mimetype: str
    size: int
    status: str
    object_path: Path | None
    meta_path: Path
    approved_by: str = ""
    approved_at: str = ""
    note: str = ""
    permalink: str = ""
    error: str = ""
    extracted: bool = False
    # 언제 올라온 것인가. **하루치를 밀어 주려면 필요하다** — 오늘 올라온 것과
    # 밀린 것을 한 목록에 섞으면 오늘 것이 묻힌다.
    staged_at: str = ""

    @property
    def is_approved(self) -> bool:
        return self.status == APPROVED

    @property
    def conversion_failed(self) -> bool:
        return self.status in FAILURE_STATES


def staging_root(archive_dir: Path | str) -> Path:
    """`stage_files` 가 쓰는 위치와 같아야 한다. 어긋나면 조용히 0건이 된다."""
    return Path(archive_dir).parent / "staging" / "workspaces"


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("첨부 메타데이터를 읽지 못했다 %s: %s", path, e)
        return None


def _from_meta(meta: dict, meta_path: Path, workspace: str, channel_id: str) -> Attachment:
    raw_object = meta.get("object_path")
    return Attachment(
        workspace=workspace,
        channel_id=channel_id,
        file_id=meta_path.parent.name,
        name=str(meta.get("name") or ""),
        filetype=str(meta.get("filetype") or ""),
        mimetype=str(meta.get("mimetype") or ""),
        size=int(meta.get("declared_size") or 0),
        status=str(meta.get("status") or PENDING),
        object_path=Path(raw_object) if raw_object else None,
        meta_path=meta_path,
        approved_by=str(meta.get("approved_by") or ""),
        approved_at=str(meta.get("approved_at") or ""),
        note=str(meta.get("review_note") or ""),
        permalink=str(meta.get("permalink") or ""),
        error=str(meta.get("error") or ""),
        extracted=bool(meta.get("extracted")),
        staged_at=str(meta.get("staged_at") or ""),
    )


def scan(archive_dir: Path | str, *, status: str | None = None) -> list[Attachment]:
    """검수 폴더 전체를 훑는다. `status` 를 주면 그 상태만."""
    root = staging_root(archive_dir)
    if not root.is_dir():
        return []
    out: list[Attachment] = []
    for meta_path in sorted(root.glob("*/channels/*/attachments/*/metadata.json")):
        meta = _read_meta(meta_path)
        if meta is None:
            continue
        # .../workspaces/<ws>/channels/<ch>/attachments/<file>/metadata.json
        parts = meta_path.parts
        try:
            ws = parts[parts.index("workspaces") + 1]
            ch = parts[parts.index("channels") + 1]
        except (ValueError, IndexError):
            continue
        item = _from_meta(meta, meta_path, ws, ch)
        if status is None or item.status == status:
            out.append(item)
    return out


def pending(archive_dir: Path | str) -> list[Attachment]:
    return scan(archive_dir, status=PENDING)


def failures(archive_dir: Path | str) -> list[Attachment]:
    """다운로드 또는 변환에 실패한 첨부만 반환한다."""
    return [item for item in scan(archive_dir) if item.conversion_failed]


def extracted_preview(item: Attachment, *, limit: int = 700) -> str:
    """PII 검사를 통과해 저장된 로컬 변환본의 짧은 검토용 미리보기."""
    if not item.extracted or limit < 1:
        return ""
    path = item.meta_path.parent / "extracted.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning(
            "첨부 변환본을 읽지 못했다 ws=%s ch=%s file=%s: %s",
            item.workspace,
            item.channel_id,
            item.file_id,
            exc,
        )
        return ""
    body = [line.strip() for line in lines if line.strip() and not line.startswith("<!--")]
    if body and body[0].startswith("# "):
        body = body[1:]
    text = "\n".join(body).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def public_failure_reason(item: Attachment) -> str:
    """콘솔과 Slack에 노출해도 되는 조치 중심 실패 사유."""
    error = item.error.lower()
    if item.status == PII_REFUSED:
        for label in ("등기부등본", "계약자 명단", "주민등록번호 형식", "주민번호 언급"):
            if label.lower() in error:
                return f"민감정보 검사 차단: OCR 결과에서 '{label}' 표현을 감지했습니다. 원본 확인이 필요합니다."
        return "민감정보 검사에서 차단됐습니다. 원본 확인이 필요합니다."
    if "files:read" in error or "로그인 페이지" in error or "토큰" in error:
        return "Slack 파일 다운로드 권한 또는 봇 토큰을 확인하세요."
    if "제한 초과" in item.error:
        return "파일 크기 제한을 초과했습니다."
    if "미설치" in item.error:
        return "서버에 필요한 문서 변환기가 설치되지 않았습니다."
    if "암호가 걸린 pdf" in error:
        return "암호화된 PDF는 자동 변환할 수 없습니다."
    if "텍스트 레이어 없음" in item.error:
        return "PDF에 텍스트가 없고 OCR 변환기를 사용할 수 없습니다."
    if "ocr 실패" in error:
        return "스캔 문서 OCR 처리에 실패했습니다."
    if "손상" in item.error or "구형 hwp" in error:
        return "문서가 손상되었거나 지원하지 않는 구형 HWP 형식입니다."
    if "텍스트를 찾지 못" in item.error:
        return "문서에서 변환할 텍스트를 찾지 못했습니다."
    if "빈 파일" in item.error:
        return "빈 파일입니다."
    if "지원하지" in item.error:
        return "현재 지원하지 않는 문서 형식입니다."
    if item.status == FAILED:
        return "문서 처리에 실패했습니다."
    return "문서 다운로드 또는 변환에 실패했습니다. 서비스 로그를 확인하세요."


# 사람에게 보일 상태 이름. **현재 메타데이터가 유일한 기준이다**(설계 §11) —
# 과거 대화나 아카이브에 「처리실패」라고 적혀 있어도 지금 변환됐으면 변환된 것이다.
STATUS_LABELS = {
    CONVERTED: "변환 완료",
    APPROVED: "변환 완료",
    PENDING: "변환 대기",
    REJECTED: "검수 반려",
    FAILED: "변환 실패",
    UNSUPPORTED: "지원하지 않는 형식",
    DOWNLOAD_OR_EXTRACT_FAILED: "다운로드 또는 추출 실패",
    PII_REFUSED: "민감정보 검사 차단",
}
NO_METADATA = "현재 상태를 확인하지 못함"


def status_label(item: Attachment) -> str:
    """이 첨부의 **지금** 상태 한 낱말."""
    if item.status in FAILURE_STATES or item.status == PII_REFUSED:
        return STATUS_LABELS.get(item.status, "변환 실패")
    if item.extracted:
        # 추출 텍스트가 아카이브에 들어갔다는 것이 곧 변환에 성공했다는 뜻이다.
        return STATUS_LABELS[CONVERTED]
    if item.object_path is None:
        return "원본 없음"
    return STATUS_LABELS.get(item.status, item.status or NO_METADATA)


def status_line(item: Attachment) -> str:
    """Slack 한 줄. 실패는 **조치 중심 사유**만 붙인다 — 추출 본문은 싣지 않는다."""
    label = status_label(item)
    name = item.name or item.file_id
    if item.status in FAILURE_STATES or item.status == PII_REFUSED:
        return f"{name} — {label}: {public_failure_reason(item)}"
    return f"{name} — {label}"


def _write_status(item: Attachment, status: str, *, actor: str, note: str) -> Attachment:
    meta = _read_meta(item.meta_path) or {}
    meta["status"] = status
    meta["approved_by"] = actor
    meta["approved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    meta["review_note"] = note[:500]
    tmp = item.meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(item.meta_path)
    # 파일명·본문은 남기지 않는다. 누가 무엇을 어떤 상태로 바꿨는지만.
    logger.info(
        "첨부 검수 ws=%s ch=%s file=%s -> %s actor=%s",
        item.workspace, item.channel_id, item.file_id, status, actor,
    )
    return _from_meta(_read_meta(item.meta_path) or meta, item.meta_path,
                      item.workspace, item.channel_id)


def approve(item: Attachment, *, actor: str, note: str = "") -> Attachment:
    """과거 검수 상태를 승인으로 기록한다. 답변의 원본 전송을 허용하지는 않는다."""
    if not actor.strip():
        raise ValueError("승인자를 남기지 않은 승인은 받지 않는다")
    return _write_status(item, APPROVED, actor=actor, note=note)


def reject(item: Attachment, *, actor: str, note: str = "") -> Attachment:
    """승인을 막거나 되돌린다. 원본 바이트는 지우지 않는다 — 재검토 근거가 사라진다."""
    return _write_status(item, REJECTED, actor=actor, note=note)


def find_sendable(
    archive_dir: Path | str,
    *,
    workspace: str,
    channel_id: str,
    name: str,
    text_extracted: bool,
) -> Attachment | None:
    """과거 원본 전송 정책의 호환 판정 함수.

    답변 엔진은 권한 필터가 끝난 검색 결과 중 OCR·PII 검사를 통과한 이미지만
    이 함수로 다시 확인한다. 동일 이름이 여러 개거나 반려된 원본은 보내지 않는다.

    ## 게이트를 좁힌 이유 (2026-09-08)

    처음에는 **모든** 첨부가 사람 승인을 기다렸다. 그래서 대기 31건·승인 0건이
    되었고, 사용자는 승인이 필요한지조차 몰랐다. 게이트가 아니라 정체였다.

    막으려던 것은 하나다 — **텍스트가 없어 PII 검사가 돌지 않는 파일.**
    수집 단계 PII 검사(`writer.PII_PATTERNS`)는 주민번호·등기부등본·계약자 명단을
    글자로 찾는다. 스캔본·이미지에는 글자가 없어 그 검사가 아예 작동하지 않는다.

    변환된 파일은 다르다. 추출 텍스트가 아카이브에 들어갔다는 것이 곧 **그 검사를
    통과했다는 뜻**이다. 그런 파일까지 승인을 기다리게 하면, 정작 사람이 봐야 할
    스캔본이 목록에 묻힌다.

    **반려는 변환 여부와 무관하게 이긴다.** 사람이 한 번 안 된다고 한 것을 자동
    판정이 되돌리면 그 판단이 의미를 잃는다.
    """
    target = (name or "").strip()
    if not target:
        return None
    matches = [
        a for a in scan(archive_dir)
        if a.workspace == workspace and a.channel_id == channel_id and a.name == target
    ]
    # 같은 이름이 여럿이면 **찾지 못한 것으로 본다** — 어느 것인지 모르는 채로
    # 원본을 벤더에 보내지 않는다.
    if len(matches) != 1:
        return None
    item = matches[0]
    if item.status == REJECTED:
        return None
    if item.status == APPROVED:
        return item
    return item if text_extracted else None


def find_approved(
    archive_dir: Path | str, *, workspace: str, channel_id: str, name: str
) -> Attachment | None:
    """채널·파일명으로 승인된 원본을 찾는다.

    원문 줄에는 파일 ID 가 없고 이름만 남는다(`[첨부:검수대기] 보고서.pdf (pdf, 240KB)`).
    같은 이름이 여러 개면 **찾지 못한 것으로 본다** — 어느 것인지 모르는 채로 원본을
    벤더에 보내지 않는다.
    """
    target = (name or "").strip()
    if not target:
        return None
    matches = [
        a for a in scan(archive_dir, status=APPROVED)
        if a.workspace == workspace and a.channel_id == channel_id and a.name == target
    ]
    return matches[0] if len(matches) == 1 else None


SUMMARY_EMPTY = "검수 대기 중인 첨부가 없습니다."


def summary(items: list[Attachment]) -> str:
    """운영자용 목록. 파일명은 담당자가 봐야 하므로 남기지만 본문은 절대 열지 않는다."""
    if not items:
        return SUMMARY_EMPTY
    lines = [f"*검수 대기 {len(items)}건*"]
    for a in items[:20]:
        kb = max(1, a.size // 1024)
        lines.append(
            f"• `{a.file_id}` {a.name} ({a.filetype or a.mimetype or '?'}, {kb}KB) "
            f"— {a.workspace}/{a.channel_id}"
        )
    if len(items) > 20:
        lines.append(f"… 그 외 {len(items) - 20}건")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------
#
# 기존 메타데이터를 확인하거나 상태를 되돌려야 하는 운영 호환용 명령줄이다.
def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="첨부 처리 메타데이터 확인 및 과거 검수 상태 관리")
    ap.add_argument("action", choices=("list", "approve", "reject"))
    ap.add_argument("file_id", nargs="?", help="approve/reject 대상 (list 로 확인)")
    ap.add_argument("--actor", default=os.getenv("USER") or os.getenv("USERNAME") or "",
                    help="승인자. 누가 승인했는지 남지 않는 승인은 받지 않는다")
    ap.add_argument("--note", default="", help="판단 근거 한 줄")
    ap.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(message)s")

    if args.action == "list":
        print(summary(pending(args.archive)))
        return 0

    if not args.file_id:
        print("대상 file_id 가 필요하다. `list` 로 확인하라.")
        return 2

    items = [a for a in scan(args.archive) if a.file_id == args.file_id]
    if len(items) != 1:
        print(f"file_id `{args.file_id}` 로 {len(items)}건이 나왔다. 하나여야 한다.")
        return 1

    item = items[0]
    try:
        if args.action == "approve":
            done = approve(item, actor=args.actor, note=args.note)
        else:
            done = reject(item, actor=args.actor, note=args.note)
    except ValueError as e:
        print(f"거절: {e}")
        return 2
    print(f"{done.name} -> {done.status} (by {done.approved_by})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
