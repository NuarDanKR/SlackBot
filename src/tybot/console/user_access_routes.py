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
| POST | `/api/account-sync` | 콘솔 계정 동기화(apply=false 면 미리 보기) |
| GET  | `/api/account-sync/runs` | 동기화 회차 기록 |
| GET  | `/api/bot-sources` | 봇 근거 채널(열린 것)과 등록·해제 이력 |
| POST | `/api/bot-sources` | 채널을 근거로 등록(여러 개) |
| POST | `/api/bot-sources/remove` | 근거에서 해제(여러 개) |
| POST | `/api/console-users/password-reset` | 한 사람에게 임시 비밀번호 재발급(TYBot DM) |
| POST | `/api/password-issue/initial` | 비밀번호를 받은 적 없는 계정에 초기 임시 비밀번호(apply=false 면 미리 보기) |

오류 본문(`detail`)은 코드다(`user_directory.CODES`). 화면이 문구로 바꾼다.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import account_sync, password_issue
from . import user_directory as store
from .auth import ConsoleUser


class GrantBody(BaseModel):
    bot: str = Field(min_length=1, max_length=40)
    empNo: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)
    expiresAt: datetime | None = None


class RevokeBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AccountSyncBody(BaseModel):
    apply: bool = False


class SourceChannel(BaseModel):
    workspace: str = Field(min_length=1, max_length=40)
    channelId: str = Field(min_length=1, max_length=40)
    name: str = Field(default="", max_length=200)


class SourcesBody(BaseModel):
    bot: str = Field(min_length=1, max_length=40)
    channels: list[SourceChannel] = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)


class PasswordResetBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class InitialPasswordBody(BaseModel):
    apply: bool = False


#: 계정 동기화 실패 코드 → HTTP 상태
SYNC_CODES = {"slack_unavailable": 503, "database_unavailable": 503}


def _refused(exc: store.UserDirectoryError) -> HTTPException:
    code = str(exc)
    return HTTPException(status_code=store.CODES.get(code, 422), detail=code)


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    reload_accounts: Callable[[], None] = lambda: None,
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

    @router.post("/api/account-sync")
    def post_account_sync(
        body: AccountSyncBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """Slack 을 읽어 게스트 계정을 맞춘다. 미리 보기도 Slack 을 읽으므로 POST 다."""
        require_admin(user)
        check_write_request(request)
        try:
            result = account_sync.run(actor=user.email, dry_run=not body.apply)
        except account_sync.AccountSyncError as exc:
            code = str(exc)
            raise HTTPException(status_code=SYNC_CODES.get(code, 503), detail=code) from exc
        if body.apply:
            # 멈춘 계정의 로그인을 지금 프로세스에서도 끊는다. 재조회 실패는 결과를 무르지 않는다.
            with contextlib.suppress(Exception):
                reload_accounts()
            audit_event(
                actor=user.email, category="console-user", action="account-sync",
                target_type="account_sync_run", target_id=str(result.get("runId", "")),
                outcome="succeeded", metadata=result["counts"],
            )
        return result

    @router.get("/api/bot-sources")
    def get_bot_sources(bot: str, user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        try:
            return store.list_sources(bot=bot)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc

    @router.post("/api/bot-sources")
    def post_bot_sources(body: SourcesBody, request: Request,
                         user: ConsoleUser = user_dependency) -> dict:
        """채널을 봇 근거로 등록한다. 임원이 멤버가 아닌 채널을 여는 일이라 사유를 받는다."""
        require_admin(user)
        check_write_request(request)
        try:
            result = store.add_sources(bot=body.bot, channels=[c.model_dump() for c in body.channels],
                                       reason=body.reason, actor=user.email)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc
        audit_event(actor=user.email, category="bot-access", action="source-add",
                    target_type="bot_source_channel", target_id=result["bot"],
                    outcome="succeeded",
                    metadata={"added": result["added"], "already": len(result["already"])})
        return result

    @router.post("/api/bot-sources/remove")
    def post_bot_sources_remove(body: SourcesBody, request: Request,
                                user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        check_write_request(request)
        try:
            result = store.remove_sources(bot=body.bot,
                                          channels=[c.model_dump() for c in body.channels],
                                          reason=body.reason, actor=user.email)
        except store.UserDirectoryError as exc:
            raise _refused(exc) from exc
        audit_event(actor=user.email, category="bot-access", action="source-remove",
                    target_type="bot_source_channel", target_id=result["bot"],
                    outcome="succeeded",
                    metadata={"removed": result["removed"], "missing": len(result["missing"])})
        return result

    @router.post("/api/console-users/password-reset")
    def post_password_reset(
        body: PasswordResetBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """임시 비밀번호를 다시 발급해 본인 DM 으로 보낸다. 기존 비밀번호는 바로 못 쓴다."""
        require_admin(user)
        check_write_request(request)
        try:
            result = password_issue.issue(body.email, kind="reset", actor=user.email)
        except password_issue.PasswordError as exc:
            code = str(exc)
            raise HTTPException(status_code=password_issue.CODES.get(code, 422),
                                detail=code) from exc
        with contextlib.suppress(Exception):
            reload_accounts()   # 기존 비밀번호 세션 목록에서 바로 뺀다
        audit_event(
            actor=user.email, category="console-user", action="password-reset",
            target_type="console-user", target_id=result["email"], outcome="succeeded",
            metadata={"workspace": result["workspace"]},
        )
        return {"email": result["email"], "expiresAt": result["expiresAt"],
                "workspace": result["workspace"]}

    @router.post("/api/password-issue/initial")
    def post_initial_passwords(
        body: InitialPasswordBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        try:
            result = password_issue.initial(actor=user.email, apply=body.apply)
        except password_issue.PasswordError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if body.apply:
            audit_event(
                actor=user.email, category="console-user", action="password-initial",
                target_type="console-user", target_id="bulk", outcome="succeeded",
                metadata={"sent": len(result["sent"]), "failed": len(result["failed"])},
            )
        return result

    @router.get("/api/account-sync/runs")
    def get_account_sync_runs(user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        try:
            return {"runs": account_sync.recent_runs()}
        except account_sync.AccountSyncError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router


__all__ = ["AccountSyncBody", "GrantBody", "InitialPasswordBody", "PasswordResetBody",
           "RevokeBody", "SourceChannel", "SourcesBody", "build_router"]
