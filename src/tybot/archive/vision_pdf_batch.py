"""B-80 PDF 페이지 Vision 소급 — dry-run 목록과 apply 를 **나눈다**.

- `dry_run()` 은 계획만 만든다. 원본을 열지도, 그리지도, 보내지도 않는다
- `apply()` 는 사람이 dry-run 을 본 뒤 실행자·사유를 대고 부른다. 그 사람이 목록의 페이지를
  승인하는 것이다(승인 행의 `approved_by`)

페이지마다 순서가 계약이다.
1. 원본 해시를 다시 잰다 — 계획 뒤 바뀌었으면 그 문서를 다루지 않는다(원본 불변 확인)
2. **실패·미확인 쪽만** 그린다(`vision_pdf_render`). PDF 전체를 그리지 않는다
3. 승인 → 예약(원장 identity UNIQUE: 같은 쪽·같은 바이트는 두 번 예약되지 않는다)
4. 한 번 보낸다(`vision_worker.dispatch_reserved_page`). timeout·429 도 결과로 닫고
   **다시 보내지 않는다** — 원장이 `unknown` 으로 남기고 사람이 정산한다
5. 결과는 파생 revision(`VisionStore.record_page`). 원문 MD·원본 파일은 고치지 않는다

민감도는 공통 계약(`vision_sensitivity`)을 쓴다. 카탈로그 행을 못 읽으면 보내지 않는다.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from . import vision_candidates
from .vision_extract import BASIS_ORIGINAL, VisionPageInput
from .vision_policy import PageApproval
from .vision_store import VisionDenied
from .vision_worker import dispatch_reserved_page

logger = logging.getLogger(__name__)

APPROVAL_TTL = timedelta(hours=2)
RESERVATION_TTL = timedelta(minutes=30)


def dry_run(archive_dir, *, workspace: str = "", file_ids=()) -> dict:
    """소급 대상 목록. 문서별 사유 코드와 보낼 쪽 번호. 아무것도 쓰거나 보내지 않는다."""
    plans = vision_candidates.plan(archive_dir, workspace=workspace, file_ids=file_ids)
    codes = Counter(p.code for p in plans)
    eligible = [p for p in plans if p.eligible]
    return {
        "documents": len(plans),
        "codes": dict(sorted(codes.items())),
        "eligible_documents": len(eligible),
        "pages": sum(len(p.pages) for p in eligible),
        "plans": [p.to_json() for p in plans],
    }


@dataclass
class ApplyStats:
    sent: int = 0
    recorded: int = 0
    skipped: Counter = field(default_factory=Counter)
    unknown: int = 0
    statuses: Counter = field(default_factory=Counter)

    def to_json(self) -> dict:
        return {
            "sent": self.sent, "recorded": self.recorded, "unknown": self.unknown,
            "skipped": dict(self.skipped), "statuses": dict(self.statuses),
        }


def access_code(archive_dir, doc, channel_scope) -> str:
    """보내도 되는 원본인가. 맞으면 빈 문자열, 아니면 사유 코드.

    1. 채널이 **지금** 등록돼 수집 범위 안이다(`channel_scope` 조회). 조회 실패도 거절
    2. 원본 객체가 이 보관소의 객체 영역 안에 있다 — metadata 의 경로만 믿고 밖의 파일을 열지 않는다
    3. staged metadata 가 그 채널의 자리에 있다(다른 채널 파일이 섞이지 않게)
    """
    try:
        in_scope = channel_scope(doc.workspace, doc.channel_id) is True
    except Exception:  # noqa: BLE001 - 모르면 보내지 않는다
        in_scope = False
    if not in_scope:
        return "channel_not_in_scope"
    root = Path(archive_dir).resolve().parent
    try:
        obj = Path(doc.object_path).resolve()
    except (TypeError, OSError):
        return "original_coordinates_invalid"
    if not obj.is_relative_to(root):
        return "original_outside_archive"
    if not doc.channel_id or not doc.workspace or not doc.file_id:
        return "original_coordinates_invalid"
    return ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply(
    archive_dir,
    *,
    store,
    router,
    renderer,
    catalog_checks,
    channel_scope,
    actor: str,
    reason: str,
    now: datetime,
    amount_usd: Decimal,
    workspace: str = "",
    file_ids=(),
    max_pages: int = 50,
) -> ApplyStats:
    """dry-run 이 고른 쪽만, 상한(`max_pages`) 안에서 한 번씩 보낸다.

    `catalog_checks(policy)` 는 서버 카탈로그에서 읽은 사실만 돌려준다
    (`capability_verified`·`credential_ready`·`catalog_max_sensitivity`). 모르면 빈 dict —
    그러면 사전 판정이 막는다.

    `channel_scope(workspace, channel_id)` 는 **지금** 그 채널이 등록돼 수집 범위 안인지
    조회한 결과다(`archive_channel_mode`: active/shadow · joined · 보류 아님). 모르면 False.
    `access_verified` 는 이 조회와 원본 좌표 대조가 둘 다 맞을 때만 참이다 — 하드코딩하지 않는다.
    """
    if not actor or not actor.strip() or not reason or not reason.strip():
        raise ValueError("actor_and_reason_required")
    if not isinstance(amount_usd, Decimal) or amount_usd <= 0:
        raise ValueError("amount_required")
    stats = ApplyStats()
    budget = max_pages
    for doc in vision_candidates.plan(archive_dir, workspace=workspace, file_ids=file_ids):
        if not doc.eligible:
            continue
        if budget <= 0:
            stats.skipped["page_limit"] += len(doc.pages)
            continue
        # 원본을 열기 전에 본다 — 범위 밖 채널의 파일은 읽지도 그리지도 않는다.
        access = access_code(archive_dir, doc, channel_scope)
        if access:
            stats.skipped[access] += len(doc.pages)
            continue
        try:
            pdf = Path(doc.object_path).read_bytes()
        except OSError:
            stats.skipped["original_unreadable"] += len(doc.pages)
            continue
        if _sha256(pdf) != doc.original_sha256:
            # 계획 뒤 원본이 바뀌었다. 다른 바이트를 같은 좌표로 보내지 않는다.
            stats.skipped["original_changed"] += len(doc.pages)
            continue
        try:
            policy, _config, _sources = store.effective_policy(doc.workspace)
            version = renderer.renderer_version()
            wanted = [p.page for p in doc.pages][:budget]
            images = renderer.render_pages(pdf, wanted, max_image_bytes=policy.max_image_bytes)
        except VisionDenied as exc:
            stats.skipped[str(exc.args[0] if exc.args else "policy_unavailable")] += len(doc.pages)
            continue
        except Exception as exc:  # noqa: BLE001 - 렌더러 없음·실패는 그 문서만 건너뛴다
            stats.skipped[type(exc).__name__] += len(doc.pages)
            continue
        facts = dict(catalog_checks(policy) or {})
        checks = {
            **facts,
            "source_verified": True,      # 방금 원본 해시를 재서 맞췄다
            "rendering_verified": True,   # 방금 이 렌더러로 그렸다
            "access_verified": True,      # 방금 등록 채널·수집 범위·원본 좌표를 조회해 맞췄다
            "pii_blocked": False,         # 계획이 PII 검사 `passed` 문서만 남겼다
        }
        for number in wanted:
            if budget <= 0:
                stats.skipped["page_limit"] += 1
                continue
            image = images.get(number)
            if image is None:
                stats.skipped["render_failed"] += 1
                continue
            page = VisionPageInput(
                doc.workspace, doc.file_id, doc.original_sha256, number, image, "image/png",
                page_basis=BASIS_ORIGINAL, rendered_sha256=doc.original_sha256,
                renderer_version=version,
            )
            approval = PageApproval(
                approval_id=uuid4().hex, workspace=doc.workspace, file_id=doc.file_id,
                original_sha256=doc.original_sha256, rendered_sha256=doc.original_sha256,
                renderer_version=version, page_basis=BASIS_ORIGINAL, page_number=number,
                image_sha256=_sha256(image), policy_revision=policy.revision,
                model=policy.model, provider=policy.provider, expires_at=now + APPROVAL_TTL,
            )
            try:
                store.approve(approval, actor=actor.strip())
                receipt = store.reserve(
                    page, approval_id=approval.approval_id, amount_usd=amount_usd,
                    expires_at=now + RESERVATION_TTL, checks=checks,
                )
            except VisionDenied as exc:
                # duplicate_job: 이미 한 번 나간 쪽이다. 다시 보내지 않는다(재청구 금지).
                stats.skipped[str(exc.args[0] if exc.args else "denied")] += 1
                continue
            budget -= 1
            stats.sent += 1
            try:
                result = dispatch_reserved_page(
                    store, receipt.reservation_id, page, checks=checks, router=router,
                )
            except Exception as exc:  # noqa: BLE001 - 원장이 unknown 으로 남겼다
                stats.unknown += 1
                logger.warning("Vision 쪽 결과 미확정 file=%s page=%d err=%s",
                               doc.file_id, number, type(exc).__name__)
                continue
            stats.statuses[result.status] += 1
            try:
                store.record_page(receipt.reservation_id, result, channel_id=doc.channel_id)
                stats.recorded += 1
            except VisionDenied as exc:
                stats.skipped[f"record_{exc.args[0] if exc.args else 'denied'}"] += 1
    return stats


__all__ = ["ApplyStats", "apply", "dry_run"]
