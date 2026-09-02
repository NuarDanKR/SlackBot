"""관리 콘솔 API.

실행:
    uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787

## 보안 전제
- 기본 바인딩은 루프백이다. 외부 노출 경로와 접근 통제는 아직 결정되지 않았다.
- 그래도 사용자 식별은 한다 — 누가 승인했고 누가 원문을 열었는지 남겨야 하기 때문이다(`auth.py`).
- 응답에 담지 않는 것: 사용자 질문·답변 본문, 시크릿 원문.
- 아카이브 원문 본문은 **관리자에게만**, 그리고 **열람 기록을 남기며** 내려보낸다.

환경설정 쓰기는 admin 전용 허용 목록만 제공한다. 원문·시크릿·임의 파일 편집 경로는 없다.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import deploy_request
from ..archive.store import ArchiveStore
from ..feedback import FeedbackLog
from ..managed_env import request_restart
from . import (
    account_store,
    deploy_approval_store,
    env_settings,
    health,
    llm_secret_store,
    reader,
    service_logs,
    timer_manager,
    workspace_store,
)
from .auth import (
    ROLES,
    SESSION_COOKIE,
    SESSION_HOURS,
    AuthConfigurationError,
    Authenticator,
    AuthError,
    ConsoleUser,
)

logger = logging.getLogger("tybot.console.api")
KST = timezone(timedelta(hours=9))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """뜨기 전에 계정 저장소를 확인한다.

    DB 계정이 없거나 DB 가 끊겼으면 **시작하지 않는다.** 그대로 뜨면 로그인 화면은
    나오는데 아무도 못 들어가는 상태가 되고, 원인은 요청이 올 때까지 드러나지 않는다.

    `@app.on_event("startup")` 대신 이걸 쓴다 — 그쪽은 FastAPI 가 폐기 예고한
    방식이라 배포할 때마다 경고가 찍힌다. 로그가 지저분하면 사람이 로그를 안 본다.
    """
    authenticator()
    yield


app = FastAPI(
    title="TYBot 관리 콘솔 API",
    version="0.1.0",
    # 개발·운영 점검용 문서 페이지. 외부 노출 경로를 정할 때 접근 제한도 함께 검토한다.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

_auth: Authenticator | None = None
_store: ArchiveStore | None = None


def authenticator() -> Authenticator:
    """설정을 프로세스 수명 동안 한 번만 읽는다. 테스트는 이 의존성을 갈아끼운다."""
    global _auth
    if _auth is None:
        _auth = Authenticator()
    return _auth


def store() -> ArchiveStore:
    global _store
    if _store is None:
        _store = ArchiveStore(reader.archive_dir())
    return _store


def reset_state() -> None:
    """테스트에서 환경변수를 바꾼 뒤 캐시를 비운다."""
    global _auth, _store
    _auth = None
    _store = None


def current_user(
    # Depends 로 받아야 테스트에서 `dependency_overrides` 로 갈아끼울 수 있다.
    # 함수 안에서 authenticator() 를 직접 부르면 오버라이드가 무시된다.
    auth: Annotated[Authenticator, Depends(authenticator)],
    tybot_console: Annotated[str | None, Cookie()] = None,
    x_forwarded_email: Annotated[str | None, Header()] = None,
) -> ConsoleUser:
    try:
        return auth.identify(session=tybot_console, forwarded_email=x_forwarded_email)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


class LoginBody(BaseModel):
    email: str
    password: str


class EnvSettingsBody(BaseModel):
    realtimeIngest: bool
    autojoinChannels: bool
    replyInThread: bool


class ConsoleAccountBody(BaseModel):
    email: str
    name: str
    role: Literal["guest", "developer", "admin"]
    active: bool = True
    workspaces: list[str] = Field(default_factory=list)
    password: str | None = None


class TimerActionBody(BaseModel):
    unit: str
    action: Literal["enable", "disable", "run", "schedule"]
    preset: str | None = None


class WorkspaceBody(BaseModel):
    label: str
    role: Literal["root", "member"] = "member"
    state: Literal["enabled", "disabled"] = "enabled"
    limitUsd: float = Field(default=2, ge=0, le=10000)
    readable: list[str] = Field(default_factory=list)
    botToken: str | None = None
    appToken: str | None = None


class DeployRequestBody(BaseModel):
    workspace: str
    reason: str


class DeployDecisionBody(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""


@app.post("/api/login")
def login(
    body: LoginBody,
    response: Response,
    auth: Annotated[Authenticator, Depends(authenticator)],
) -> dict:
    """회사 이메일·비밀번호로 로그인하고 세션 쿠키를 받는다.

    쿠키를 쓰는 이유: 화면이 토큰을 들고 있으면 localStorage 에 남고, 화면 스크립트가
    읽을 수 있는 값이 된다. HttpOnly 쿠키는 스크립트가 읽지 못한다.
    `SameSite=strict` 로 두어 다른 사이트에서 이 콘솔로 요청을 보낼 수 없게 한다.
    """
    try:
        session = auth.login(body.email, body.password)
    except AuthError as e:
        logger.warning("로그인 실패 — 이메일 %r", body.email)
        raise HTTPException(status_code=401, detail=str(e)) from e

    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="strict",
        # 기본 구성은 http 이므로 secure 를 강제하지 않는다.
        # HTTPS 를 붙이면 CONSOLE_COOKIE_SECURE=1 로 켠다.
        secure=os.getenv("CONSOLE_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
        path="/",
    )
    user = auth.account_by_email(body.email)
    assert user is not None  # login() 이 성공했으면 반드시 있다
    logger.info("로그인 — %s", user.user.email)
    return _me(user.user)


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


User = Annotated[ConsoleUser, Depends(current_user)]


def _visible(user: ConsoleUser, rows: list[dict], key: str = "workspace") -> list[dict]:
    """담당자는 자기 워크스페이스만 본다. 필터는 **응답을 만들기 전에** 건다."""
    if user.all_workspaces:
        return rows
    return [r for r in rows if user.may_see(str(r.get(key, "")))]


# ---------------------------------------------------------------------------
# 읽기 엔드포인트
# ---------------------------------------------------------------------------

def _me(user: ConsoleUser) -> dict:
    return {
        "name": user.display(),
        "email": user.email,
        "role": user.role,
        "workspaces": sorted(user.workspaces),
        "allWorkspaces": user.all_workspaces,
    }


@app.get("/api/me")
def me(user: User) -> dict:
    return _me(user)


@app.get("/api/status")
def status(user: User) -> dict:
    """데이터 현황 — 워크스페이스별 수집 상태와 추이."""
    rows = _visible(user, reader.workspace_status(store()), key="key")
    return {"workspaces": rows}


@app.get("/api/usage")
def usage(user: User) -> dict:
    """API 사용량. 담당자에게는 자기 워크스페이스 몫만 보인다.

    범위를 여기서 걸러내지 않고 `reader` 에 넘긴다. 응답을 만든 뒤 목록만 걸러내면
    시간대별·모델별·기준선 같은 합계에 다른 워크스페이스 값이 남는다.
    """
    return reader.usage_snapshot(None if user.all_workspaces else user.workspaces)


@app.get("/api/collected")
def collected(user: User) -> dict:
    """수집 문서 목록. 본문은 담지 않는다."""
    return {"docs": _visible(user, reader.collected_docs(store()))}


@app.get("/api/collected/content")
def collected_content(
    user: User,
    path: Annotated[str, Query(description="아카이브 기준 상대 경로")],
) -> dict:
    """수집 문서 원문.

    관리자에게만 내려보내고, **열람 사실을 기록으로 남긴다.** 이 문서들은 Slack 채널
    구성원만 볼 수 있던 대화라, 콘솔에서 조용히 열리는 경로를 만들면 채널 권한이 무의미해진다.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="대화 원문은 관리자만 열 수 있습니다. 내용 확인이 필요하면 해당 Slack 채널에서 확인해 주세요.",
        )
    try:
        content = reader.read_document(path)
    except reader.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    record_read(user, path)
    return {"path": path, "content": content}


