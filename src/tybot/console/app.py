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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import deploy_request, heartbeat
from ..archive.store import ArchiveStore
from ..feedback import FeedbackLog
from ..managed_env import request_restart
from . import (
    account_store,
    audit_store,
    deploy_approval_store,
    env_settings,
    health,
    llm_secret_store,
    reader,
    service_logs,
    specialist_git,
    specialist_runtime_store,
    specialist_source,
    specialist_store,
    specialist_zip,
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
    defaultModel: str | None = None


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


class SpecialistProposalBody(BaseModel):
    key: str
    name: str
    domain: str
    adapter: str
    state: Literal["draft", "enabled", "disabled"] = "draft"
    version: str = ""
    contractVersion: str = "v1"
    workspaces: list[str] = Field(default_factory=list)
    # 이 전문가가 쓸 모델. 비면 게이트웨이 기본값 —
    # 간단한 분야에 무거운 모델을 쓸 이유가 없다.
    model: str = Field(default="", max_length=64)
    # 라우터가 읽는 설명. 질문마다 프롬프트에 실리므로 짧게 묶는다.
    routingHint: str = Field(default="", max_length=300)
    # 이 전문가를 부를 최소 신뢰도. 오답의 값이 분야마다 다르다.
    minConfidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # 답변 규칙. 비면 저장소의 프롬프트 파일을 쓴다.
    rules: str = Field(default="", max_length=8000)
    repositoryUrl: str = Field(default="", max_length=300)
    releaseRef: str = Field(default="", max_length=100)
    sourceCommit: str = Field(default="", max_length=40)
    artifactHashes: dict[str, str] = Field(default_factory=dict)
    sourceType: Literal["manual", "git", "zip"] = "manual"
    sourceName: str = Field(default="", max_length=150)
    bundleSha256: str = Field(default="", max_length=64)
    uploadReceipt: str = Field(default="", max_length=200)


class SpecialistImportBody(BaseModel):
    repositoryUrl: str = Field(min_length=1, max_length=300)
    release: str = Field(default="latest", min_length=1, max_length=100)


class SpecialistDecisionBody(BaseModel):
    note: str = Field(default="", max_length=500)


def _audit_event(**kwargs) -> None:
    """Record an audit event without exposing operational writes to DB rollout races."""
    try:
        audit_store.record(**kwargs)
    except audit_store.AuditStoreError:
        logger.exception("통합 감사 기록 실패")


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
    """Workspace collection and today's answer health."""
    rows = _workspace_runtime_status(user)
    return {"workspaces": rows}


@app.get("/api/usage")
def usage(
    user: User,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """API 사용량. 담당자에게는 자기 워크스페이스 몫만 보인다.

    범위를 여기서 걸러내지 않고 `reader` 에 넘긴다. 응답을 만든 뒤 목록만 걸러내면
    시간대별·모델별·기준선 같은 합계에 다른 워크스페이스 값이 남는다.
    """
    period_start, period_end = _report_period(start, end)
    return reader.usage_snapshot(
        None if user.all_workspaces else user.workspaces,
        start_date=period_start,
        end_date=period_end,
    )


@app.get("/api/capabilities")
def capabilities(user: User) -> dict:
    """Features are advertised only when their backend contract exists."""
    return {
        "specialists": user.may_manage_bot and specialist_store.is_ready(),
        "approvedSummaries": False,
        "summaryReview": False,
    }


def _scoped_health(user: ConsoleUser, *, include_text: bool = False) -> dict:
    return health.report(
        allowed=None if user.all_workspaces else user.workspaces,
        store=store(),
        include_text=include_text and user.is_admin,
    )


def _report_period(start: date | None, end: date | None) -> tuple[date, date]:
    today = reader._now().date()
    period_start = start or today
    period_end = end or today
    if period_start > period_end:
        raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if period_end > today:
        raise HTTPException(status_code=422, detail="미래 날짜는 조회할 수 없습니다.")
    if (period_end - period_start).days >= 366:
        raise HTTPException(status_code=422, detail="조회 기간은 최대 366일입니다.")
    return period_start, period_end


def _workspace_runtime_status(user: ConsoleUser) -> list[dict]:
    """Collection and today's answer health, scoped before aggregation."""
    workspaces = _visible(user, reader.workspace_status(store()), key="key")
    visible_keys = {str(row["key"]) for row in workspaces}
    today = reader._now().date().isoformat()
    by_workspace: dict[str, list[dict]] = {key: [] for key in visible_keys}
    for record in reader._read_qa_records(1):
        key = str(record.get("workspace") or "")
        if key in visible_keys and str(record.get("ts") or "")[:10] == today:
            by_workspace[key].append(record)

    enriched: list[dict] = []
    for workspace in workspaces:
        records = by_workspace[str(workspace["key"])]
        errors = sum(1 for row in records if str(row.get("error") or "").strip())
        no_hits = sum(1 for row in records if int(row.get("hits") or 0) == 0)
        slow = sum(1 for row in records if int(row.get("elapsed_ms") or 0) > health.SLOW_MS)
        if not records:
            answer_health = "unknown"
            error_health = "unknown"
        elif errors:
            answer_health = "bad"
            error_health = "bad"
        else:
            answer_health = "watch" if no_hits or slow else "ok"
            error_health = "ok"
        enriched.append({
            **workspace,
            "answersToday": len(records),
            "answerErrorsToday": errors,
            "noHitAnswersToday": no_hits,
            "slowAnswersToday": slow,
            "lastAnsweredAt": max(
                (str(row.get("ts") or "") for row in records),
                default=None,
            ),
            "answerHealth": answer_health,
            "errorHealth": error_health,
        })
    return enriched


@app.get("/api/dashboards/collection")
def collection_dashboard(user: User) -> dict:
    workspaces = _workspace_runtime_status(user)
    return {
        "documents": sum(int(row.get("docs") or 0) for row in workspaces),
        "rawLines": sum(int(row.get("rawLines") or 0) for row in workspaces),
        "stalled": [row for row in workspaces if row.get("health") == "stalled"],
        "brokenDocuments": sum(int(row.get("brokenDocs") or 0) for row in workspaces),
        "uninvitedChannels": sum(int(row.get("uninvitedChannels") or 0) for row in workspaces),
        "workspaces": workspaces,
        "summaryReview": {"available": False, "pending": 0},
    }


@app.get("/api/dashboards/answers")
def answers_dashboard(
    user: User,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    period_start, period_end = _report_period(start, end)
    usage_data = reader.usage_snapshot(
        None if user.all_workspaces else user.workspaces,
        start_date=period_start,
        end_date=period_end,
    )
    report = _scoped_health(user)
    answers = usage_data["answerSummary"]
    feedback = report["sections"]["feedback"]
    specialist_calls: list[dict] = []
    with contextlib.suppress(specialist_store.SpecialistStoreError):
        specialist_calls = specialist_store.list_calls(
            allowed=None if user.all_workspaces else user.workspaces,
            limit=500,
        )
        specialist_calls = [
            row for row in specialist_calls
            if period_start.isoformat() <= str(row.get("at") or "")[:10]
            <= period_end.isoformat()
        ]
    return {
        "today": usage_data["asOf"][:10],
        "periodStart": usage_data["periodStart"],
        "periodEnd": usage_data["periodEnd"],
        "isToday": usage_data["isToday"],
        "calls": usage_data["calls"],
        "callsToday": usage_data["callsToday"],
        "spentUsd": usage_data["spentUsd"],
        "limitUsd": usage_data["limitUsd"],
        "answers": {key: value for key, value in answers.items() if key != "problems"},
        "feedback": {
            key: value for key, value in feedback.items()
            if key not in {"items", "contributors", "problems"}
        },
        "specialists": {
            "calls": len(specialist_calls),
            "success": sum(1 for row in specialist_calls if row.get("result") == "success"),
            "fallback": sum(1 for row in specialist_calls if row.get("result") == "fallback"),
        },
    }


@app.get("/api/dashboards/operations")
def operations_dashboard(user: User) -> dict:
    _require_developer(user)
    report = _scoped_health(user)
    specialist_errors = 0
    with contextlib.suppress(specialist_store.SpecialistStoreError):
        specialist_errors = sum(
            1 for row in specialist_store.list_specialists()
            if row.get("health") == "error" or row.get("state") == "error"
        )
    timer_rows = []
    if user.is_admin:
        with contextlib.suppress(timer_manager.TimerManagerError):
            timer_rows = timer_manager.snapshot()
    deployment_state = deploy_request.console_status()
    return {
        "slack": report["sections"]["bot"],
        "commands": report["sections"]["commands"],
        "disabledTimers": sum(1 for row in timer_rows if not row.get("enabled")),
        "deployment": deployment_state,
        "specialistErrors": specialist_errors,
    }


@app.get("/api/dashboards/console")
def console_dashboard(user: User) -> dict:
    _require_admin(user)
    try:
        users = account_store.list_users()
    except account_store.AccountStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        requests = specialist_store.list_requests()
    except specialist_store.SpecialistStoreError:
        requests = []
    events = audit_store.list_events(qa_log_dir=reader.qa_log_dir(), limit=20)
    return {
        "users": len(users),
        "admins": sum(1 for row in users if row.get("role") == "admin" and row.get("active")),
        "pendingApprovals": sum(1 for row in requests if row.get("state") == "awaiting_approval"),
        "recentAudit": events[:10],
    }


@app.get("/api/questions")
def questions(
    user: User,
    workspace: str = "",
    result: str = "",
    start: date | None = None,
    end: date | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    _require_developer(user)
    period_start, period_end = _report_period(start, end)
    rows = reader.usage_snapshot(
        None if user.all_workspaces else user.workspaces,
        start_date=period_start,
        end_date=period_end,
    )["recent"]
    if workspace:
        rows = [row for row in rows if row.get("workspace") == workspace]
    if result:
        rows = [
            row for row in rows
            if row.get("reason") == result or row.get("intent") == result
        ]
    return {
        "today": reader._now().date().isoformat(),
        "periodStart": period_start.isoformat(),
        "periodEnd": period_end.isoformat(),
        "questions": rows[:limit],
    }


@app.get("/api/diagnostics/archive")
def archive_diagnostics(user: User) -> dict:
    from ..attachment_review import failures, public_failure_reason

    report = _scoped_health(user)
    failed = failures(reader.archive_dir())
    if not user.all_workspaces:
        failed = [item for item in failed if item.workspace in user.workspaces]
    failed.sort(key=lambda item: item.staged_at, reverse=True)
    section = dict(report["sections"]["archive"])
    section["attachmentFailures"] = len(failed)
    section["failedAttachments"] = [
        {
            "workspace": item.workspace,
            "channelId": item.channel_id,
            "name": item.name,
            "filetype": item.filetype,
            "reason": public_failure_reason(item),
            "permalink": item.permalink,
            "stagedAt": item.staged_at,
        }
        for item in failed[:200]
    ]
    return {"checkedAt": report["checkedAt"], "section": section}


@app.get("/api/diagnostics/answers")
def answer_diagnostics(user: User) -> dict:
    _require_developer(user)
    report = _scoped_health(user)
    return {"checkedAt": report["checkedAt"], "section": report["sections"]["answers"]}


@app.get("/api/diagnostics/answers/evidence")
def answer_diagnostic_evidence(
    user: User,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    """품질 집계의 원인이 된 질문 문장과 출처를 관리자에게만 보여준다.

    질문 본문은 업무 내용을 포함하므로 일반 품질 API에는 절대 섞지 않는다. 관리자가 명시적으로
    상세 보기를 눌렀을 때만 내려주고, 열람 사실은 본문 없이 감사 기록에 남긴다.
    """
    _require_admin(user)
    rows = reader._read_qa_records(7)
    if not user.all_workspaces:
        rows = [row for row in rows if str(row.get("workspace", "")) in user.workspaces]
    rows.sort(key=lambda row: str(row.get("ts", "")), reverse=True)
    notable = [
        row for row in rows
        if str(row.get("error") or "").strip()
        or int(row.get("hits") or 0) == 0
        or int(row.get("elapsed_ms") or 0) > health.SLOW_MS
    ][:limit]
    _audit_event(
        actor=user.email,
        category="answer-quality",
        action="read-evidence",
        target_type="qa-records",
        target_id="recent-notable",
        outcome="succeeded",
        metadata={"count": len(notable)},
    )
    return {
        "items": [
            {
                "at": row.get("ts", ""),
                "workspace": row.get("workspace", ""),
                "question": row.get("question", ""),
                  "reason": (
                      "error"
                      if str(row.get("error") or "").strip()
                      else "no_hits"
                      if int(row.get("hits") or 0) == 0
                      else "slow"
                  ),
                "hits": int(row.get("hits") or 0),
                "citations": list(row.get("citations") or []),
                "model": row.get("model") or "-",
                "elapsedMs": int(row.get("elapsed_ms") or 0),
                "error": row.get("error") or "",
            }
            for row in notable
        ]
    }


@app.get("/api/diagnostics/slack")
def slack_diagnostics(user: User) -> dict:
    _require_developer(user)
    report = _scoped_health(user)
    return {
        "checkedAt": report["checkedAt"],
        "bot": report["sections"]["bot"],
        "commands": report["sections"]["commands"],
    }


@app.get("/api/diagnostics/commands")
def command_diagnostics(user: User) -> dict:
    """Command registration diagnostics without duplicated workspace health."""
    _require_developer(user)
    report = _scoped_health(user)
    return {
        "checkedAt": report["checkedAt"],
        "section": report["sections"]["commands"],
    }


@app.get("/api/feedback")
def feedback_report(user: User) -> dict:
    _require_developer(user)
    report = _scoped_health(user, include_text=True)
    return {"checkedAt": report["checkedAt"], "section": report["sections"]["feedback"]}


def _deployed_prompt_version(adapter: str) -> str:
    """지금 서버에 깔린 계약 파일의 버전. 못 읽으면 빈 문자열.

    읽기 실패가 화면을 막지 않는다 — 버전 하나 때문에 전문 봇 목록이 통째로
    안 뜨면 그게 더 나쁘다.
    """
    try:
        from ..specialist_adapters import prompt_version

        return prompt_version(adapter)
    except Exception:  # noqa: BLE001 - 표시값 하나가 화면을 막지 않는다
        return ""


def _specialist_response(row: dict) -> dict:
    return {
        "key": row["key"],
        "name": row["name"],
        "domain": row["domain"],
        "adapter": row["adapter"],
        "adapterAvailable": bool(row.get("adapterAvailable")),
        "state": row["state"],
        "version": row.get("version") or "",
        # **승인 버전과 실제 배포된 계약 버전을 나란히 보인다.**
        #
        # `specialist_bot.version` 은 「승인한 것」 이고 `deployedVersion` 은
        # 「지금 파일에 있는 것」 이다. 둘이 갈리면 승인 밖에서 프롬프트가 바뀐
        # 것이고, 그건 오류 없이 지나간다 — 설계가 나란히 보이라고 한 이유다
        # (specialist-deployment.md §버전은 응답과 승인 기록을 대조한다).
        #
        # `prompt_version()` 은 만들어 두고 아무도 안 읽던 함수다. 만들고 안
        # 잇는 것이 우리가 가장 자주 겪은 고장이다.
        "deployedVersion": _deployed_prompt_version(str(row.get("adapter") or "")),
        "contractVersion": row.get("contract_version") or "v1",
        "health": row.get("health") or "unknown",
        "errorCode": row.get("error_code") or "",
        "lastCheckedAt": row.get("last_checked_at"),
        "workspaces": list(row.get("workspaces") or []),
        "model": row.get("model") or "",
        "routingHint": row.get("routing_hint") or "",
        "minConfidence": float(row.get("min_confidence") or 0.6),
        # 규칙 본문은 목록에 담지 않는다 — 8000자가 표마다 실리면 화면이 무거워진다.
        # 편집 화면이 상세 조회로 따로 받는다.
        "hasRules": bool((row.get("rules") or "").strip()),
        "rulesVersion": int(row.get("rules_version") or 0),
        "repositoryUrl": row.get("repository_url") or "",
        "releaseRef": row.get("release_ref") or "",
        "sourceCommit": row.get("source_commit") or "",
        "artifactHashes": dict(row.get("artifact_hashes") or {}),
        "sourceType": row.get("source_type") or "manual",
        "sourceName": row.get("source_name") or "",
        "bundleSha256": row.get("bundle_sha256") or "",
        "updatedAt": row.get("updated_at"),
        "updatedBy": row.get("updated_by") or "-",
    }


def _specialist_request_response(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "specialist": row["specialist"],
        "proposal": row.get("proposal") or {},
        "checks": row.get("checks") or [],
        "requester": row["requester"],
        "requestedAt": row["requested_at"],
        "state": row["state"],
        "approver": row.get("approver"),
        "decidedAt": row.get("decided_at"),
        "note": row.get("note") or "",
    }


def _visible_specialists(user: ConsoleUser, rows: list[dict]) -> list[dict]:
    if user.all_workspaces:
        return rows
    return [row for row in rows if set(row.get("workspaces") or []) & set(user.workspaces)]


def _visible_specialist_requests(user: ConsoleUser, rows: list[dict]) -> list[dict]:
    if user.all_workspaces:
        return rows
    return [
        row for row in rows
        if set((row.get("proposal") or {}).get("workspaces") or []) & set(user.workspaces)
    ]


@app.get("/api/models")
def models(user: User) -> dict:
    """쓸 수 있는 모델. **레지스트리가 사실이다.**

    화면이 목록을 따로 들고 있으면, 레지스트리에 없는 모델을 고를 수 있게 되고
    그건 저장할 때가 아니라 **질문할 때** 실패한다(UnknownModel → 마스터 폴백).
    실패가 조용해서 "전문가가 왜 안 답하나" 로만 보인다.

    프로바이더 키가 없는 모델은 `usable=false` 로 표시한다 — 목록에서 빼지 않는다.
    빼 버리면 "왜 안 보이나" 를 알 길이 없다.
    """
    _require_developer(user)
    from ..gateway.router import DEFAULT_REGISTRY

    configured = set()
    try:
        for row in llm_secret_store.list_secrets():
            if row.get("enabled") or row.get("inEnv"):
                configured.add(str(row["provider"]))
    except workspace_store.WorkspaceStoreError:
        # 키 상태를 못 읽어도 목록은 보여야 한다.
        configured = set()

    out = []
    seen: set[str] = set()
    for key, spec in DEFAULT_REGISTRY.items():
        # 옛 표기(날짜 꼬리)는 같은 모델을 가리키므로 한 번만 보인다.
        if spec.model in seen:
            continue
        seen.add(spec.model)
        out.append({
            "model": key,
            "provider": spec.provider,
            "inputPer1M": spec.input_price_per_mtok,
            "outputPer1M": spec.output_price_per_mtok,
            "maxSensitivity": spec.max_sensitivity.value,
            "usable": spec.provider in configured,
        })
    out.sort(key=lambda row: (row["provider"], row["inputPer1M"]))
    return {"models": out}


@app.get("/api/specialists")
def specialists(user: User) -> dict:
    _require_developer(user)
    try:
        rows = specialist_store.list_specialists()
        requests = specialist_store.list_requests()
    except specialist_store.SpecialistStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "specialists": [_specialist_response(row) for row in _visible_specialists(user, rows)],
        "requests": [
            _specialist_request_response(row)
            for row in _visible_specialist_requests(user, requests)
        ],
        "adapters": specialist_store.adapters(),
    }


@app.get("/api/specialists/{key}")
def specialist(key: str, user: User) -> dict:
    _require_developer(user)
    try:
        row = specialist_store.get_specialist(key)
    except specialist_store.SpecialistStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="전문 봇을 찾을 수 없습니다.")
    if not _visible_specialists(user, [row]):
        raise HTTPException(status_code=403, detail="이 전문 봇을 볼 권한이 없습니다.")
    # 상세 조회에서만 규칙 본문을 준다. 편집 화면이 이것을 받아 고친다.
    detail = _specialist_response(row)
    detail["rules"] = row.get("rules") or ""
    return detail


@app.post("/api/specialists/import-preview")
def import_specialist_release(
    body: SpecialistImportBody,
    request: Request,
    user: User,
) -> dict:
    """Read and validate an immutable prompt contract without executing repository code."""
    _require_developer(user)
    _check_write_request(request)
    try:
        imported = specialist_git.import_release(body.repositoryUrl, body.release)
    except specialist_git.SpecialistGitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if imported["adapter"] not in specialist_store.ALLOWED_ADAPTERS:
        raise HTTPException(
            status_code=422,
            detail="TYBot 코드에 등록되지 않은 전문 봇 어댑터입니다.",
        )
    _audit_event(
        actor=user.email,
        category="specialist",
        action="release-preview",
        target_type="specialist",
        target_id=imported["key"],
        outcome="succeeded",
        metadata={
            "repository": imported["repositoryUrl"],
            "release": imported["releaseRef"],
            "commit": imported["sourceCommit"],
        },
    )
    return imported


@app.post("/api/specialists/import-upload")
async def import_specialist_upload(
    request: Request,
    user: User,
    filename: Annotated[str | None, Header(alias="X-TYBot-Filename")] = None,
) -> dict:
    """Validate a small contract-only ZIP without extracting it to the filesystem."""
    _require_developer(user)
    _check_write_request(request)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
        raise HTTPException(status_code=415, detail="application/zip 파일만 업로드할 수 있습니다.")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > specialist_zip.MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="계약 ZIP은 2MB를 넘을 수 없습니다.")
        content.extend(chunk)
    try:
        imported = specialist_zip.import_bundle(bytes(content), filename or "")
    except specialist_zip.SpecialistZipError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if imported["adapter"] not in specialist_store.ALLOWED_ADAPTERS:
        raise HTTPException(
            status_code=422,
            detail="TYBot 코드에 등록되지 않은 전문 봇 어댑터입니다.",
        )
    imported["uploadReceipt"] = specialist_zip.issue_receipt(imported, user.email)
    _audit_event(
        actor=user.email,
        category="specialist",
        action="bundle-preview",
        target_type="specialist",
        target_id=imported["key"],
        outcome="succeeded",
        metadata={"filename": imported["sourceName"], "sha256": imported["bundleSha256"]},
    )
    return imported


@app.get("/api/specialist-calls")
def specialist_calls(
    user: User,
    specialist: str = "",
    result: str = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    _require_developer(user)
    try:
        rows = specialist_store.list_calls(
            allowed=None if user.all_workspaces else user.workspaces,
            specialist=specialist.strip().lower(),
            result=result.strip().lower(),
            limit=limit,
        )
    except specialist_store.SpecialistStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "calls": [
            {
                "id": str(row["id"]), "at": row["at"], "workspace": row["workspace"],
                "specialist": row["specialist"], "routingReason": row["routing_reason"],
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                "result": row["result"], "elapsedMs": row["elapsed_ms"],
                "costUsd": float(row["cost_usd"]), "errorCode": row["error_code"],
            }
            for row in rows
        ]
    }


@app.post("/api/specialists/requests")
def create_specialist_request(
    body: SpecialistProposalBody,
    request: Request,
    user: User,
) -> dict:
    _require_developer(user)
    _check_write_request(request)
    requested_workspaces = set(body.workspaces)
    if not user.all_workspaces and (
        not requested_workspaces or not requested_workspaces <= set(user.workspaces)
    ):
        raise HTTPException(
            status_code=403,
            detail="담당 워크스페이스 범위의 전문 봇만 요청할 수 있습니다.",
        )
    proposal = body.model_dump()
    if body.sourceType == "zip":
        if not specialist_zip.verify_receipt(proposal, user.email, body.uploadReceipt):
            raise HTTPException(
                status_code=422,
                detail="ZIP 검증 영수증이 만료됐거나 내용이 변경됐습니다. 파일을 다시 올리세요.",
            )
    elif body.repositoryUrl:
        try:
            imported = specialist_git.import_release(body.repositoryUrl, body.releaseRef)
        except specialist_git.SpecialistGitError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        protected = (
            "repositoryUrl", "releaseRef", "sourceCommit", "artifactHashes",
            "sourceType", "sourceName", "bundleSha256", "key", "name", "domain",
            "adapter", "version", "contractVersion", "rules",
        )
        if any(proposal.get(field) != imported.get(field) for field in protected):
            raise HTTPException(
                status_code=422,
                detail="가져온 릴리스가 미리보기와 다릅니다. 다시 가져온 뒤 요청하세요.",
            )
        proposal.update({field: imported[field] for field in protected})
    proposal.pop("uploadReceipt", None)
    try:
        request_id = specialist_store.create_request(actor=user.email, proposal=proposal)
        rows = specialist_store.list_requests()
    except specialist_store.SpecialistStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist", action="change-request",
        target_type="specialist", target_id=body.key, outcome="requested",
        metadata={"requestId": request_id, "state": body.state, "adapter": body.adapter},
    )
    return {
        "requests": [
            _specialist_request_response(row)
            for row in _visible_specialist_requests(user, rows)
        ]
    }


@app.post("/api/specialists/requests/{request_id}/{decision}")
def decide_specialist_request(
    request_id: int,
    decision: Literal["approve", "reject"],
    body: SpecialistDecisionBody,
    request: Request,
    user: User,
) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        specialist_store.decide_request(
            request_id=request_id,
            actor=user.email,
            decision=decision,
            note=body.note,
            # 전문 봇 개발자와 승인 관리자는 역할이 분리되어 있다. 과거 요청까지
            # 포함해 요청자가 자신의 변경을 승인하는 경로를 열지 않는다.
            allow_self=False,
        )
        rows = specialist_store.list_specialists()
        requests = specialist_store.list_requests()
    except specialist_store.SpecialistStoreError as exc:
        _audit_event(
            actor=user.email, category="specialist", action=decision,
            target_type="specialist-request", target_id=str(request_id), outcome="failed",
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist", action=decision,
        target_type="specialist-request", target_id=str(request_id), outcome="succeeded",
    )
    return {
        "specialists": [_specialist_response(row) for row in rows],
        "requests": [_specialist_request_response(row) for row in requests],
        "adapters": specialist_store.adapters(),
    }


# ===========================================================================
# 전문 봇 2단계 — 실행형 런타임
# ===========================================================================
#
# 설계: docs/design/specialist-runtime-v2.md §콘솔 API와 권한
#
# **1단계 API 와 경로를 분리한다.** `/api/specialists/*` 는 프롬프트 계약이고
# 여기는 격리 실행이다. 상한도 검사도 다르므로, 같은 경로에 얹으면 한쪽 완화가
# 다른 쪽을 조용히 넓힌다.
#
# **콘솔은 아무것도 실행하지 않는다.** Podman 을 부르지 않고 systemd 를 만지지
# 않는다. 여기서 하는 일은 DB 에 상태를 남기는 것까지고, 실제 빌드·기동은 고정된
# root helper 가 한다(`deploy/tybot-specialist-build`, `-deploy`).


class RuntimeGitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=2, max_length=32)
    repositoryUrl: str = Field(min_length=1, max_length=300)
    releaseRef: str = Field(min_length=1, max_length=100)


class RuntimeNoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


def _require_runtime_ready() -> None:
    if not specialist_runtime_store.is_ready():
        raise HTTPException(
            status_code=503,
            detail="실행형 런타임 스키마가 아직 적용되지 않았습니다"
                   "(deploy/sql/specialist_runtime_schema.sql).",
        )


def _runtime_scope(user: ConsoleUser, key: str) -> None:
    """제출·승인·조회 **모든 단계**에서 워크스페이스 범위를 다시 본다.

    한 번만 보면, 범위 밖 전문가의 배포를 나중에 활성화·롤백할 수 있다.
    """
    if user.all_workspaces:
        return
    try:
        row = specialist_store.get_specialist(key)
    except specialist_store.SpecialistStoreError:
        row = None
    allowed = set(user.workspaces)
    scope = set((row or {}).get("workspaces") or [])
    if not scope or not scope <= allowed:
        raise HTTPException(
            status_code=403, detail="담당 워크스페이스의 전문 봇만 다룰 수 있습니다."
        )


@app.get("/api/specialist-runtime")
def specialist_runtime_overview(user: User, key: str = "") -> dict:
    """실행형 현황. 게스트는 상태와 비민감 버전만 본다."""
    if not user.may_manage_bot:
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    _require_runtime_ready()
    specialist = key.strip()
    if specialist:
        _runtime_scope(user, specialist)
    try:
        sources = specialist_runtime_store.list_sources(specialist)
        deployments = specialist_runtime_store.list_deployments(specialist)
    except specialist_runtime_store.RuntimeStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"sources": sources, "deployments": deployments}


