from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tybot.archive.image_model_catalog import (
    FreeQuotaState,
    FreeVisionModelUnavailable,
    ImageModelCatalogEntry,
    evaluate_free_vision_model,
    select_free_vision_model,
)
from tybot.gateway.base import Sensitivity

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
TTL = timedelta(hours=24)


def _entry(**changes) -> ImageModelCatalogEntry:
    base = ImageModelCatalogEntry(
        model="free-vision-v1",
        provider="approved-provider",
        supports_vision=True,
        zero_cost_verified=True,
        verified_at=NOW - timedelta(hours=1),
        verification_revision="catalog-7",
        max_sensitivity=Sensitivity.INTERNAL,
        allowed_regions=frozenset({"kr"}),
        credential_ready=True,
        quota_state=FreeQuotaState.AVAILABLE,
        quota_remaining=20,
        quota_resets_at=NOW + timedelta(hours=2),
    )
    return replace(base, **changes)


def _decision(entry: ImageModelCatalogEntry):
    return evaluate_free_vision_model(
        entry,
        sensitivity=Sensitivity.INTERNAL,
        region="kr",
        now=NOW,
        verification_ttl=TTL,
    )


def test_every_verified_condition_is_required() -> None:
    cases = [
        (_entry(supports_vision=False), "vision_not_verified"),
        (_entry(zero_cost_verified=False), "zero_cost_not_verified"),
        (_entry(verified_at=None), "verification_missing"),
        (_entry(verification_revision=""), "verification_missing"),
        (_entry(verified_at=NOW - timedelta(days=2)), "verification_stale"),
        (_entry(max_sensitivity=Sensitivity.PUBLIC), "sensitivity_not_allowed"),
        (_entry(allowed_regions=frozenset({"us"})), "region_not_allowed"),
        (_entry(credential_ready=False), "credential_not_ready"),
        (_entry(quota_state=FreeQuotaState.UNKNOWN), "quota_unknown"),
        (_entry(quota_state=FreeQuotaState.EXHAUSTED), "quota_exhausted"),
        (_entry(quota_remaining=0), "quota_exhausted"),
    ]
    for entry, blocker in cases:
        assert blocker in _decision(entry).blockers


def test_a_fully_verified_entry_is_eligible() -> None:
    decision = _decision(_entry())
    assert decision.eligible
    assert decision.blockers == ()


def test_the_requested_identity_is_selected_exactly() -> None:
    selected = select_free_vision_model(
        (_entry(),),
        model="free-vision-v1",
        provider="approved-provider",
        sensitivity=Sensitivity.INTERNAL,
        region="kr",
        now=NOW,
        verification_ttl=TTL,
    )
    assert (selected.model, selected.provider) == ("free-vision-v1", "approved-provider")


def test_a_blocked_model_does_not_fall_back_to_another_model() -> None:
    blocked = _entry(quota_state=FreeQuotaState.EXHAUSTED)
    other = replace(_entry(), model="other-free-model")
    with pytest.raises(FreeVisionModelUnavailable) as caught:
        select_free_vision_model(
            (blocked, other),
            model=blocked.model,
            provider=blocked.provider,
            sensitivity=Sensitivity.INTERNAL,
            region="kr",
            now=NOW,
            verification_ttl=TTL,
        )
    assert caught.value.blockers == ("quota_exhausted",)


def test_an_unregistered_name_is_not_inferred_from_a_price_or_name() -> None:
    with pytest.raises(FreeVisionModelUnavailable) as caught:
        select_free_vision_model(
            (_entry(),),
            model="free-looking-name-with-zero-price",
            provider="approved-provider",
            sensitivity=Sensitivity.PUBLIC,
            region="kr",
            now=NOW,
            verification_ttl=TTL,
        )
    assert caught.value.blockers == ("model_not_in_catalog",)


def test_naive_clock_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_free_vision_model(
            _entry(),
            sensitivity=Sensitivity.PUBLIC,
            region="kr",
            now=datetime(2026, 10, 27, 12, 0),
            verification_ttl=TTL,
        )


def test_negative_quota_is_not_a_valid_catalog_fact() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _entry(quota_remaining=-1)
