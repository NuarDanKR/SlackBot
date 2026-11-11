"""Vision provider API 자격 관리 API. 평문 조회 경로는 없다."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import image_description_secret_admin as admin
from .auth import ConsoleUser
from .image_description_admin import ImageDescriptionAdminError


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=2, max_length=32)
    key: str | None = Field(default=None, min_length=16, max_length=4096)
    enabled: bool | None = None
    reason: str = Field(min_length=1, max_length=500)


def _rows(rows: tuple[dict, ...]) -> list[dict]:
    return [
        {
            "provider": row["provider"],
            "mask": row["mask"],
            "enabled": row["enabled"],
            "updatedBy": row["updated_by"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


def _refused(exc: ImageDescriptionAdminError) -> HTTPException:
    code = str(exc)
    status = 503 if code.startswith("vision_database_") else 422
    return HTTPException(status_code=status, detail={"code": code})


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)

    @router.get("/api/archiver/image-description/provider-secrets")
    def get_provider_secrets(user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        if not user.all_workspaces:
            raise HTTPException(status_code=403, detail={"code": "workspace_forbidden"})
        try:
            return {"secrets": _rows(admin.list_secrets())}
        except ImageDescriptionAdminError as exc:
            raise _refused(exc) from None

    @router.put("/api/archiver/image-description/provider-secrets")
    def put_provider_secret(
        body: SecretBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        if not user.all_workspaces:
            raise HTTPException(status_code=403, detail={"code": "workspace_forbidden"})
        check_write_request(request)
        try:
            rows = admin.save_secret(
                body.provider,
                key=body.key,
                enabled=body.enabled,
                actor=user.email,
            )
        except ImageDescriptionAdminError as exc:
            raise _refused(exc) from None
        audited = True
        try:
            audit_event(
                actor=user.email,
                category="image-description",
                action="image_description_provider_secret_changed",
                target_type="vision-provider",
                target_id=body.provider,
                outcome="succeeded",
                metadata={
                    "reason": body.reason.strip(),
                    "keyReplaced": body.key is not None,
                    "enabled": body.enabled,
                },
            )
        except Exception:  # noqa: BLE001 - 저장 성공 뒤 재시도를 유도하지 않는다
            audited = False
        return {"secrets": _rows(rows), "audited": audited}

    return router


__all__ = ["SecretBody", "build_router"]