@app.post("/api/specialist-runtime/sources/git")
def submit_runtime_git_source(
    body: RuntimeGitBody, request: Request, user: User
) -> dict:
    """공개 Git 태그를 제출한다. **체크아웃하지 않는다** — 형식만 본다."""
    _require_developer(user)
    _check_write_request(request)
    _require_runtime_ready()
    _runtime_scope(user, body.key)
    try:
        url, ref = specialist_source.validate_git_source(
            body.repositoryUrl, body.releaseRef
        )
        source_id = specialist_runtime_store.create_source(
            specialist=body.key, source_type="git", submitted_by=user.email,
            repository_url=url, release_ref=ref,
        )
    except (specialist_source.SourceError,
            specialist_runtime_store.RuntimeStoreError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist-runtime", action="submit-git",
        target_type="specialist-source", target_id=str(source_id), outcome="requested",
        metadata={"specialist": body.key, "releaseRef": ref},
    )
    return {"sourceId": source_id, "repositoryUrl": url, "releaseRef": ref}


@app.post("/api/specialist-runtime/sources/upload")
async def submit_runtime_zip_source(
    request: Request,
    user: User,
    key: Annotated[str, Header(alias="X-TYBot-Specialist")],
    filename: Annotated[str | None, Header(alias="X-TYBot-Filename")] = None,
) -> dict:
    """소스 ZIP 을 검역에 넣는다. **압축을 풀지 않는다.**

    콘솔이 풀면 경로 탈출·심볼릭 링크·압축 폭탄이 콘솔이 쓸 수 있는 모든 곳에
    닿는다. 여기서는 색인만 읽고 원본 바이트를 UUID 이름으로 저장한다.
    """
    _require_developer(user)
    _check_write_request(request)
    _require_runtime_ready()
    _runtime_scope(user, key)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
        raise HTTPException(status_code=415, detail="application/zip 만 업로드할 수 있습니다.")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > specialist_source.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"소스 ZIP 은 "
                       f"{specialist_source.MAX_UPLOAD_BYTES // (1024 * 1024)}MB 를 "
                       "넘을 수 없습니다.",
            )
        content.extend(chunk)
    try:
        bundle = specialist_source.inspect_zip(bytes(content), filename or "")
        if bundle.manifest.key != key:
            raise specialist_source.SourceError(
                f"매니페스트 key({bundle.manifest.key})가 요청과 다릅니다."
            )
        # 검역은 `/var/lib/tybot` 아래다. **콘솔 작업 디렉터리에 두지 않는다** —
        # 배포가 그 경로를 지우거나 덮을 수 있고, 그러면 제출물이 조용히 사라진다.
        quarantine_key = specialist_source.store(bytes(content), heartbeat.state_dir())
        source_id = specialist_runtime_store.create_source(
            specialist=key, source_type="zip", submitted_by=user.email,
            source_name=filename or "", bundle_sha256=bundle.bundle_sha256,
            quarantine_key=quarantine_key,
        )
    except (specialist_source.SourceError,
            specialist_runtime_store.RuntimeStoreError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist-runtime", action="submit-zip",
        target_type="specialist-source", target_id=str(source_id), outcome="requested",
        # **파일 내용도 검역 경로도 남기지 않는다.** 해시와 수치뿐이다.
        metadata={
            "specialist": key, "sha256": bundle.bundle_sha256,
            "entries": bundle.entries, "version": bundle.manifest.version,
        },
    )
    return {
        "sourceId": source_id,
        "bundleSha256": bundle.bundle_sha256,
        "entries": bundle.entries,
        "version": bundle.manifest.version,
        "runtime": bundle.manifest.runtime,
    }


