from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tybot.archive.image_description import PROMPT_REVISION, ImageInput
from tybot.archive.image_description_policy import (
    ImageDescriptionApproval,
    ImageDescriptionDispatch,
    ImageDescriptionPolicy,
    preflight_image_description,
)
from tybot.archive.image_model_catalog import (
    FreeQuotaState,
    ImageModelCatalogEntry,
)
from tybot.gateway.base import Sensitivity

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
IMAGE_BYTES = b"synthetic image bytes"
SHA = "a" * 64
IMAGE = ImageInput("tyit", "C12345678", "F12345678", SHA, IMAGE_BYTES, "image/png")
POLICY = ImageDescriptionPolicy(
    workspace="tyit",
    revision="global-1:tyit-2",
    enabled=True,
    workspace_enabled=True,
    model="free-vision-v1",
    provider="approved-provider",
    region="kr",
    sensitivity=Sensitivity.INTERNAL,
    catalog_verification_ttl_seconds=3600,
    max_image_bytes=1024,
    max_output_tokens=256,
    timeout_seconds=30,
)
ENTRY = ImageModelCatalogEntry(
    model=POLICY.model,
    provider=POLICY.provider,
    supports_vision=True,
    zero_cost_verified=True,
    verified_at=NOW - timedelta(minutes=5),
    verification_revision="catalog-1",
    max_sensitivity=Sensitivity.INTERNAL,
    allowed_regions=frozenset({"kr"}),
    credential_ready=True,
    quota_state=FreeQuotaState.AVAILABLE,
    quota_remaining=10,
)
CATALOG = ENTRY
APPROVAL = ImageDescriptionApproval(
    approval_id="approval-1",
    workspace=IMAGE.workspace,
    channel=IMAGE.channel,
    file_id=IMAGE.file_id,
    original_sha256=IMAGE.original_sha256,
    policy_revision=POLICY.revision,
    prompt_revision=PROMPT_REVISION,
    model=POLICY.model,
    provider=POLICY.provider,
    expires_at=NOW + timedelta(hours=1),
)
DISPATCH = ImageDescriptionDispatch(
    dispatch_id="dispatch-1",
    approval_id=APPROVAL.approval_id,
    workspace=IMAGE.workspace,
    channel=IMAGE.channel,
    file_id=IMAGE.file_id,
    original_sha256=IMAGE.original_sha256,
    image_sha256=hashlib.sha256(IMAGE_BYTES).hexdigest(),
    policy_revision=POLICY.revision,
    prompt_revision=PROMPT_REVISION,
    model=POLICY.model,
    provider=POLICY.provider,
    max_output_tokens=POLICY.max_output_tokens,
    expires_at=NOW + timedelta(minutes=10),
    active=True,
)


def _run(**changes):
    values = dict(
        image=IMAGE,
        policy=POLICY,
        catalog=CATALOG,
        approval=APPROVAL,
        dispatch=DISPATCH,
        prompt_revision=PROMPT_REVISION,
        now=NOW,
        source_verified=True,
        access_verified=True,
        pii_blocked=False,
        bot_generated=False,
    )
    values.update(changes)
    return preflight_image_description(**values)


def test_every_current_fact_is_required_before_an_external_call() -> None:
    cases = [
        ({"source_verified": False}, "source_unverified"),
        ({"access_verified": False}, "access_unverified"),
        ({"pii_blocked": True}, "pii_blocked"),
        ({"bot_generated": True}, "bot_generated"),
    ]
    for changes, code in cases:
        verdict = _run(**changes)
        assert not verdict.ready
        assert verdict.code == code


def test_policy_off_is_not_a_collection_failure() -> None:
    verdict = _run(policy=replace(POLICY, workspace_enabled=False))
    assert (verdict.state, verdict.code) == ("disabled", "image_description_disabled")


def test_catalog_blockers_are_preserved_without_fallback() -> None:
    blocked = replace(ENTRY, quota_state=FreeQuotaState.EXHAUSTED)
    verdict = _run(catalog=blocked)
    assert (verdict.state, verdict.code) == ("blocked", "quota_exhausted")


