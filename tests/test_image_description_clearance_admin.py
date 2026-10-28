from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tybot.archive.attachment_doc import AttachmentDoc
from tybot.archive.image_description_store import ImageDescriptionDenied
from tybot.console.image_description_clearance_admin import (
    ClearanceRequest,
    ImageDescriptionClearanceAdminError,
    create_clearance,
    revoke_clearance,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SHA = "a" * 64


def _doc(**changes) -> AttachmentDoc:
    values = {
        "workspace": "tyit",
        "channel_id": "C12345678",
        "channel": "현장",
        "file_id": "F12345678",
        "name": "photo.png",
        "revision": "revision-1",
        "visibility": "private",
        "acl": frozenset({"U1", "U2"}),
        "filetype": "png",
        "sha256": SHA,
        "staged_at": "2026-10-28T10:00:00+00:00",
        "human_upload_confirmed": True,
    }
    values.update(changes)
    return AttachmentDoc(**values)


def _request(**changes) -> ClearanceRequest:
    values = {
        "workspace": "tyit",
        "channel_id": "C12345678",
        "file_id": "F12345678",
        "expected_original_sha256": SHA,
        "expires_at": NOW + timedelta(hours=1),
        "pii_cleared": True,
        "reason": "사진을 직접 확인함",
        "actor": "owner@example.com",
    }
    values.update(changes)
    return ClearanceRequest(**values)


class Store:
    def __init__(self):
        self.saved = []
        self.revoked = []

    def effective_policy(self, workspace):
        assert workspace == "tyit"
        return (
            SimpleNamespace(
                provider="free-provider",
                model="free-vision",
                revision="policy-7",
            ),
            {},
            {},
        )

    def save_clearance(self, clearance_id, assessment, *, reason, actor):
        self.saved.append((clearance_id, assessment, reason, actor))

    def revoke_clearance(self, clearance_id, *, workspace, actor, reason):
        self.revoked.append((clearance_id, workspace, actor, reason))


def test_clearance_scope_comes_from_current_source_and_effective_policy() -> None:
    store = Store()
    result = create_clearance(_request(), now=NOW, docs=[_doc()], store=store)

    assert result["originalSha256Mask"] == SHA[:12]
    assert result["aclMemberCount"] == 2
    assert "acl" not in result
    assert "reason" not in result
    _, assessment, reason, actor = store.saved[0]
    assert (assessment.provider, assessment.model, assessment.policy_revision) == (
        "free-provider",
        "free-vision",
        "policy-7",
    )
    assert assessment.acl == frozenset({"U1", "U2"})
    assert assessment.visibility == "private"
    assert reason == "사진을 직접 확인함"
    assert actor == "owner@example.com"


@pytest.mark.parametrize(
    ("clearance_request", "doc", "code"),
    [
        (_request(expected_original_sha256="b" * 64), _doc(), "source_changed"),
        (_request(), _doc(human_upload_confirmed=False), "human_upload_unverified"),
        (_request(), _doc(acl=frozenset()), "source_acl_unknown"),
        (_request(), _doc(visibility=""), "source_acl_unknown"),
        (_request(), _doc(filetype="pdf"), "source_not_image"),
        (_request(pii_cleared=False), _doc(), "pii_review_required"),
        (
            _request(expires_at=NOW + timedelta(hours=24, seconds=1)),
            _doc(),
            "invalid_expiry",
        ),
    ],
)
def test_refusal_happens_before_any_clearance_is_saved(
    clearance_request, doc, code
) -> None:
    store = Store()
    with pytest.raises(ImageDescriptionClearanceAdminError, match=f"^{code}$"):
        create_clearance(clearance_request, now=NOW, docs=[doc], store=store)
    assert store.saved == []


def test_only_the_latest_revision_can_be_cleared() -> None:
    old = _doc(staged_at="2026-10-27T10:00:00+00:00")
    current = replace(
        old,
        sha256="b" * 64,
        revision="revision-2",
        staged_at="2026-10-28T10:00:00+00:00",
    )
    store = Store()

    with pytest.raises(ImageDescriptionClearanceAdminError, match=r"^source_changed$"):
        create_clearance(_request(), now=NOW, docs=[old, current], store=store)
    assert store.saved == []


def test_policy_failure_is_a_fixed_code() -> None:
    class BrokenStore(Store):
        def effective_policy(self, workspace):
            raise ImageDescriptionDenied("database row details must not escape")

    with pytest.raises(ImageDescriptionClearanceAdminError, match=r"^policy_unavailable$"):
        create_clearance(_request(), now=NOW, docs=[_doc()], store=BrokenStore())


def test_revoke_is_bound_to_the_requested_workspace() -> None:
    store = Store()
    assert revoke_clearance(
        "clearance-1",
        workspace="tyit",
        actor="owner@example.com",
        reason="승인 취소",
        store=store,
    ) == {"ok": True, "id": "clearance-1"}
    assert store.revoked == [
        ("clearance-1", "tyit", "owner@example.com", "승인 취소")
    ]


def test_write_routes_are_registered_before_the_static_mount() -> None:
    from tybot.console import image_description_clearance_routes as routes
    from tybot.console.app import app, image_description_clearance_router

    paths = {
        (route.path, tuple(route.methods or ()))
        for route in image_description_clearance_router.routes
    }
    assert any(
        path == "/api/archiver/image-description/clearances" and "POST" in methods
        for path, methods in paths
    )
    assert any(
        path == "/api/archiver/image-description/clearances/{clearance_id}/revoke"
        and "POST" in methods
        for path, methods in paths
    )
    assert any(
        path == "/api/archiver/image-description/approvals" and "POST" in methods
        for path, methods in paths
    )
    assert any(
        path == "/api/archiver/image-description/approvals/{approval_id}/revoke"
        and "POST" in methods
        for path, methods in paths
    )
    assert any(
        getattr(route, "original_router", None) is image_description_clearance_router
        for route in app.routes
    )
    assert routes.build_router
