"""Create and revoke B-81 human safety clearances without trusting browser scope."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..archive import attachment_reader
from ..archive.image_description_clearance import SafetyAssessment
from ..archive.image_description_source import FILETYPE_MIME
from ..archive.image_description_store import ImageDescriptionDenied, ImageDescriptionStore
from . import image_description_admin, reader

MAX_CLEARANCE_LIFETIME = timedelta(hours=24)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ImageDescriptionClearanceAdminError(RuntimeError):
    """A fixed-code refusal safe to expose through the console API."""


@dataclass(frozen=True)
class ClearanceRequest:
    workspace: str
    channel_id: str
    file_id: str
    expected_original_sha256: str
    expires_at: datetime
    pii_cleared: bool
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
        raise ImageDescriptionClearanceAdminError("source_not_found")
    return matches[0]


def _validate_request(request: ClearanceRequest, *, now: datetime) -> None:
    values = (
        request.workspace,
        request.channel_id,
        request.file_id,
        request.expected_original_sha256,
        request.reason,
        request.actor,
    )
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ImageDescriptionClearanceAdminError("invalid_request")
    if request.expected_original_sha256 != request.expected_original_sha256.lower():
        raise ImageDescriptionClearanceAdminError("invalid_request")
    if not _SHA256.fullmatch(request.expected_original_sha256):
        raise ImageDescriptionClearanceAdminError("invalid_request")
    if request.pii_cleared is not True:
        raise ImageDescriptionClearanceAdminError("pii_review_required")
    if now.utcoffset() is None or request.expires_at.utcoffset() is None:
        raise ImageDescriptionClearanceAdminError("invalid_clock")
    if not now < request.expires_at <= now + MAX_CLEARANCE_LIFETIME:
        raise ImageDescriptionClearanceAdminError("invalid_expiry")


def _create(store, request: ClearanceRequest, *, now: datetime, docs=None) -> dict:
    _validate_request(request, now=now)
    doc = _current_doc(
        request.workspace,
        request.channel_id,
        request.file_id,
        docs=docs,
    )
    digest = str(doc.sha256 or "").strip().lower()
    if digest != request.expected_original_sha256:
        raise ImageDescriptionClearanceAdminError("source_changed")
    if not doc.human_upload_confirmed:
        raise ImageDescriptionClearanceAdminError("human_upload_unverified")
    if str(doc.filetype or "").strip().lower() not in FILETYPE_MIME:
        raise ImageDescriptionClearanceAdminError("source_not_image")
    visibility = str(doc.visibility or "").strip()
    acl = frozenset(str(value).strip() for value in doc.acl if str(value).strip())
    if not visibility or not acl:
        raise ImageDescriptionClearanceAdminError("source_acl_unknown")

    try:
        policy, _config, _sources = store.effective_policy(request.workspace)
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionClearanceAdminError("policy_unavailable") from exc
    if not all((policy.provider, policy.model, policy.revision)):
        raise ImageDescriptionClearanceAdminError("policy_unavailable")

    clearance_id = uuid.uuid4().hex
    assessment = SafetyAssessment(
        workspace=request.workspace,
        channel_id=request.channel_id,
        file_id=request.file_id,
        original_sha256=digest,
        provider=policy.provider,
        model=policy.model,
        policy_revision=policy.revision,
        visibility=visibility,
        acl=acl,
        assessed_by=request.actor,
        assessed_at=now,
        expires_at=request.expires_at,
        pii_cleared=True,
    )
    try:
        store.save_clearance(
            clearance_id,
            assessment,
            reason=request.reason,
            actor=request.actor,
        )
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionClearanceAdminError("clearance_not_saved") from exc
    return {
        "id": clearance_id,
        "workspace": request.workspace,
        "channelId": request.channel_id,
        "fileId": request.file_id,
        "originalSha256Mask": digest[:12],
        "provider": policy.provider,
        "model": policy.model,
        "policyRevision": policy.revision,
        "visibility": visibility,
        "aclMemberCount": len(acl),
        "piiCleared": True,
        "assessedAt": now,
        "expiresAt": request.expires_at,
    }


def create_clearance(
    request: ClearanceRequest,
    *,
    now: datetime | None = None,
    docs=None,
    store=None,
) -> dict:
    """Persist a clearance bound to current archive metadata and effective policy."""
    moment = now or datetime.now(UTC)
    if store is not None:
        return _create(store, request, now=moment, docs=docs)
    try:
        with image_description_admin._connect() as connection:
            return _create(
                ImageDescriptionStore(connection),
                request,
                now=moment,
                docs=docs,
            )
    except image_description_admin.ImageDescriptionAdminError as exc:
        raise ImageDescriptionClearanceAdminError(str(exc)) from exc
    except ImageDescriptionClearanceAdminError:
        raise
    except Exception:  # noqa: BLE001 - driver exceptions can contain failing rows
        # Driver errors may include the failing row. Never expose or chain them.
        raise ImageDescriptionClearanceAdminError("vision_database_unavailable") from None


def revoke_clearance(
    clearance_id: str,
    *,
    workspace: str,
    actor: str,
    reason: str,
    store=None,
) -> dict:
    """Revoke only an active clearance; never alter its recorded assessment."""
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (clearance_id, workspace, actor, reason)
    ):
        raise ImageDescriptionClearanceAdminError("invalid_request")

    def revoke(target_store) -> dict:
        try:
            target_store.revoke_clearance(
                clearance_id.strip(),
                workspace=workspace.strip(),
                actor=actor.strip(),
                reason=reason.strip(),
            )
        except ImageDescriptionDenied as exc:
            raise ImageDescriptionClearanceAdminError("clearance_not_active") from exc
        return {"ok": True, "id": clearance_id.strip()}

    if store is not None:
        return revoke(store)
    try:
        with image_description_admin._connect() as connection:
            return revoke(ImageDescriptionStore(connection))
    except image_description_admin.ImageDescriptionAdminError as exc:
        raise ImageDescriptionClearanceAdminError(str(exc)) from exc
    except ImageDescriptionClearanceAdminError:
        raise
    except Exception:  # noqa: BLE001 - driver exceptions can contain failing rows
        raise ImageDescriptionClearanceAdminError("vision_database_unavailable") from None


__all__ = [
    "MAX_CLEARANCE_LIFETIME",
    "ClearanceRequest",
    "ImageDescriptionClearanceAdminError",
    "create_clearance",
    "revoke_clearance",
]