@app.get("/api/collected/audit")
def collected_audit(user: User) -> dict:
    """원문 열람 기록. 관리자만 본다."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자만 볼 수 있습니다.")
    return {"entries": read_audit_entries()}


@app.get("/api/harness")
def harness(user: User) -> dict:
    """봇 규칙 문서 목록과 내용."""
    _require_developer(user)
    return {"files": _visible(user, reader.harness_files())}


def _require_admin(user: ConsoleUser) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자만 환경변수 설정을 볼 수 있습니다.")


def _require_developer(user: ConsoleUser) -> None:
    if not user.may_manage_bot:
        raise HTTPException(status_code=403, detail="개발자 또는 관리자 권한이 필요합니다.")


def _check_write_request(request: Request) -> None:
    """쿠키 인증 쓰기 요청의 CSRF를 검사한다."""
    if request.headers.get("x-tybot-csrf") != "1":
        raise HTTPException(status_code=403, detail="CSRF 확인 헤더가 없습니다.")

    origin = (request.headers.get("origin") or "").rstrip("/")
    same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
    allowed = {
        value.strip().rstrip("/")
        for value in (os.getenv("CONSOLE_ALLOWED_ORIGINS") or "").split(",")
        if value.strip()
    }
    if not origin or (origin != same_origin and origin not in allowed):
        raise HTTPException(status_code=403, detail="허용되지 않은 화면에서 보낸 변경 요청입니다.")


@app.get("/api/env-settings")
def get_env_settings(user: User) -> dict:
    _require_admin(user)
    return env_settings.snapshot()


@app.put("/api/env-settings")
def put_env_settings(
    body: EnvSettingsBody,
    request: Request,
    user: User,
) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        result, changed = env_settings.save(body.model_dump(), actor=user.display())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except OSError as e:
        logger.error("환경변수 설정 저장 실패: %s", e)
        raise HTTPException(status_code=500, detail="환경변수 설정 파일을 저장하지 못했습니다.") from e

    try:
        env_settings.audit_change(user.display(), user.email, changed, "applied")
    except OSError as e:
        logger.error("환경변수 설정 감사 로그 실패: %s", e)
    logger.warning("환경변수 설정 변경 — actor=%s changed=%s", user.email, changed)
    return {**result, "changed": changed}


# ---------------------------------------------------------------------------
# 워크스페이스 관리 — admin 전용, 시크릿 원문은 응답하지 않는다
# ---------------------------------------------------------------------------


def _env_token_keys() -> set[str]:
    """Slack 토큰이 환경변수에 있는 워크스페이스.

    레지스트리 이전에 만든 워크스페이스는 토큰이 /etc/tybot/tybot.env 에 있다.
    그걸 그냥 "미등록" 으로 보이면 **동작 중인 봇이 고장난 것처럼 읽힌다.**
    어디에 있는지를 말한다.
    """
    from ..workspaces import env_suffix

    keys: set[str] = set()
    configured = [
        key.strip().lower()
        for key in (os.getenv("WORKSPACES") or "").split(",")
        if key.strip()
    ]
    for key in configured:
        suffix = env_suffix(key)
        if (
            os.getenv(f"SLACK_BOT_TOKEN_{suffix}", "").strip()
            and os.getenv(f"SLACK_APP_TOKEN_{suffix}", "").strip()
        ):
            keys.add(key)
    if (
        os.getenv("SLACK_BOT_TOKEN", "").strip()
        and os.getenv("SLACK_APP_TOKEN", "").strip()
    ):
        keys.add(os.getenv("PILOT_WORKSPACE", "pilot").lower())
    return keys


def _workspace_response(row: dict, env_keys: set[str] | None = None) -> dict:
    env_keys = env_keys or set()
    in_env = str(row["key"]).lower() in env_keys
    missing = "환경변수 사용" if in_env else "미등록"
    return {
        "key": row["key"],
        "label": row["label"],
        "role": row["role"],
        "state": row["state"],
        "error": row.get("error"),
        "limitUsd": float(row["limit_usd"]),
        "readable": list(row.get("readable") or []),
        "botTokenMask": row.get("bot_token_mask") or missing,
        "appTokenMask": row.get("app_token_mask") or missing,
        "secretUpdatedAt": row.get("secret_updated_at"),
        "secretUpdatedBy": row.get("secret_updated_by") or "-",
        "archivePath": row["archive_path"],
        "createdAt": row["created_at"],
        "createdBy": row["created_by"],
        # DB 토큰이 아직 없고 환경변수 대체 토큰이 있으면 이전 동작을 안내한다.
        "tokenInEnv": in_env and not row.get("bot_token_mask"),
    }


@app.get("/api/workspaces")
def get_workspaces(user: User) -> dict:
    _require_admin(user)
    try:
        rows = workspace_store.list_workspaces()
    except workspace_store.WorkspaceStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    env_keys = _env_token_keys()
    return {"workspaces": [_workspace_response(row, env_keys) for row in rows]}


@app.put("/api/workspaces/{key}")
def put_workspace(key: str, body: WorkspaceBody, request: Request, user: User) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        workspace_store.save_workspace(
            actor=user.email,
            key=key,
            label=body.label,
            role=body.role,
            state=body.state,
            limit_usd=body.limitUsd,
            readable=body.readable,
            bot_token=body.botToken,
            app_token=body.appToken,
        )
        request_restart(user.email, [f"WORKSPACE_REGISTRY:{key.strip().lower()}"])
        rows = workspace_store.list_workspaces()
    except workspace_store.WorkspaceStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except OSError as e:
        logger.exception("워크스페이스 저장 후 재시작 요청 실패")
        raise HTTPException(
            status_code=503,
            detail="워크스페이스는 저장됐지만 봇 재시작을 요청하지 못했습니다.",
        ) from e
    logger.warning("워크스페이스 변경 — actor=%s workspace=%s", user.email, key)
    env_keys = _env_token_keys()
    return {"workspaces": [_workspace_response(row, env_keys) for row in rows], "restartPending": True}


# ---------------------------------------------------------------------------
# 콘솔 사용자 관리 — admin 전용
# ---------------------------------------------------------------------------

@app.get("/api/console-users")
def console_users(user: User) -> dict:
    _require_admin(user)
    try:
        rows = account_store.list_users()
    except account_store.AccountStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"users": rows, "roles": list(ROLES)}


@app.put("/api/console-users")
def put_console_user(
    body: ConsoleAccountBody,
    request: Request,
    user: User,
    auth: Annotated[Authenticator, Depends(authenticator)],
) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        account_store.save_user(
            actor_email=user.email,
            email=body.email,
            name=body.name,
            role=body.role,
            active=body.active,
            workspaces=body.workspaces,
            password=body.password,
        )
        auth.reload()
    except account_store.AccountStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except AuthConfigurationError as e:
        logger.error("콘솔 사용자 저장 후 인증 계정 재조회 실패: %s", e)
        raise HTTPException(
            status_code=503,
            detail="계정은 저장됐지만 현재 프로세스에 다시 읽지 못했습니다. 콘솔을 재시작해 주세요.",
        ) from e
    logger.warning(
        "콘솔 사용자 변경 — actor=%s target=%s role=%s active=%s",
        user.email,
        body.email.strip().lower(),
        body.role,
        body.active,
    )
    return {"ok": True}


class LlmSecretBody(BaseModel):
    provider: str
    key: str = Field(default="", max_length=400)
    enabled: bool = True


@app.get("/api/llm-secrets")
def get_llm_secrets(user: User) -> dict:
    """LLM 키 상태. **가린 값만 나간다 — 복호화해서 돌려주는 길은 없다.**"""
    _require_admin(user)
    try:
        return {"secrets": llm_secret_store.list_secrets()}
    except workspace_store.WorkspaceStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.put("/api/llm-secrets")
def put_llm_secret(body: LlmSecretBody, request: Request, user: User) -> dict:
    """키를 등록·교체하거나 사용을 중지한다.

    삭제는 없다. 지우면 언제 무엇을 쓰고 있었는지가 사라진다.
    """
    _require_admin(user)
    _check_write_request(request)
    try:
        if body.key.strip():
            llm_secret_store.save_secret(body.provider, body.key, actor=user.email)
        else:
            llm_secret_store.set_enabled(body.provider, body.enabled, actor=user.email)
        secrets = llm_secret_store.list_secrets()
    except workspace_store.WorkspaceStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # 키 값은 절대 로그에 남기지 않는다. 무엇을 했는지만 남긴다.
    logger.warning(
        "LLM 키 변경 — actor=%s provider=%s action=%s",
        user.email,
        body.provider,
        "rotate" if body.key.strip() else ("enable" if body.enabled else "disable"),
    )
    return {"secrets": secrets}


@app.get("/api/health-report")
def health_report(user: User) -> dict:
    """헬스 체크 — 봇이 "돌고는 있는데 제 일을 못 하는" 상태를 드러낸다.

    아래 `/api/health` 는 프로세스가 살아 있는지만 답하는 무인증 확인용이고,
    이쪽은 수집·답변 품질·명령 정합·피드백까지 본다. 담당자에게는 자기 워크스페이스
    몫만 보인다 — 범위는 `health.report` 안에서 거른다(합계가 섞이지 않게).
    """
    _require_developer(user)
    return health.report(
        allowed=None if user.all_workspaces else user.workspaces,
        store=store(),
        # 신고 본문에는 업무 내용이 들어 있다. 관리자에게만 보낸다.
        include_text=user.role == "admin",
    )


class FeedbackHandledBody(BaseModel):
    note: str = Field(default="", max_length=500)


@app.put("/api/health-report/feedback/{event_id}/handled")
def mark_feedback_handled(
    event_id: str,
    body: FeedbackHandledBody,
    request: Request,
    user: User,
) -> dict:
    """신고를 처리했다고 표시한다.

    신고가 봇에 반영됐는지 아무도 알 수 없던 것이 문제였다. 정정을 받아도 누가
    무엇을 고쳤는지 남는 곳이 없어, 같은 신고를 두 번 보거나 아무도 안 봤다.

    **신고를 고치거나 지우지 않는다.** 같은 append-only 로그에 한 줄을 더 쌓는다.
    """
    _require_admin(user)
    _check_write_request(request)
    if not re.fullmatch(r"[0-9a-f]{12}", event_id):
        raise HTTPException(status_code=422, detail="피드백 식별자가 올바르지 않습니다.")
    try:
        FeedbackLog(reader.qa_log_dir()).resolve(
            target=event_id,
            actor=user.email,
            note=body.note,
        )
    except OSError as e:
        logger.exception("피드백 처리 표시 실패")
        raise HTTPException(status_code=500, detail="처리 표시를 남기지 못했습니다.") from e
    logger.warning("피드백 처리 표시 — actor=%s event=%s", user.email, event_id)
    return health.report(
        allowed=None if user.all_workspaces else user.workspaces,
        store=store(),
        include_text=user.role == "admin",
    )


@app.get("/api/service-logs")
def service_log_entries(
    user: User,
    level: Annotated[str, Query(pattern="^(info|warning|error)$")] = "info",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    _require_developer(user)
    try:
        entries = service_logs.read(level=level, limit=limit)
    except service_logs.ServiceLogError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"level": level.lower(), "entries": entries}


# ---------------------------------------------------------------------------
# 배치 관리 — admin 전용, 고정된 TYBot 타이머만
# ---------------------------------------------------------------------------

@app.get("/api/timers")
def timers(user: User) -> dict:
    _require_admin(user)
    try:
        return {"timers": timer_manager.snapshot()}
    except timer_manager.TimerManagerError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.put("/api/timers/action")
def timer_action(body: TimerActionBody, request: Request, user: User) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        rows = timer_manager.apply(body.unit, body.action, body.preset)
    except timer_manager.TimerManagerError as e:
        try:
            timer_manager.audit(
                user.display(), user.email, body.unit, body.action, body.preset, "failed"
            )
        except OSError:
            logger.exception("배치 작업 실패 감사 로그 기록 실패")
        raise HTTPException(status_code=422, detail=str(e)) from e
    try:
        timer_manager.audit(
            user.display(), user.email, body.unit, body.action, body.preset, "applied"
        )
    except OSError:
        logger.exception("배치 작업 감사 로그 기록 실패")
    logger.warning(
        "배치 작업 변경 — actor=%s unit=%s action=%s preset=%s",
        user.email,
        body.unit,
        body.action,
        body.preset,
    )
    return {"timers": rows}


# ---------------------------------------------------------------------------
# 배포 관리 — admin은 요청만 만들고 root path 유닛이 update.sh를 실행한다
# ---------------------------------------------------------------------------


@app.get("/api/deployment")
def deployment(user: User) -> dict:
    _require_developer(user)
    return deploy_request.console_status()


@app.put("/api/deployment/request")
def request_deployment(request: Request, user: User) -> dict:
    """관리자의 직접 배포. 승인 절차를 거치지 않는다.

    승인 절차는 **개발자가 올린 코드를 다른 사람이 본다**는 데 뜻이 있다.
    서버에 root 로 들어가 `update.sh` 를 칠 수 있는 관리자에게 같은 절차를
    강제하면, 콘솔을 놔두고 SSH 로 도는 길만 열린다 — 그쪽은 기록도 안 남는다.
    막는 대신 남긴다: 실행자를 배포 상태와 감사 로그에 기록한다.

    개발자(role=developer)는 여전히 요청→승인을 거친다.
    """
    _require_admin(user)
    _check_write_request(request)
    try:
        result = deploy_request.request_deploy(user.email, note="관리자 직접 배포")
    except OSError as e:
        logger.exception("배포 요청 파일 기록 실패")
        raise HTTPException(status_code=500, detail="배포 요청을 기록하지 못했습니다.") from e
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("reason") or "배포 요청 거절"))
    logger.warning("관리자 직접 배포 — actor=%s (승인 절차 없음)", user.email)
    return deploy_request.console_status()


def _deploy_request_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "workspace": row["workspace"],
        "workspaceLabel": row["workspace_label"],
        "requester": row["requester"],
        "requestedAt": row["requested_at"],
        "repo": row["repo"],
        "branch": row["branch"],
        "commit": row["commit_sha"],
        "commitTitle": row["commit_title"],
        "author": row["author"],
        "fastForward": row["fast_forward"],
        "filesChanged": row.get("files") or [],
        "checks": row.get("checks") or [],
        "state": row["state"],
        "approvalExpiresAt": row.get("approval_expires_at"),
        "approver": row.get("approver"),
        "decidedAt": row.get("decided_at"),
    }


@app.get("/api/deploy-requests")
def get_deploy_requests(user: User) -> dict:
    _require_developer(user)
    try:
        rows = deploy_approval_store.list_requests(None if user.all_workspaces else set(user.workspaces))
    except deploy_approval_store.DeployApprovalError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"requests": [_deploy_request_response(row) for row in rows]}


@app.put("/api/deploy-requests")
def create_deploy_request(body: DeployRequestBody, request: Request, user: User) -> dict:
    _require_developer(user)
    _check_write_request(request)
    if not user.all_workspaces and not user.may_see(body.workspace):
        raise HTTPException(status_code=403, detail="담당 워크스페이스만 배포를 요청할 수 있습니다.")
    try:
        request_id = deploy_approval_store.create_request(
            workspace=body.workspace,
            requester=user.email,
            reason=body.reason,
        )
        rows = deploy_approval_store.list_requests(
            None if user.all_workspaces else set(user.workspaces)
        )
    except deploy_approval_store.DeployApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.warning(
        "배포 승인 요청 등록 — actor=%s workspace=%s request=%s",
        user.email,
        body.workspace,
        request_id,
    )
    return {"requests": [_deploy_request_response(row) for row in rows]}


@app.put("/api/deploy-requests/{request_id}/decision")
def decide_deploy_request(
    request_id: int,
    body: DeployDecisionBody,
    request: Request,
    user: User,
) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        decision = deploy_approval_store.decide_request(
            request_id=request_id,
            approver=user.email,
            decision=body.decision,
            note=body.note,
        )
        if decision["approved"]:
            result = deploy_request.request_deploy(
                user.email,
                note=f"승인된 배포 요청 #{request_id}",
                approval_id=request_id,
            )
            if not result.get("ok"):
                deploy_approval_store.restore_awaiting(request_id)
                raise deploy_approval_store.DeployApprovalError(
                    str(result.get("reason") or "배포 실행 요청을 만들지 못했습니다.")
                )
        rows = deploy_approval_store.list_requests(None)
    except deploy_approval_store.DeployApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.warning(
        "배포 요청 결정 — actor=%s request=%s decision=%s",
        user.email,
        request_id,
        body.decision,
    )
    return {"requests": [_deploy_request_response(row) for row in rows]}


@app.get("/api/health")
def health_probe() -> dict:
    """인증 없이 열어 두는 확인용 엔드포인트. 상태만 알려 주고 내용은 담지 않는다."""
    return {"ok": True, "at": datetime.now(KST).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# Slack 앱 매니페스트 — 저장소 파일을 그대로 내려보낸다
# ---------------------------------------------------------------------------

# 경로는 `reader` 가 갖는다. 이 모듈을 import 하면 FastAPI 가 따라오는데,
# 헬스 체크는 콘솔이 없는 서버에서도 매니페스트를 읽어야 한다.
manifest_path = reader.manifest_path


@app.get("/api/manifest")
def manifest(user: User) -> dict:
    """설치 안내 화면이 보여줄 매니페스트를 **파일에서 직접** 읽어 준다.

    예전에는 같은 내용이 화면 코드(`SetupGuide.tsx`)에 상수로 박혀 있었다. 기능을 더해
    스코프나 이벤트가 늘 때마다 두 곳을 함께 고쳐야 했고, 한쪽만 고치면 **이 화면을 보고
    만든 앱에 권한이 빠져 봇이 오류 없이 반쪽만 동작했다.** 파일 하나를 진실로 삼는다.

    내용에 시크릿은 없다(설치 템플릿). 그래도 스코프 구성이 드러나므로 로그인은 요구한다.
    """
    _require_developer(user)
    path = manifest_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("매니페스트를 읽지 못했습니다: %s (%s)", path, e)
        raise HTTPException(
            status_code=503,
            detail=(
                "매니페스트 파일을 읽지 못했습니다. 저장소의 "
                "docs/pilot/slack-app-manifest.yaml 이 배포본에 포함됐는지 확인하세요."
            ),
        ) from e

    stat = path.stat()
    return {
        "content": content,
        "path": str(path),
        # 화면에서 '언제 것인지' 를 보여주면, 배포가 안 된 상태를 사람이 바로 알아챈다.
        "updated_at": datetime.fromtimestamp(stat.st_mtime, KST).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()[:12],
    }


# ---------------------------------------------------------------------------
# 원문 열람 기록 — 콘솔에서 지울 수 없어야 한다
# ---------------------------------------------------------------------------

def audit_path() -> Path:
    return reader.qa_log_dir() / "archive-read.jsonl"


def record_read(user: ConsoleUser, path: str) -> None:
    """append only. 기록 실패는 로그로 남기되 열람을 막지는 않는다."""
    entry = {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "actor": user.display(),
        "email": user.email,
        "path": path,
    }
    try:
        target = audit_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("원문 열람 기록 실패 (%s): %s", path, e)
    logger.info("원문 열람 — %s (%s) %s", user.display(), user.email, path)


def read_audit_entries(limit: int = 100) -> list[dict]:
    try:
        lines = audit_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 정적 파일 — 빌드된 콘솔 화면을 같은 프로세스에서 서빙한다(선택)
# ---------------------------------------------------------------------------

def mount_frontend() -> None:
    """`CONSOLE_DIST` 가 가리키는 폴더를 `/` 에 붙인다.

    별도 웹서버를 두지 않아도 되게 하는 편의 기능이다. 값이 없으면 API 만 돈다.
    """
    dist = os.getenv("CONSOLE_DIST")
    if not dist:
        return
    root = Path(dist)
    if not (root / "index.html").exists():
        logger.warning("CONSOLE_DIST 에 index.html 이 없습니다: %s", root)
        return

    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(root), html=True), name="console")
    logger.info("콘솔 화면을 함께 서빙합니다: %s", root)


@app.exception_handler(AuthError)
def _auth_error(_request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": str(exc)})


mount_frontend()
