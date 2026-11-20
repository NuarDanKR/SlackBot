"""사람이 남긴 사실 정정 — 콘솔에서 보고 철회한다(2026-11-20 오너 결정).

정정은 원문 권한자 모두의 답변에 정정자 이름과 함께 붙는다. 틀린 정정이나 악의적인
정정을 정정자 본인만 지울 수 있으면 그 사람이 없을 때 계속 퍼진다 — 그래서 운영
기능은 콘솔에 둔다(CLAUDE.md 「운영 기능은 콘솔에 둔다」).

- 읽기·철회 모두 관리자이고, 그 워크스페이스를 볼 수 있어야 한다(원칙 3)
- 철회는 사유 필수·CSRF·감사. 행을 지우지 않는다 — 누가 왜 철회했는지가 기록이다
- 원문 좌표는 돌려주지 않는다. 화면에는 정정 문장과 채널 ID 만 보인다
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..fact_correction import Store
from .auth import ConsoleUser

MAX_REASON_CHARS = 300


class FactCorrectionUnavailable(RuntimeError):
    pass


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise FactCorrectionUnavailable("database_unconfigured")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise FactCorrectionUnavailable("database_unavailable") from exc


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _row(item) -> dict:
    return {
        "id": item.id,
        "correctorId": item.corrector_id,
        "correctorName": item.corrector_name,
        "subject": item.subject,
        "before": item.before,
        "after": item.after,
        "body": item.body,
        "channelId": item.source_channel_id,
        "createdAt": _iso(item.created_at),
        "expiresAt": _iso(item.expires_at),
        "showCount": item.show_count,
        "revokedAt": _iso(item.revoked_at),
        "revokedBy": item.revoked_by,
        "revokeReason": item.revoke_reason,
    }


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    connect: Callable = _connect,
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)

    def _workspace(user: ConsoleUser, value: str) -> str:
        workspace = (value or "").strip().lower()
        if not workspace or not user.may_see(workspace):
            raise HTTPException(status_code=403, detail={"code": "workspace_forbidden"})
        return workspace

    @router.get("/api/fact-corrections")
    def list_fact_corrections(
        workspace: Annotated[str, Query(min_length=1, max_length=40)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        key = _workspace(user, workspace)
        try:
            with connect() as conn:
                items = Store(conn).recent(key, limit=limit)
        except FactCorrectionUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": str(exc)}) from None
        return {"workspace": key, "corrections": [_row(item) for item in items]}

    @router.post("/api/fact-corrections/{correction_id}/revoke")
    def revoke_fact_correction(
        correction_id: int,
        body: RevokeBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        key = _workspace(user, body.workspace)
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail={"code": "reason_required"})
        try:
            with connect() as conn:
                revoked = Store(conn).revoke(
                    correction_id, workspace=key, actor=user.email, reason=reason
                )
        except FactCorrectionUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": str(exc)}) from None
        if not revoked:
            raise HTTPException(status_code=404, detail={"code": "correction_not_active"})
        audited = True
        try:
            audit_event(
                actor=user.email,
                category="answer",
                action="fact_correction_revoke",
                target_type="fact_correction",
                target_id=str(correction_id),
                workspace=key,
                metadata={"reason_chars": len(reason)},
            )
        except Exception:  # noqa: BLE001 - 철회는 끝났다. 감사 실패로 되돌리지 않는다
            audited = False
        return {"id": correction_id, "revoked": True, "audited": audited}

    return router


__all__ = ["FactCorrectionUnavailable", "RevokeBody", "build_router"]
