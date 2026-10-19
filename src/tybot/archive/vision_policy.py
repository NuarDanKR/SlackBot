"""Fail-closed preflight for document Vision pages. No DB, files, or model calls.

Policy, approval and reservation must be loaded freshly by the future worker.
This module is not an activation path or a substitute for a durable dispatch ledger.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .vision_extract import BASIS_ORIGINAL, BASIS_RENDERED, VisionPageInput

_HASH = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class VisionPolicy:
    workspace: str = ""
    revision: str = ""
    enabled: bool = False
    workspace_enabled: bool = False
    model: str = ""
    provider: str = ""
    capability_verified: bool = False
    credential_ready: bool = False
    max_image_bytes: int = 0
    max_output_tokens: int = 0
    timeout_seconds: float = 0


@dataclass(frozen=True)
class PageApproval:
    approval_id: str
    workspace: str
    file_id: str
    original_sha256: str
    rendered_sha256: str
    renderer_version: str
    page_basis: str
    page_number: int
    image_sha256: str
    policy_revision: str
    model: str
    provider: str
    expires_at: datetime
    revoked: bool = False


@dataclass(frozen=True)
class PageReservation:
    """Receipt from an atomic budget/dispatch ledger, not a caller's estimate."""

    reservation_id: str
    approval_id: str
    workspace: str
    file_id: str
    original_sha256: str
    image_sha256: str
    page_number: int
    page_basis: str
    rendered_sha256: str
    renderer_version: str
    policy_revision: str
    model: str
    provider: str
    max_output_tokens: int
    amount_usd: Decimal
    expires_at: datetime
    active: bool = False


@dataclass(frozen=True)
class Preflight:
    state: str
    code: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def _future(value: datetime, now: datetime) -> bool:
    return (
        isinstance(value, datetime) and value.utcoffset() is not None
        and value > now
    )


def _positive_integer(value) -> bool:
    return type(value) is int and value > 0


def _valid_hash(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def preflight(
    page: VisionPageInput,
    *,
    policy: VisionPolicy | None,
    approval: PageApproval | None,
    reservation: PageReservation | None,
    now: datetime,
    source_verified: bool = False,
    rendering_verified: bool = False,
    access_verified: bool = False,
    pii_blocked: bool = True,
) -> Preflight:
    """Check immutable page coordinates before any gateway call.

    True verification flags must come from trusted current source/render/ACL checks.
    Missing DB data is None, never a Master/env fallback. Ready does not dispatch.
    """
    def reject(code: str) -> Preflight:
        return Preflight("preflight_rejected", code)
    if policy is None:
        return reject("policy_unavailable")
    if policy.enabled is not True or policy.workspace_enabled is not True:
        return Preflight("disabled", "vision_disabled")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return reject("invalid_clock")
    if policy.workspace != page.workspace:
        return reject("workspace_mismatch")
    if (not policy.revision or not policy.model or not policy.provider
            or not _positive_integer(policy.max_image_bytes)
            or not _positive_integer(policy.max_output_tokens)
            or type(policy.timeout_seconds) not in (int, float)
            or not math.isfinite(policy.timeout_seconds) or policy.timeout_seconds <= 0):
        return reject("policy_invalid")
    if policy.capability_verified is not True:
        return reject("vision_capability_unverified")
    if policy.credential_ready is not True:
        return reject("credential_unavailable")
    if access_verified is not True:
        return reject("access_unverified")
    if pii_blocked is not False:
        return reject("pii_blocked")
    if source_verified is not True or rendering_verified is not True:
        return reject("source_unverified")
    if (not isinstance(page.workspace, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", page.workspace)
            or not isinstance(page.file_id, str)
            or not re.fullmatch(r"F[A-Z0-9]{8,}", page.file_id)
            or not _valid_hash(page.original_sha256)
            or not _positive_integer(page.page_number)
            or page.page_basis not in (BASIS_ORIGINAL, BASIS_RENDERED)
            or page.mime not in ("image/png", "image/jpeg")
            or not isinstance(page.image, bytes) or not page.image):
        return reject("input_invalid")
    if len(page.image) > policy.max_image_bytes:
        return reject("image_limit_exceeded")
    rendered = page.rendered_sha256 or page.original_sha256
    if (not _valid_hash(rendered)
            or (page.page_basis == BASIS_ORIGINAL and rendered != page.original_sha256)
            or (page.page_basis == BASIS_RENDERED
                and (not page.rendered_sha256 or not page.renderer_version))):
        return reject("render_identity_invalid")
    image_hash = hashlib.sha256(page.image).hexdigest()
    if approval is None:
        return Preflight("approval_required", "approval_missing")
    if approval.revoked is not False or not approval.approval_id:
        return reject("approval_revoked")
    if not _future(approval.expires_at, now):
        return reject("approval_expired")
    expected = (
        page.workspace, page.file_id, page.original_sha256, rendered,
        page.renderer_version, page.page_basis, page.page_number, image_hash,
        policy.revision, policy.model, policy.provider,
    )
    actual = (
        approval.workspace, approval.file_id, approval.original_sha256,
        approval.rendered_sha256, approval.renderer_version, approval.page_basis,
        approval.page_number, approval.image_sha256, approval.policy_revision,
        approval.model, approval.provider,
    )
    if actual != expected:
        return reject("approval_scope_mismatch")
    if reservation is None:
        return Preflight("budget_held", "reservation_unavailable")
    if (reservation.active is not True or not reservation.reservation_id
            or not _future(reservation.expires_at, now)
            or not isinstance(reservation.amount_usd, Decimal)
            or not reservation.amount_usd.is_finite() or reservation.amount_usd <= 0):
        return Preflight("budget_held", "reservation_inactive")
    if (
        reservation.approval_id, reservation.workspace, reservation.file_id,
        reservation.original_sha256, reservation.image_sha256, reservation.page_number,
        reservation.page_basis, reservation.rendered_sha256, reservation.renderer_version,
        reservation.policy_revision, reservation.model, reservation.provider,
        reservation.max_output_tokens,
    ) != (
        approval.approval_id, page.workspace, page.file_id, page.original_sha256,
        image_hash, page.page_number, page.page_basis, rendered, page.renderer_version,
        policy.revision, policy.model, policy.provider,
        policy.max_output_tokens,
    ):
        return reject("reservation_scope_mismatch")
    return Preflight("ready", "")