@app.post("/api/specialist-runtime/deployments/{deployment_id}/activate")
def activate_runtime_deployment(
    deployment_id: int, body: RuntimeNoteBody, request: Request, user: User
) -> dict:
    """standby 후보를 active 로. **요청자와 승인자가 달라야 한다.**"""
    _require_admin(user)
    _check_write_request(request)
    _require_runtime_ready()
    try:
        rows = specialist_runtime_store.list_deployments()
        row = next((r for r in rows if int(r["id"]) == deployment_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="배포 기록을 찾지 못했습니다.")
        _runtime_scope(user, str(row["specialist"]))
        specialist_runtime_store.check_separation(str(row["deployed_by"]), user.email)
        specialist_runtime_store.activate(deployment_id, actor=user.email)
    except specialist_runtime_store.RuntimeStoreError as exc:
        _audit_event(
            actor=user.email, category="specialist-runtime", action="activate",
            target_type="specialist-deployment", target_id=str(deployment_id),
            outcome="failed",
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist-runtime", action="activate",
        target_type="specialist-deployment", target_id=str(deployment_id),
        outcome="succeeded",
        metadata={"specialist": row["specialist"], "digest": row["image_digest"]},
    )
    return {"deployments": specialist_runtime_store.list_deployments()}


@app.post("/api/specialist-runtime/specialists/{key}/disable")
def disable_runtime_specialist(
    key: str, body: RuntimeNoteBody, request: Request, user: User
) -> dict:
    """라우팅을 즉시 닫는다. **컨테이너 정리를 기다리지 않는다.**

    남의 코드가 이상하게 답할 때 기다릴 수 있는 시간은 몇 분이 아니라 몇 초다.
    """
    _require_admin(user)
    _check_write_request(request)
    _require_runtime_ready()
    _runtime_scope(user, key)
    try:
        specialist_runtime_store.disable(key, actor=user.email)
    except specialist_runtime_store.RuntimeStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist-runtime", action="disable",
        target_type="specialist", target_id=key, outcome="succeeded",
        metadata={"note": body.note[:200]},
    )
    return {"deployments": specialist_runtime_store.list_deployments(key)}


