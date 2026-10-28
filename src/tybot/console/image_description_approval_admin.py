"""Create and revoke exact B-81 external-transmission approvals."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..archive import attachment_reader
from ..archive.image_description import PROMPT_REVISION
from ..archive.image_description_policy import ImageDescriptionApproval
from ..archive.image_description_source import FILETYPE_MIME
from ..archive.image_description_store import ImageDescriptionDenied, ImageDescriptionStore
from . import image_description_admin, reader

MAX_APPROVAL_LIFETIME = timedelta(hours=24)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ImageDescriptionApprovalAdminError(RuntimeError):
    """A fixed-code refusal safe to expose through the console API."""


@dataclass(frozen=True)
class ApprovalRequest:
    workspace: str
    channel_id: str
    file_id: str
    expected_original_sha256: str
    expires_at: datetime
    reason: str
    actor: str


def _current_doc(workspace: str, channel_id: str, file_id: str, *, docs=None):
    revisions = (
        list(docs)
        if docs is not None
        else attachment_reader.all_revisions(reader.archive_dir())
    )
    matches = [
        doc
        for doc in attachment_reader.current_by_file(revisions)
        if (doc.workspace, doc.channel_id, doc.file_id)
        == (workspace, channel_id, file_id)
    ]
    if len(matches) != 1:
        raise ImageDescriptionApprovalAdminError("source_not_found")
    return matches[0]


def _validate(request: ApprovalRequest, *, now: datetime) -> None:
    values = (
        request.workspace,
        request.channel_id,
        request.file_id,
        request.expected_original_sha256,
        request.reason,
        request.actor,
    )
    if any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in values
    ):
        raise ImageDescriptionApprovalAdminError("invalid_request")
    if not _SHA256.fullmatch(request.expected_original_sha256):
        raise ImageDescriptionApprovalAdminError("invalid_request")
    if now.utcoffset() is None or request.expires_at.utcoffset() is None:
        raise ImageDescriptionApprovalAdminError("invalid_clock")
    if not now < request.expires_at <= now + MAX_APPROVAL_LIFETIME:
        raise ImageDescriptionApprovalAdminError("invalid_expiry")


def _create(store, request: ApprovalRequest, *, now: datetime, docs=None) -> dict:
    _validate(request, now=now)
    doc = _current_doc(
        request.workspace,
        request.channel_id,
        request.file_id,
        docs=docs,
    )
    digest = str(doc.sha256 or "").strip().lower()
    if digest != request.expected_original_sha256:
        raise ImageDescriptionApprovalAdminError("source_changed")
    if not doc.human_upload_confirmed:
        raise ImageDescriptionApprovalAdminError("human_upload_unverified")
    if str(doc.filetype or "").strip().lower() not in FILETYPE_MIME:
        raise ImageDescriptionApprovalAdminError("source_not_image")
    visibility = str(doc.visibility or "").strip()
    acl = frozenset(str(value).strip() for value in doc.acl if str(value).strip())
    if not visibility or not acl:
        raise ImageDescriptionApprovalAdminError("source_acl_unknown")

    try:
        policy, _config, _sources = store.effective_policy(request.workspace)
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionApprovalAdminError("policy_unavailable") from exc
    if not all((policy.provider, policy.model, policy.revision)):
        raise ImageDescriptionApprovalAdminError("policy_unavailable")

    approval_id = uuid.uuid4().hex
    approval = ImageDescriptionApproval(
        approval_id=approval_id,
        workspace=request.workspace,
        channel=request.channel_id,
        file_id=request.file_id,
        original_sha256=digest,
        policy_revision=policy.revision,
        prompt_revision=PROMPT_REVISION,
        model=policy.model,
        provider=policy.provider,
        expires_at=request.expires_at,
    )
    try:
        store.approve(approval, actor=request.actor, reason=request.reason)
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionApprovalAdminError("approval_not_saved") from exc
    return {
        "id": approval_id,
        "workspace": request.workspace,
        "channelId": request.channel_id,
        "fileId": request.file_id,
        "originalSha256Mask": digest[:12],
        "provider": policy.provider,
        "model": policy.model,
        "policyRevision": policy.revision,
        "promptRevision": PROMPT_REVISION,
        "expiresAt": request.expires_at,
    }


def create_approval(
    request: ApprovalRequest,
    *,
    now: datetime | None = None,
    docs=None,
    store=None,
) -> dict:
    """Persist an approval bound to current metadata and the effective route."""
    moment = now or datetime.now(UTC)
    if store is not None:
        return _create(store, request, now=moment, docs=docs)
    try:
        with image_description_admin._connect() as connection:
            return _create(ImageDescriptionStore(connection), request, now=moment, docs=docs)
    except image_description_admin.ImageDescriptionAdminError as exc:
        raise ImageDescriptionApprovalAdminError(str(exc)) from exc
    except ImageDescriptionApprovalAdminError:
        raise
    except Exception:  # noqa: BLE001 - driver errors can contain failing rows
        raise ImageDescriptionApprovalAdminError("vision_database_unavailable") from None


def revoke_approval(
    approval_id: str,
    *,
    workspace: str,
    actor: str,
    reason: str,
    store=None,
) -> dict:
    """Revoke an active approval in the caller's workspace only."""
    values = (approval_id, workspace, actor, reason)
    if any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in values
    ):
        raise ImageDescriptionApprovalAdminError("invalid_request")

    def revoke(target_store) -> dict:
        try:
            target_store.revoke(
                approval_id,
                workspace=workspace,
                actor=actor,
                reason=reason,
            )
        except ImageDescriptionDenied as exc:
            raise ImageDescriptionApprovalAdminError("approval_not_active") from exc
        return {"ok": True, "id": approval_id}

    if store is not None:
        return revoke(store)
    try:
        with image_description_admin._connect() as connection:
            return revoke(ImageDescriptionStore(connection))
    except image_description_admin.ImageDescriptionAdminError as exc:
        raise ImageDescriptionApprovalAdminError(str(exc)) from exc
    except ImageDescriptionApprovalAdminError:
        raise
    except Exception:  # noqa: BLE001 - driver errors can contain failing rows
        raise ImageDescriptionApprovalAdminError("vision_database_unavailable") from None


__all__ = [
    "MAX_APPROVAL_LIFETIME",
    "ApprovalRequest",
    "ImageDescriptionApprovalAdminError",
    "create_approval",
    "revoke_approval",
]
