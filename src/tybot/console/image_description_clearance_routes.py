"""Console write routes for B-81 image safety clearance decisions."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import image_description_clearance_admin as admin
from .auth import ConsoleUser


class ClearanceBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=40)
    channelId: str = Field(min_length=1, max_length=40)
    fileId: str = Field(min_length=1, max_length=120)
    expectedOriginalSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expiresAt: datetime
    piiCleared: Literal[True]
    reason: str = Field(min_length=1, max_length=500)


class RevokeBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)


def _workspace(user: ConsoleUser, value: str) -> str:
    workspace = value.strip().lower()
    if not user.may_see(workspace):
        raise HTTPException(status_code=403, detail="workspace_forbidden")
    return workspace


def _refused(exc: admin.ImageDescriptionClearanceAdminError) -> HTTPException:
    code = str(exc)
    status = 503 if code.startswith("vision_database_") else 422
    return HTTPException(status_code=status, detail=code)


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
) -> APIRouter:
    """Build routes after app security dependencies exist, avoiding an import cycle."""
    router = APIRouter()
    user_dependency = Depends(current_user)

    @router.post("/api/archiver/image-description/clearances")
    def post_clearance(
        body: ClearanceBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            result = admin.create_clearance(
                admin.ClearanceRequest(
                    workspace=workspace,
                    channel_id=body.channelId.strip(),
                    file_id=body.fileId.strip(),
                    expected_original_sha256=body.expectedOriginalSha256,
                    expires_at=body.expiresAt,
                    pii_cleared=body.piiCleared,
                    reason=body.reason.strip(),
                    actor=user.email,
                )
            )
        except admin.ImageDescriptionClearanceAdminError as exc:
            raise _refused(exc) from exc
        audit_event(
            actor=user.email,
            category="archiver_image_description",
            action="clearance_create",
            target_type="image_clearance",
            target_id=result["id"],
            workspace=workspace,
            outcome="succeeded",
            metadata={
                "channel_id": result["channelId"],
                "file_id": result["fileId"],
                "policy_revision": result["policyRevision"],
            },
        )
        return result

    @router.post("/api/archiver/image-description/clearances/{clearance_id}/revoke")
    def post_revoke_clearance(
        clearance_id: str,
        body: RevokeBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            result = admin.revoke_clearance(
                clearance_id,
                workspace=workspace,
                actor=user.email,
                reason=body.reason,
            )
        except admin.ImageDescriptionClearanceAdminError as exc:
            raise _refused(exc) from exc
        audit_event(
            actor=user.email,
            category="archiver_image_description",
            action="clearance_revoke",
            target_type="image_clearance",
            target_id=result["id"],
            workspace=workspace,
            outcome="succeeded",
            metadata={},
        )
        return result

    return router


__all__ = ["ClearanceBody", "RevokeBody", "build_router"]
