from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tybot.archive.attachment_doc import AttachmentDoc
from tybot.archive.image_description import PROMPT_REVISION
from tybot.archive.image_description_store import ImageDescriptionDenied
from tybot.console.image_description_approval_admin import (
    ApprovalRequest,
    ImageDescriptionApprovalAdminError,
    create_approval,
    revoke_approval,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
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
        "acl": frozenset({"U1"}),
        "filetype": "png",
        "sha256": SHA,
        "staged_at": "2026-10-28T10:00:00+00:00",
        "human_upload_confirmed": True,
    }
    values.update(changes)
    return AttachmentDoc(**values)


def _request(**changes) -> ApprovalRequest:
    values = {
        "workspace": "tyit",
        "channel_id": "C12345678",
        "file_id": "F12345678",
        "expected_original_sha256": SHA,
        "expires_at": NOW + timedelta(hours=1),
        "reason": "외부 전송 범위 확인",
        "actor": "owner@example.com",
    }
    values.update(changes)
    return ApprovalRequest(**values)


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

    def approve(self, approval, *, actor, reason):
        self.saved.append((approval, actor, reason))

    def revoke(self, approval_id, *, workspace, actor, reason):
        self.revoked.append((approval_id, workspace, actor, reason))


def test_approval_scope_comes_from_current_source_policy_and_prompt() -> None:
    store = Store()
    result = create_approval(_request(), now=NOW, docs=[_doc()], store=store)

    approval, actor, reason = store.saved[0]
    assert result["originalSha256Mask"] == SHA[:12]
    assert "reason" not in result and "acl" not in result
    assert (
        approval.workspace,
        approval.channel,
        approval.file_id,
        approval.original_sha256,
    ) == ("tyit", "C12345678", "F12345678", SHA)
    assert (approval.provider, approval.model, approval.policy_revision) == (
        "free-provider",
        "free-vision",
        "policy-7",
    )
    assert approval.prompt_revision == PROMPT_REVISION
    assert (actor, reason) == ("owner@example.com", "외부 전송 범위 확인")


@pytest.mark.parametrize(
    ("approval_request", "doc", "code"),
    [
        (_request(expected_original_sha256="b" * 64), _doc(), "source_changed"),
        (_request(), _doc(human_upload_confirmed=False), "human_upload_unverified"),
        (_request(), _doc(filetype="pdf"), "source_not_image"),
        (_request(), _doc(acl=frozenset()), "source_acl_unknown"),
        (_request(), _doc(visibility=""), "source_acl_unknown"),
        (
            _request(expires_at=NOW + timedelta(hours=24, seconds=1)),
            _doc(),
            "invalid_expiry",
        ),
    ],
)
def test_refusal_happens_before_an_approval_is_saved(
    approval_request, doc, code
) -> None:
    store = Store()
    with pytest.raises(ImageDescriptionApprovalAdminError, match=f"^{code}$"):
        create_approval(approval_request, now=NOW, docs=[doc], store=store)
    assert store.saved == []


def test_only_the_latest_revision_can_be_approved() -> None:
    old = _doc(staged_at="2026-10-27T10:00:00+00:00")
    current = replace(
        old,
        sha256="b" * 64,
        revision="revision-2",
        staged_at="2026-10-28T10:00:00+00:00",
    )
    with pytest.raises(ImageDescriptionApprovalAdminError, match=r"^source_changed$"):
        create_approval(_request(), now=NOW, docs=[old, current], store=Store())


def test_policy_failure_is_a_fixed_code() -> None:
    class BrokenStore(Store):
        def effective_policy(self, workspace):
            raise ImageDescriptionDenied("private database detail")

    with pytest.raises(ImageDescriptionApprovalAdminError, match=r"^policy_unavailable$"):
        create_approval(_request(), now=NOW, docs=[_doc()], store=BrokenStore())


def test_revoke_is_bound_to_workspace_and_reason() -> None:
    store = Store()
    assert revoke_approval(
        "approval-1",
        workspace="tyit",
        actor="owner@example.com",
        reason="전송 승인 철회",
        store=store,
    ) == {"ok": True, "id": "approval-1"}
    assert store.revoked == [
        ("approval-1", "tyit", "owner@example.com", "전송 승인 철회")
    ]
