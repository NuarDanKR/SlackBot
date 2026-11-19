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
| `screen_conflict` | 같은 파일의 다른 정본이 PII 알림·모름·격리다 — 한쪽이 통과여도 보내지 않는다 |

## 같은 파일이 여러 번 잡힐 때

staged metadata 는 같은 Slack 파일을 여러 번 남길 수 있다(재수집·여러 메시지에 공유).
- **판정이 엇갈리면 막는다.** 한 정본은 `passed`, 다른 정본은 알림·모름·격리라면 그 파일은
  아직 사람이 보지 않은 것이다. 통과한 쪽만 믿고 보내지 않는다(fail-closed)
- **같은 채널의 같은 계획은 하나로 센다.** 원장은 같은 쪽을 두 번 예약하지 않지만(identity
  UNIQUE), dry-run 숫자가 부풀면 예산·상한을 잘못 정한다
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
SCREEN_CONFLICT = "screen_conflict"

#: 이 판정이 같은 파일의 다른 정본에 있으면 통과한 정본도 보내지 않는다.
_SCREEN_HOLD = frozenset({SCREEN_NOTICE, SCREEN_UNKNOWN, POLICY_ISOLATED})

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
    return sorted(_reconcile(out), key=lambda d: (d.workspace, d.channel_id, d.file_id))


def _reconcile(plans: list[DocumentPlan]) -> list[DocumentPlan]:
    """같은 파일의 여러 정본: 판정이 엇갈리면 막고, 같은 채널의 같은 계획은 하나로."""
    from dataclasses import replace

    held = {(p.workspace, p.file_id) for p in plans if p.code in _SCREEN_HOLD}
    seen: set[tuple] = set()
    out: list[DocumentPlan] = []
    for plan in plans:
        if plan.eligible and (plan.workspace, plan.file_id) in held:
            plan = replace(plan, code=SCREEN_CONFLICT, pages=())
        key = (plan.workspace, plan.channel_id, plan.file_id, plan.original_sha256,
               plan.code, plan.pages)
        if key in seen:
            continue
        seen.add(key)
        out.append(plan)
    return out


__all__ = [
    "ELIGIBLE", "METADATA_PAGE_CAP", "SCREEN_CONFLICT", "DocumentPlan", "PageCandidate", "plan",
    "plan_document",
]
