"""이미지 설명 외부 전송 직전의 fail-closed 판정.

DB·파일·네트워크를 읽지 않는다. worker가 현재 ACL/원본/PII/카탈로그 사실과
영속 승인·dispatch 영수증을 새로 읽어 전달해야 한다. ``ready``는 호출 권한이지
설명 생성 성공이나 원본 수집 성공을 뜻하지 않는다.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from tybot.gateway.base import Sensitivity

from .image_description import ALLOWED_MIME, ImageInput
from .image_model_catalog import ImageModelCatalogEntry, evaluate_free_vision_model

_HASH = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class ImageDescriptionPolicy:
    workspace: str = ""
    revision: str = ""
    enabled: bool = False
    workspace_enabled: bool = False
    model: str = ""
    provider: str = ""
    region: str = ""
    sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL
    catalog_verification_ttl_seconds: float = 0
    max_image_bytes: int = 0
    max_output_tokens: int = 0
    timeout_seconds: float = 0


@dataclass(frozen=True)
class ImageDescriptionApproval:
    approval_id: str
    workspace: str
    channel: str
    file_id: str
    original_sha256: str
    policy_revision: str
    prompt_revision: str
    model: str
    provider: str
    expires_at: datetime
    revoked: bool = False


@dataclass(frozen=True)
class ImageDescriptionDispatch:
    """영속 원장이 commit한 뒤 worker에 주는 1회 dispatch 영수증."""

    dispatch_id: str
    approval_id: str
    workspace: str
    channel: str
    file_id: str
    original_sha256: str
    image_sha256: str
    policy_revision: str
    prompt_revision: str
    model: str
    provider: str
    max_output_tokens: int
    expires_at: datetime
    active: bool = False


@dataclass(frozen=True)
class ImageDescriptionPreflight:
    state: str
    code: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def _future(value: datetime, now: datetime) -> bool:
    return (
        isinstance(value, datetime)
        and value.utcoffset() is not None
        and value > now
    )


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def preflight_image_description(
    image: ImageInput,
    *,
    policy: ImageDescriptionPolicy | None,
    catalog: ImageModelCatalogEntry | None,
    approval: ImageDescriptionApproval | None,
    dispatch: ImageDescriptionDispatch | None,
    prompt_revision: str,
    now: datetime,
    source_verified: bool = False,
    access_verified: bool = False,
    pii_blocked: bool = True,
    bot_generated: bool = True,
) -> ImageDescriptionPreflight:
    """모든 좌표와 현재 사실을 대조한다. 하나라도 모르면 호출하지 않는다."""

    def reject(code: str) -> ImageDescriptionPreflight:
        return ImageDescriptionPreflight("preflight_rejected", code)

    if policy is None:
        return reject("policy_unavailable")
    if policy.enabled is not True or policy.workspace_enabled is not True:
        return ImageDescriptionPreflight("disabled", "image_description_disabled")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return reject("invalid_clock")
    if policy.workspace != image.workspace:
        return reject("workspace_mismatch")
    if (
        not policy.revision
        or not policy.model
        or not policy.provider
        or not policy.region
        or not isinstance(policy.sensitivity, Sensitivity)
        or type(policy.catalog_verification_ttl_seconds) not in (int, float)
        or not math.isfinite(policy.catalog_verification_ttl_seconds)
        or policy.catalog_verification_ttl_seconds <= 0
        or not _positive_integer(policy.max_image_bytes)
        or not _positive_integer(policy.max_output_tokens)
        or type(policy.timeout_seconds) not in (int, float)
        or not math.isfinite(policy.timeout_seconds)
        or policy.timeout_seconds <= 0
    ):
        return reject("policy_invalid")
    if source_verified is not True:
        return reject("source_unverified")
    if access_verified is not True:
        return reject("access_unverified")
    if pii_blocked is not False:
        return reject("pii_blocked")
    if bot_generated is not False:
        return reject("bot_generated")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", image.workspace)
        or not re.fullmatch(r"C[A-Z0-9]{8,}", image.channel)
        or not re.fullmatch(r"F[A-Z0-9]{8,}", image.file_id)
        or not _valid_hash(image.original_sha256)
        or image.mime not in ALLOWED_MIME
        or not isinstance(image.image, bytes)
        or not image.image
    ):
        return reject("input_invalid")
    if len(image.image) > policy.max_image_bytes:
        return reject("image_limit_exceeded")
    if not prompt_revision:
        return reject("prompt_revision_missing")
    if catalog is None:
        return ImageDescriptionPreflight("blocked", "catalog_unavailable")
    if (catalog.model, catalog.provider) != (policy.model, policy.provider):
        return reject("model_not_approved")
    decision = evaluate_free_vision_model(
        catalog,
        sensitivity=policy.sensitivity,
        region=policy.region,
        now=now,
        verification_ttl=timedelta(seconds=policy.catalog_verification_ttl_seconds),
    )
    if not decision.eligible:
        return ImageDescriptionPreflight("blocked", decision.blockers[0])

    if approval is None:
        return ImageDescriptionPreflight("approval_required", "approval_missing")
    if approval.revoked is not False or not approval.approval_id:
        return reject("approval_revoked")
    if not _future(approval.expires_at, now):
        return reject("approval_expired")
    expected_approval = (
        image.workspace,
        image.channel,
        image.file_id,
        image.original_sha256,
        policy.revision,
        prompt_revision,
        policy.model,
        policy.provider,
    )
    actual_approval = (
        approval.workspace,
        approval.channel,
        approval.file_id,
        approval.original_sha256,
        approval.policy_revision,
        approval.prompt_revision,
        approval.model,
        approval.provider,
    )
    if actual_approval != expected_approval:
        return reject("approval_scope_mismatch")

    if dispatch is None:
        return ImageDescriptionPreflight("quota_held", "dispatch_unavailable")
    if dispatch.active is not True or not dispatch.dispatch_id:
        return ImageDescriptionPreflight("quota_held", "dispatch_inactive")
    if not _future(dispatch.expires_at, now):
        return reject("dispatch_expired")
    image_sha256 = hashlib.sha256(image.image).hexdigest()
    expected_dispatch = (
        approval.approval_id,
        *expected_approval,
        image_sha256,
        policy.max_output_tokens,
    )
    actual_dispatch = (
        dispatch.approval_id,
        dispatch.workspace,
        dispatch.channel,
        dispatch.file_id,
        dispatch.original_sha256,
        dispatch.policy_revision,
        dispatch.prompt_revision,
        dispatch.model,
        dispatch.provider,
        dispatch.image_sha256,
        dispatch.max_output_tokens,
    )
    if actual_dispatch != expected_dispatch:
        return reject("dispatch_scope_mismatch")
    return ImageDescriptionPreflight("ready", "")
