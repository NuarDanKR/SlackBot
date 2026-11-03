"""콘솔 API — 사용자 목록과 봇 사용 권한 (`/api/people` · `/api/bot-access`).

저장소: `user_directory` · 판정: `tybot.bot_access`

전부 **admin 전용**이다. 사람 목록에 이메일·사번·조직이 들어 있고, 권한 부여는
임원용 봇의 문을 여는 일이다. 쓰기는 CSRF 검사와 감사 기록을 거친다 — 규칙은
`app.py` 의 것을 빌린다(`build_router` 로 주입, import 순환 회피).

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| GET  | `/api/people` | 사람 목록(view=slack·granted·all, q, workspace, bot) |
| GET  | `/api/bot-access` | 한 봇의 부여·회수 이력 |
| GET  | `/api/bot-access/check` | 봇이 내릴 판정 그대로(사번 또는 워크스페이스+Slack 사용자) |
| POST | `/api/bot-access` | 부여 |
| POST | `/api/bot-access/{id}/revoke` | 회수 |

오류 본문(`detail`)은 코드다(`user_directory.CODES`). 화면이 문구로 바꾼다.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import user_directory as store
from .auth import ConsoleUser


class GrantBody(BaseModel):
    bot: str = Field(min_length=1, max_length=40)
    empNo: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)
    expiresAt: datetime | None = None


class RevokeBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _refused(exc: store.UserDirectoryError) -> HTTPException:
    code = str(exc)
    return HTTPException(status_code=store.CODES.get(code, 422), detail=code)


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)

    @router.get("/api/people")
    def get_people(
        view: str = "slack",
        q: str | None = Query(default=None, max_length=80),
        workspace: str | None = Query(default=None, max_length=40),
        bot: str | None = Query(default=None, max_length=40),
        limit: int = 50,
        offset: int = 0,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        try:
            return store.list_people(view=view, q=q, workspace=workspace, bot=bot,
                                     limit=limit, offset=offset)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc

    @router.get("/api/bot-access")
    def get_bot_access(
        bot: str,
        empNo: str | None = Query(default=None, max_length=40),
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        try:
            return store.list_grants(bot=bot, emp_no=empNo)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc

    @router.get("/api/bot-access/check")
    def get_bot_access_check(
        bot: str,
        empNo: str | None = Query(default=None, max_length=40),
        workspace: str | None = Query(default=None, max_length=40),
        slackUser: str | None = Query(default=None, max_length=40),
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        try:
            return store.check(bot=bot, emp_no=empNo, workspace=workspace,
                               slack_user=slackUser)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc

    @router.post("/api/bot-access")
    def post_bot_access(
        body: GrantBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        try:
            result = store.grant(store.GrantRequest(
                bot=body.bot, emp_no=body.empNo, reason=body.reason,
                actor=user.email, expires_at=body.expiresAt,
            ))
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc
        audit_event(
            actor=user.email, category="bot-access", action="grant",
            target_type="bot_access", target_id=str(result["id"]),
            outcome="succeeded",
            metadata={"bot": result["bot"], "emp_no": result["empNo"],
                      "expires_at": result["expiresAt"].isoformat()
                      if result["expiresAt"] else None},
        )
        return result

    @router.post("/api/bot-access/{grant_id}/revoke")
    def post_bot_access_revoke(
        grant_id: int,
        body: RevokeBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        try:
            result = store.revoke(grant_id, reason=body.reason, actor=user.email)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc
        audit_event(
            actor=user.email, category="bot-access", action="revoke",
            target_type="bot_access", target_id=str(result["id"]),
            outcome="succeeded",
            metadata={"bot": result["bot"], "emp_no": result["empNo"]},
        )
        return result

    return router


__all__ = ["GrantBody", "RevokeBody", "build_router"]