@app.post("/api/specialist-runtime/specialists/{key}/rollback")
def rollback_runtime_specialist(
    key: str, body: RuntimeNoteBody, request: Request, user: User
) -> dict:
    """직전 승인 digest 로 되돌린다. 되돌릴 곳이 없으면 **그 사실을 말한다.**"""
    _require_admin(user)
    _check_write_request(request)
    _require_runtime_ready()
    _runtime_scope(user, key)
    try:
        target = specialist_runtime_store.rollback_target(key)
        if target is None:
            raise HTTPException(
                status_code=422,
                detail="되돌릴 이전 승인 배포가 없습니다.",
            )
        new_id = specialist_runtime_store.create_deployment(
            specialist=key, build_id=int(target["build_id"]), deployed_by=user.email
        )
    except specialist_runtime_store.RuntimeStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="specialist-runtime", action="rollback",
        target_type="specialist", target_id=key, outcome="requested",
        metadata={"digest": target["image_digest"], "deploymentId": new_id},
    )
    return {
        "deploymentId": new_id,
        "imageDigest": target["image_digest"],
        "deployments": specialist_runtime_store.list_deployments(key),
    }


@app.get("/api/audit-events")
def audit_events(
    user: User,
    category: str = "",
    workspace: str = "",
    actor: str = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    _require_admin(user)
    return {
        "events": audit_store.list_events(
            qa_log_dir=reader.qa_log_dir(), category=category.strip(),
            workspace=workspace.strip(), actor=actor.strip(), limit=limit,
        )
    }


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
    _audit_event(
        actor=user.email,
        category="environment",
        action="change",
        target_type="settings",
        target_id="console-managed.env",
        outcome="succeeded",
        metadata={"changed": changed},
    )
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
    _audit_event(
        actor=user.email,
        category="workspace",
        action="save",
        target_type="workspace",
        target_id=key.strip().lower(),
        workspace=key.strip().lower(),
        outcome="succeeded",
        metadata={"role": body.role, "state": body.state},
    )
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
    _audit_event(
        actor=user.email,
        category="console-user",
        action="save",
        target_type="console-user",
        target_id=body.email.strip().lower(),
        outcome="succeeded",
        metadata={"role": body.role, "active": body.active},
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
    _audit_event(
        actor=user.email,
        category="llm",
        action="rotate" if body.key.strip() else "state-change",
        target_type="llm-provider",
        target_id=body.provider,
        outcome="succeeded",
        metadata={"enabled": body.enabled},
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
    _audit_event(
        actor=user.email,
        category="feedback",
        action="handled",
        target_type="feedback",
        target_id=event_id,
        outcome="succeeded",
    )
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
    _audit_event(
        actor=user.email,
        category="timer",
        action=body.action,
        target_type="systemd-timer",
        target_id=body.unit,
        outcome="succeeded",
        metadata={"preset": body.preset},
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
    _audit_event(
        actor=user.email,
        category="deployment",
        action="direct-deploy",
        target_type="deployment",
        target_id=str(result.get("request_id") or "current"),
        outcome="requested",
    )
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
    _audit_event(
        actor=user.email,
        category="deployment",
        action="request",
        target_type="deploy-request",
        target_id=str(request_id),
        workspace=body.workspace,
        outcome="requested",
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
    _audit_event(
        actor=user.email,
        category="deployment",
        action=body.decision,
        target_type="deploy-request",
        target_id=str(request_id),
        outcome="succeeded",
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
    _audit_event(
        actor=user.email,
        category="archive",
        action="read",
        target_type="document",
        target_id=path,
        outcome="succeeded",
    )


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
