"""Archiver 이미지 설명용 무료 Vision 모델 카탈로그 경계.

일반 LLM 단가표의 ``0`` 이나 모델 이름으로 무료/Vision 지원을 추측하지 않는다.
운영 검증 결과가 명시된 항목만 평가하며, 요청한 모델이 막히면 다른 모델(특히
유료 모델)로 폴백하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tybot.gateway.base import Sensitivity


class FreeQuotaState(StrEnum):
    """무료 quota를 서버가 마지막으로 확인한 상태."""

    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImageModelCatalogEntry:
    """시크릿을 포함하지 않는 이미지 설명 모델 검증 결과."""

    model: str
    provider: str
    supports_vision: bool
    zero_cost_verified: bool
    verified_at: datetime | None
    verification_revision: str
    max_sensitivity: Sensitivity
    allowed_regions: frozenset[str]
    credential_ready: bool
    quota_state: FreeQuotaState
    quota_remaining: int | None = None
    quota_resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.provider.strip():
            raise ValueError("model/provider must not be empty")
        if self.quota_remaining is not None and self.quota_remaining < 0:
            raise ValueError("quota_remaining must be non-negative or None")


@dataclass(frozen=True)
class ImageModelDecision:
    """콘솔과 worker가 함께 쓰는 후보 판정. blocker가 비면 호출 가능하다."""

    entry: ImageModelCatalogEntry
    blockers: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.blockers


class FreeVisionModelUnavailable(PermissionError):
    """요청한 정확한 모델을 무료 이미지 설명에 사용할 수 없다."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("free vision model unavailable: " + ",".join(blockers))


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def evaluate_free_vision_model(
    entry: ImageModelCatalogEntry,
    *,
    sensitivity: Sensitivity,
    region: str,
    now: datetime,
    verification_ttl: timedelta,
) -> ImageModelDecision:
    """Vision·무료·정책·credential·quota를 모두 확인한다.

    ``now``와 검증 시각은 timezone-aware여야 한다. 시각을 비교하지 못하는 항목을
    최신으로 간주하면 오래된 무료 계약이 영구히 살아남으므로 거절한다.
    """
    if not _aware(now):
        raise ValueError("now must be timezone-aware")
    if verification_ttl <= timedelta(0):
        raise ValueError("verification_ttl must be positive")

    blockers: list[str] = []
    if not entry.supports_vision:
        blockers.append("vision_not_verified")
    if not entry.zero_cost_verified:
        blockers.append("zero_cost_not_verified")
    if not entry.verification_revision.strip() or not _aware(entry.verified_at):
        blockers.append("verification_missing")
    elif now - entry.verified_at > verification_ttl:
        blockers.append("verification_stale")
    elif entry.verified_at > now:
        blockers.append("verification_from_future")
    if entry.max_sensitivity.rank() < sensitivity.rank():
        blockers.append("sensitivity_not_allowed")
    if not region.strip() or region not in entry.allowed_regions:
        blockers.append("region_not_allowed")
    if not entry.credential_ready:
        blockers.append("credential_not_ready")
    if entry.quota_state is FreeQuotaState.UNKNOWN:
        blockers.append("quota_unknown")
    elif entry.quota_state is FreeQuotaState.EXHAUSTED or entry.quota_remaining == 0:
        blockers.append("quota_exhausted")

    return ImageModelDecision(entry=entry, blockers=tuple(blockers))


def select_free_vision_model(
    entries: tuple[ImageModelCatalogEntry, ...],
    *,
    model: str,
    provider: str,
    sensitivity: Sensitivity,
    region: str,
    now: datetime,
    verification_ttl: timedelta,
) -> ImageModelCatalogEntry:
    """요청한 정확한 identity만 고른다. 거절 시 다른 모델로 폴백하지 않는다."""
    found = next(
        (entry for entry in entries if entry.model == model and entry.provider == provider),
        None,
    )
    if found is None:
        raise FreeVisionModelUnavailable(("model_not_in_catalog",))

    decision = evaluate_free_vision_model(
        found,
        sensitivity=sensitivity,
        region=region,
        now=now,
        verification_ttl=verification_ttl,
    )
    if not decision.eligible:
        raise FreeVisionModelUnavailable(decision.blockers)
    return found
