from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from tybot.archive.vision_extract import VisionPageInput
from tybot.archive.vision_policy import PageApproval, PageReservation, VisionPolicy, preflight

NOW = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
PAGE = VisionPageInput("tyit", "F0TEST0001", "a" * 64, 3, b"synthetic-page", "image/png")
IMAGE_HASH = sha256(PAGE.image).hexdigest()
POLICY = VisionPolicy(
    workspace="tyit", revision="rev-1", enabled=True, workspace_enabled=True,
    model="test-model", provider="anthropic", capability_verified=True,
    credential_ready=True, max_image_bytes=1024, max_output_tokens=2000, timeout_seconds=30,
)
APPROVAL = PageApproval(
    "approval-1", "tyit", PAGE.file_id, PAGE.original_sha256, PAGE.original_sha256,
    "", PAGE.page_basis, 3, IMAGE_HASH, "rev-1", "test-model", "anthropic",
    NOW + timedelta(minutes=10),
)
RESERVATION = PageReservation(
    "reservation-1", "approval-1", "tyit", PAGE.file_id, PAGE.original_sha256,
    IMAGE_HASH, 3, PAGE.page_basis, PAGE.original_sha256, "",
    "rev-1", "test-model", "anthropic", 2000, Decimal("0.1"),
    NOW + timedelta(minutes=2), active=True,
)


def check(**over):
    options = dict(
        policy=POLICY, approval=APPROVAL, reservation=RESERVATION, now=NOW,
        source_verified=True, rendering_verified=True, access_verified=True, pii_blocked=False,
    )
    options.update(over)
    page = options.pop("page", PAGE)
    return preflight(page, **options)


def test_defaults_cannot_activate_vision():
    assert not check(policy=VisionPolicy()).ready
    assert check(policy=None).code == "policy_unavailable"
    assert not preflight(PAGE, policy=POLICY, approval=APPROVAL,
                         reservation=RESERVATION, now=NOW).ready


def test_ready_is_only_a_preflight_not_a_model_call():
    assert check().ready
    assert check().state == "ready"


@pytest.mark.parametrize(("changes", "code"), [
    ({"enabled": False}, "vision_disabled"),
    ({"workspace_enabled": False}, "vision_disabled"),
    ({"workspace": "mgmt"}, "workspace_mismatch"),
    ({"revision": ""}, "policy_invalid"),
    ({"model": ""}, "policy_invalid"),
    ({"provider": ""}, "policy_invalid"),
    ({"max_output_tokens": 0}, "policy_invalid"),
    ({"max_output_tokens": True}, "policy_invalid"),
    ({"max_image_bytes": 0}, "policy_invalid"),
    ({"timeout_seconds": float("nan")}, "policy_invalid"),
    ({"timeout_seconds": float("inf")}, "policy_invalid"),
    ({"timeout_seconds": -1}, "policy_invalid"),
    ({"capability_verified": False}, "vision_capability_unverified"),
    ({"credential_ready": False}, "credential_unavailable"),
])
def test_invalid_or_disabled_policies_are_closed(changes, code):
    out = check(policy=replace(POLICY, **changes))
    assert not out.ready and out.code == code


@pytest.mark.parametrize(("option", "value", "code"), [
    ("source_verified", False, "source_unverified"),
    ("rendering_verified", False, "source_unverified"),
    ("access_verified", False, "access_unverified"),
    ("pii_blocked", True, "pii_blocked"),
    ("pii_blocked", None, "pii_blocked"),
])
def test_fresh_source_and_access_checks_are_required(option, value, code):
    out = check(**{option: value})
    assert not out.ready and out.code == code


def test_approval_is_separate_from_enabled_policy_and_model():
    assert check(approval=None).state == "approval_required"
    assert check(approval=replace(APPROVAL, revoked=True)).code == "approval_revoked"
    assert check(approval=replace(APPROVAL, expires_at=NOW)).code == "approval_expired"
    assert check(approval=replace(APPROVAL, expires_at=NOW.replace(tzinfo=None))).code == "approval_expired"


@pytest.mark.parametrize(("field", "value"), [
    ("workspace", "mgmt"), ("file_id", "F0OTHER001"),
    ("original_sha256", "b" * 64), ("rendered_sha256", "b" * 64),
    ("image_sha256", "b" * 64), ("page_number", 4),
    ("renderer_version", "different-renderer"), ("page_basis", "rendered_pdf"),
    ("policy_revision", "rev-2"), ("provider", "openai"), ("model", "other-model"),
])
def test_approval_cannot_be_reused_for_a_different_scope(field, value):
    out = check(approval=replace(APPROVAL, **{field: value}))
    assert not out.ready and out.code == "approval_scope_mismatch"


def test_budget_reservation_is_mandatory_and_bound_to_this_page():
    assert check(reservation=None).state == "budget_held"
    assert check(reservation=replace(RESERVATION, active=False)).code == "reservation_inactive"
    assert check(reservation=replace(RESERVATION, expires_at=NOW)).code == "reservation_inactive"
    for amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("0"), 0.1):
        assert not check(reservation=replace(RESERVATION, amount_usd=amount)).ready


@pytest.mark.parametrize(("field", "value"), [
    ("approval_id", "other-approval"), ("workspace", "mgmt"),
    ("file_id", "F0OTHER001"), ("original_sha256", "b" * 64),
    ("image_sha256", "b" * 64), ("page_number", 4),
    ("page_basis", "rendered_pdf"), ("rendered_sha256", "b" * 64),
    ("renderer_version", "other-renderer"),
    ("policy_revision", "rev-2"), ("provider", "openai"),
    ("model", "other-model"), ("max_output_tokens", 3000),
])
def test_reservation_cannot_pay_for_another_dispatch(field, value):
    assert check(reservation=replace(RESERVATION, **{field: value})).code == "reservation_scope_mismatch"


def test_changed_page_bytes_and_render_versions_invalidate_approval():
    assert check(page=replace(PAGE, image=b"different-page")).code == "approval_scope_mismatch"
    assert check(page=replace(PAGE, image=b"x" * 1025)).code == "image_limit_exceeded"
    assert check(page=replace(PAGE, page_number=True)).code == "input_invalid"
    assert check(page=replace(PAGE, original_sha256="not-a-hash")).code == "input_invalid"
    assert check(page=replace(PAGE, rendered_sha256="b" * 64)).code == "render_identity_invalid"
    assert check(page=replace(PAGE, page_basis="rendered_pdf",
                              rendered_sha256="b" * 64)).code == "render_identity_invalid"


def test_rendered_hwp_page_needs_both_hashes_and_renderer_approval():
    page = replace(PAGE, page_basis="rendered_pdf", rendered_sha256="b" * 64,
                   renderer_version="renderer-fonts-v1")
    grant = replace(APPROVAL, rendered_sha256=page.rendered_sha256,
                    page_basis=page.page_basis, renderer_version=page.renderer_version)
    reservation = replace(RESERVATION, page_basis=page.page_basis,
                          rendered_sha256=page.rendered_sha256,
                          renderer_version=page.renderer_version)
    assert check(page=page, approval=grant, reservation=reservation).ready
    assert not check(page=page, approval=grant).ready
    assert not check(page=replace(page, renderer_version="renderer-fonts-v2"), approval=grant,
                     reservation=reservation).ready
