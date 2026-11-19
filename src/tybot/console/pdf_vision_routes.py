"""B-80 PDF 쪽 Vision 전사 콘솔 라우트 — 현황 읽기와 정책 저장.

`image_description_policy_routes` 와 같은 이유로 `build_router` 가 보안 의존성을 받는다
(`app` 을 import 하면 순환이다). 저장은 허가가 아니다 — 모델을 부르지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import pdf_vision_admin as admin
from .auth import ConsoleUser

CONFLICT_CODES = frozenset({"policy_conflict"})
UNAVAILABLE_PREFIXES = ("vision_database_", "pdf_vision_unavailable", "policy_unavailable",
                        "connection_not_idle")
MAX_CONFIG_KEYS = 50


class PolicyBody(BaseModel):
    """`extra="forbid"` — 모르는 칸을 버리면 운영자는 저장됐다고 믿는다. 실행자는 로그인 사용자."""

    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(default="", max_length=40)
    expectedRevision: str | None = Field(default=None, max_length=200)
    enabled: bool
    config: dict = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=admin.MAX_REASON_CHARS)


def _detail(code: str, blockers: tuple[str, ...] = ()) -> dict:
    return {"code": code, "blockers": list(blockers)}


def _workspace(user: ConsoleUser, value: str) -> str:
    workspace = (value or "").strip().lower()
    if workspace == admin.GLOBAL:
        # 전역 기본값은 모든 워크스페이스에 걸린다. 일부만 보는 사람은 못 고친다(원칙 3).
        if not user.all_workspaces:
            raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))
        return admin.GLOBAL
    if not user.may_see(workspace):
        raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))
    return workspace


def _refused(exc: admin.PdfVisionAdminError) -> HTTPException:
    if exc.code in CONFLICT_CODES:
        status = 409
    elif exc.code.startswith(UNAVAILABLE_PREFIXES):
        status = 503
    else:
        status = 422
    return HTTPException(status_code=status, detail=_detail(exc.code, exc.blockers))


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)

    @router.get("/api/archiver/pdf-vision")
    def get_pdf_vision(
        workspace: Annotated[str, Query(min_length=1, max_length=40)],
        limit: Annotated[int, Query(ge=1, le=admin.MAX_JOBS)] = 50,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """정책·요금표·오늘 사용액·작업 상태. 전사 본문·원본·키는 반환하지 않는다."""
        require_admin(user)
        key = _workspace(user, workspace)
        if key == admin.GLOBAL:
            raise HTTPException(status_code=422, detail=_detail("workspace_required"))
        try:
            return admin.snapshot(key, limit=limit)
        except admin.PdfVisionAdminError as exc:
            raise _refused(exc) from None

    @router.put("/api/archiver/pdf-vision/policy")
    def put_pdf_vision_policy(
        body: PolicyBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """정책 행 하나를 고쳐 쓴다. 모델도 소급도 시작하지 않는다."""
        require_admin(user)
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        if len(body.config) > MAX_CONFIG_KEYS:
            raise HTTPException(status_code=422, detail=_detail("config_key_unknown"))
        try:
            said = admin.save_policy_change(admin.PolicyChangeRequest(
                workspace=workspace, expected_revision=body.expectedRevision,
                enabled=body.enabled, config=dict(body.config), reason=body.reason.strip(),
                actor=user.email,
            ))
        except admin.PdfVisionAdminError as exc:
            raise _refused(exc) from None
        # 저장은 끝났다. 감사 실패로 500 을 내면 다시 누르고 revision 이 둘 생긴다.
        audited = True
        try:
            audit_event(**said.audit)
        except Exception:  # noqa: BLE001
            audited = False
        return {
            "workspace": said.workspace, "revision": said.revision,
            "previousRevision": said.previous_revision, "enabled": said.enabled,
            "changedKeys": list(said.changed_keys), "audited": audited,
        }

    return router


__all__ = ["CONFLICT_CODES", "PolicyBody", "build_router"]
