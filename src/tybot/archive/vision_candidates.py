"""B-80 PDF 페이지 Vision 후보 — **실패·미확인 페이지만** 고른다. 아무것도 보내지 않는다.

입력은 수집이 남긴 staged metadata 다(`attachment_review.scan`). 변환 시점에 PDF 를 쪽마다
판정해 두었다(`convert._pdf`):

- `missing_units`  : 본문을 못 얻은 쪽(텍스트 레이어도 로컬 OCR 도 비었거나 표시 줄뿐)
- `unverified_units`: 글자는 읽었지만 그림이 큰 쪽 — 그림 안은 모른다(image-heavy·회전 텍스트 포함)

이 두 목록에 **없는 쪽은 로컬 추출·OCR 이 읽은 쪽**이다. 그런 쪽은 외부로 보내지 않는다.

문서 단위로 먼저 거른다. 다음은 Vision 대기열에 **들어가지 않는다**(코드로 사유를 남긴다).

| 코드 | 뜻 |
|---|---|
| `not_pdf` | PDF 가 아니다(XLS·HWP 등). HWP 는 rhwp 가 만든 PDF 로 다시 들어온다 |
| `original_missing` | 원본이 보관돼 있지 않다(원본 URL·객체 없음) — 그릴 원본이 없다 |
| `policy_isolated` | PII·민감 문서로 격리됐거나 차단됐다. 민감 문서 판정은 완화하지 않는다 |
| `screen_notice` | PII 검사가 알림을 남겼다(예: 일부만 읽힌 파일의 민감 용어). 사람이 먼저 본다 |
| `screen_unknown` | PII 검사 결과를 모른다 — 안전하다고 추정하지 않는다 |
| `coverage_unknown` | 쪽 단위 판정이 없다(옛 metadata). 다시 변환해 판정한 뒤에 다룬다 |
| `list_truncated` | 실패 쪽 목록이 metadata 상한(50)에 닿았다 — 전체 목록이 아니므로 고르지 않는다 |
| `no_failed_pages` | 모든 쪽을 로컬에서 읽었다 |
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: metadata 의 쪽 목록 상한(`convert.Coverage.to_json`). 여기 닿으면 전체 목록이 아니다.
METADATA_PAGE_CAP = 50

ELIGIBLE = "eligible"
NOT_PDF = "not_pdf"
ORIGINAL_MISSING = "original_missing"
POLICY_ISOLATED = "policy_isolated"
SCREEN_NOTICE = "screen_notice"
SCREEN_UNKNOWN = "screen_unknown"
COVERAGE_UNKNOWN = "coverage_unknown"
LIST_TRUNCATED = "list_truncated"
NO_FAILED_PAGES = "no_failed_pages"

#: 쪽을 고르는 이유.
REASON_MISSING = "missing"
REASON_UNVERIFIED = "unverified"

_ISOLATED_STATUS = frozenset({"pii_refused", "quarantined", "blocked"})


@dataclass(frozen=True)
class PageCandidate:
    page: int
    reason: str  # REASON_MISSING | REASON_UNVERIFIED


@dataclass(frozen=True)
class DocumentPlan:
    workspace: str
    channel_id: str
    file_id: str
    name: str
    original_sha256: str
    object_path: Path | None
    code: str
    pages: tuple[PageCandidate, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        return self.code == ELIGIBLE

    def to_json(self) -> dict:
        """dry-run 출력. 파일 이름은 남기고 본문은 없다."""
        return {
            "workspace": self.workspace, "channel_id": self.channel_id,
            "file_id": self.file_id, "name": self.name, "code": self.code,
            "pages": [{"page": p.page, "reason": p.reason} for p in self.pages],
        }


def _is_pdf(attachment) -> bool:
    filetype = (attachment.filetype or "").lower()
    return filetype == "pdf" or (attachment.mimetype or "").lower() == "application/pdf"


def plan_document(attachment) -> DocumentPlan:
    """staged 첨부 하나 → 문서 계획. 판정 순서가 계약이다 — 막는 사유가 먼저다."""
    base = dict(
        workspace=attachment.workspace, channel_id=attachment.channel_id,
        file_id=attachment.file_id, name=attachment.name,
        original_sha256=attachment.sha256 or "", object_path=attachment.object_path,
    )
    if not _is_pdf(attachment):
        return DocumentPlan(**base, code=NOT_PDF)
    if (
        attachment.status in _ISOLATED_STATUS
        or attachment.conversion_state == "blocked"
        or attachment.screen_result in {"blocked", "quarantined"}
    ):
        return DocumentPlan(**base, code=POLICY_ISOLATED)
    # 부분 PII 검사는 fail-closed(오너 결정 2026-11-19 이 완화하지 않는 방어). 알림이 있으면
    # 그 알림이 「일부만 읽혀 모른다」 일 수 있다 — 사람이 보기 전에는 보내지 않는다.
    if attachment.screen_result == "passed_with_notice":
        return DocumentPlan(**base, code=SCREEN_NOTICE)
    if attachment.screen_result != "passed":
        return DocumentPlan(**base, code=SCREEN_UNKNOWN)
    if (
        attachment.original_state != "retained"
        or attachment.object_path is None
        or not Path(attachment.object_path).is_file()
        or len(attachment.sha256 or "") != 64
    ):
        return DocumentPlan(**base, code=ORIGINAL_MISSING)
    if attachment.coverage_unit != "page" or not attachment.coverage_total:
        return DocumentPlan(**base, code=COVERAGE_UNKNOWN)
    missing, unverified = attachment.missing_units, attachment.unverified_units
    if len(missing) >= METADATA_PAGE_CAP or len(unverified) >= METADATA_PAGE_CAP:
        return DocumentPlan(**base, code=LIST_TRUNCATED)
    total = attachment.coverage_total
    pages: dict[int, str] = {}
    for number in missing:
        if 1 <= number <= total:
            pages[number] = REASON_MISSING
    for number in unverified:
        if 1 <= number <= total:
            pages.setdefault(number, REASON_UNVERIFIED)
    if not pages:
        return DocumentPlan(**base, code=NO_FAILED_PAGES)
    return DocumentPlan(
        **base, code=ELIGIBLE,
        pages=tuple(PageCandidate(n, pages[n]) for n in sorted(pages)),
    )


def plan(archive_dir: Path | str, *, workspace: str = "", file_ids=()) -> list[DocumentPlan]:
    """보관소 전체(또는 일부)의 문서 계획. 쓰지도 보내지도 않는다."""
    from ..attachment_review import scan

    wanted = set(file_ids or ())
    out: list[DocumentPlan] = []
    for attachment in scan(archive_dir):
        if workspace and attachment.workspace != workspace:
            continue
        if wanted and attachment.file_id not in wanted:
            continue
        # 변환이 다 끝난 파일은 볼 쪽이 없다. 실패·부분 성공만 계획에 올린다.
        if attachment.conversion_state not in {"partial", "failed"}:
            continue
        out.append(plan_document(attachment))
    return sorted(out, key=lambda d: (d.workspace, d.channel_id, d.file_id))


__all__ = [
    "ELIGIBLE", "METADATA_PAGE_CAP", "DocumentPlan", "PageCandidate", "plan", "plan_document",
]
