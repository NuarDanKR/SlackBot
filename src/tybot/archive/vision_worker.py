"""One-shot bridge from a durable Vision reservation to page extraction.

This is deliberately not a queue runner or an activation path.  The caller must
load and verify the current source, rendering, ACL and PII facts and provide the
already rendered page.  ``VisionStore.begin_dispatch`` commits those checks
before this module can send bytes to a provider.
"""
from __future__ import annotations

from decimal import Decimal

from .vision_extract import VisionInputError, VisionPageInput, VisionPageResult, extract_page


def dispatch_reserved_page(
    store,
    job_id: str,
    page: VisionPageInput,
    *,
    checks: dict,
    router,
) -> VisionPageResult:
    """Dispatch one reserved page and settle its charge exactly once.

    A model/provider error may happen after transmission.  If the gateway did
    not return trustworthy usage, the ledger remains ``unknown`` rather than
    recording a false zero.  The charge is ``tokens x tariff`` bound at
    reservation time; the gateway's own ``cost_usd`` is ignored (a zero-priced
    Router spec makes it 0 regardless of the real bill).  ``VisionInputError`` is raised only before the
    gateway call, so that path can be settled as a known zero-cost rejection.
    """
    grant = store.begin_dispatch(job_id, page, checks=checks)
    try:
        result = extract_page(
            page,
            router=router,
            model=grant.model,
            provider=grant.provider,
            max_output_tokens=grant.max_output_tokens,
            timeout_seconds=grant.timeout_seconds,
            sensitivity=grant.sensitivity,
        )
    except VisionInputError:
        store.finish(job_id, actual_usd=Decimal("0"))
        raise
    except Exception:
        # The failure boundary is after the durable dispatch marker.  A provider
        # may already have received the page, so neither retry nor zero charge is
        # safe without reconciliation.
        store.finish(job_id, actual_usd=None)
        raise

    # gateway 의 cost_usd 는 믿지 않는다 — Router 단가가 0 이면 0 이 나온다.
    # 예약 때 묶인 요금표 × 응답 토큰. 하나라도 모르면 None(모름, 예약액 유지).
    charge = getattr(grant, "charge_usd", None)
    actual = charge(result.usage.input_tokens, result.usage.output_tokens) if charge else None
    store.finish(job_id, actual_usd=actual)
    return result


__all__ = ["dispatch_reserved_page"]