def test_catalog_identity_must_match_the_saved_policy() -> None:
    other = replace(ENTRY, model="other-model")
    verdict = _run(catalog=other)
    assert verdict.code == "model_not_approved"


def test_catalog_freshness_is_rechecked_at_dispatch_time() -> None:
    old = replace(ENTRY, verified_at=NOW - timedelta(hours=2))
    verdict = _run(catalog=old)
    assert (verdict.state, verdict.code) == ("blocked", "verification_stale")


@pytest.mark.parametrize(
    ("approval", "code"),
    [
        (replace(APPROVAL, revoked=True), "approval_revoked"),
        (replace(APPROVAL, expires_at=NOW), "approval_expired"),
        (replace(APPROVAL, prompt_revision="old-prompt"), "approval_scope_mismatch"),
        (replace(APPROVAL, original_sha256="b" * 64), "approval_scope_mismatch"),
    ],
)
def test_approval_is_bound_to_the_exact_source_and_revisions(approval, code) -> None:
    assert _run(approval=approval).code == code


# --- 2026-11-20 오너 결정: 건별 승인·PII 판별은 전제가 아니다 -------------

POLICY_DISPATCH = replace(DISPATCH, approval_id="")


def test_no_approval_and_no_pii_assessment_is_ready_when_policy_bound() -> None:
    verdict = preflight_image_description(
        image=IMAGE, policy=POLICY, catalog=CATALOG, approval=None,
        dispatch=POLICY_DISPATCH, prompt_revision=PROMPT_REVISION, now=NOW,
        source_verified=True, access_verified=True, pii_blocked=False, bot_generated=False,
    )
    assert verdict.ready


def test_an_omitted_pii_fact_is_not_read_as_cleared() -> None:
    verdict = preflight_image_description(
        image=IMAGE, policy=POLICY, catalog=CATALOG, approval=None,
        dispatch=POLICY_DISPATCH, prompt_revision=PROMPT_REVISION, now=NOW,
        source_verified=True, access_verified=True, bot_generated=False,
    )
    assert verdict.code == "pii_blocked"


@pytest.mark.parametrize(
    "dispatch",
    [
        replace(POLICY_DISPATCH, policy_revision="global-1:tyit-1"),
        replace(POLICY_DISPATCH, model="other-model"),
        replace(POLICY_DISPATCH, prompt_revision="old-prompt"),
        replace(POLICY_DISPATCH, original_sha256="b" * 64),
        # 옛 승인에 묶인 영수증을 승인 없이 쓰지 않는다.
        DISPATCH,
    ],
)
def test_policy_bound_dispatch_must_match_the_current_policy(dispatch) -> None:
    assert _run(approval=None, dispatch=dispatch).code == "dispatch_scope_mismatch"


def test_policy_off_still_blocks_without_an_approval() -> None:
    verdict = _run(
        approval=None, dispatch=POLICY_DISPATCH, policy=replace(POLICY, enabled=False)
    )
    assert verdict.code == "image_description_disabled"


def test_explicit_pii_block_is_still_honoured() -> None:
    assert _run(approval=None, dispatch=POLICY_DISPATCH, pii_blocked=True).code == "pii_blocked"


@pytest.mark.parametrize(
    ("dispatch", "code"),
    [
        (None, "dispatch_unavailable"),
        (replace(DISPATCH, active=False), "dispatch_inactive"),
        (replace(DISPATCH, expires_at=NOW), "dispatch_expired"),
        (replace(DISPATCH, image_sha256="b" * 64), "dispatch_scope_mismatch"),
        (replace(DISPATCH, max_output_tokens=999), "dispatch_scope_mismatch"),
    ],
)
def test_dispatch_is_one_exact_committed_permission(dispatch, code) -> None:
    assert _run(dispatch=dispatch).code == code


def test_large_images_are_rejected_before_the_router() -> None:
    verdict = _run(policy=replace(POLICY, max_image_bytes=len(IMAGE_BYTES) - 1))
    assert verdict.code == "image_limit_exceeded"


def test_all_gates_open_only_for_the_exact_approved_image() -> None:
    verdict = _run()
    assert verdict.ready
    assert (verdict.state, verdict.code) == ("ready", "")
