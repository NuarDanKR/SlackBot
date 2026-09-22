"""PF 운영 콘솔 API — `/pf/api/` 아래 **읽기 전용 고정 경로만**.

설계: `docs/design/pf-hermes-owner-plan.md` §8, `docs/design/pf-console.md`

## 오늘 여는 것
개요·수집/Git·배치·비용·감사 **조회**뿐이다. `restart` · `stop` · archive sync ·
release activate · rollback 은 고정 helper, 중복 실행 lock, timeout, append-only 감사가
구현된 뒤에만 연다(§11). 오늘 버튼을 먼저 만들면 「눌러도 아무 일 없음」 과 「눌렀는데
되돌릴 수 없음」 중 하나가 된다.

## 만들지 않는 것 (§8.3)
임의 shell, env 원문 편집, secret 조회, 아카이브 편집, 자동 Git merge/force push,
TYBot 관리 화면. request body 로 service key 나 unit 이름을 받아 범위를 바꾸는 것도
하지 않는다 — 범위는 **권한에서만** 온다.

## 프로세스 경계
이 앱은 `tybot.*` 를 임포트하지 않는다. TYBot 의 env·DB·아카이브에 닿는 경로가
하나도 없어야 격리가 말이 된다. `tests/test_pf_isolation.py` 가 그 규칙을 고정한다.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import health, store
from .auth import PFAuthenticator, PFAuthError, PFUser
from .config import (
    SESSION_COOKIE,
    SESSION_COOKIE_PATH,
    SESSION_HOURS,
    PFConfigError,
    cookie_secure,
    dist_dir,
    stale_after_seconds,
    state_root,
)

logger = logging.getLogger("tybot_pf.api")

app = FastAPI(
    title="PF 운영 콘솔",
    description="프금팀 Hermes 상태를 보는 읽기 전용 화면입니다.",
    # `/pf/` 로 프록시된다. 문서 경로도 그 아래에 둔다.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------

_authenticator: PFAuthenticator | None = None


def authenticator() -> PFAuthenticator:
    global _authenticator
    if _authenticator is None:
        _authenticator = PFAuthenticator()
    return _authenticator


def current_user(
    auth: Annotated[PFAuthenticator, Depends(authenticator)],
    pf_console: Annotated[str | None, Cookie()] = None,
) -> PFUser:
    """세션 쿠키로 사용자를 만든다.

    **TYBot 쿠키(`tybot_console`)는 쳐다보지 않는다.** 이름이 다르므로 브라우저가
    같이 보내와도 이 자리에 오지 않는다. 그게 분리의 실제 모양이다.
    """
    if not pf_console:
        raise PFAuthError("로그인이 필요합니다.")
    return auth.user(pf_console)


User = Annotated[PFUser, Depends(current_user)]


@app.exception_handler(PFAuthError)
def _auth_error(_request: Request, exc: PFAuthError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(PFConfigError)
def _config_error(_request: Request, exc: PFConfigError) -> JSONResponse:
    # 설정이 없으면 **빈 화면이 아니라 이유**를 보여 준다. 빈 화면은 「아직 자료가
    # 없나 보다」 로 읽히고, 그 상태로 며칠이 지난다.
    logger.error("PF 콘솔 설정 오류: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(store.PFStoreError)
def _store_error(_request: Request, exc: store.PFStoreError) -> JSONResponse:
    logger.error("PF 조회 실패: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@app.post("/pf/api/login")
def login(
    body: LoginBody,
    response: Response,
    auth: Annotated[PFAuthenticator, Depends(authenticator)],
) -> dict:
    try:
        session = auth.login(body.email, body.password)
    except PFAuthError as exc:
        logger.warning("PF 로그인 실패 — 이메일 %r", body.email)
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="strict",
        # TLS 를 붙인 뒤에 켠다. 그 전에 켜면 쿠키가 전송되지 않아 로그인이 안 되고,
        # 화면에는 「비밀번호가 틀렸다」 처럼 보인다.
        secure=cookie_secure(),
        # `/pf` 로 좁힌다. TYBot 콘솔(`/`) 요청에 PF 세션이 실려 가지 않는다.
        path=SESSION_COOKIE_PATH,
    )
    user = auth.user(session)
    store.record(actor=user.email, action="login", detail=f"서비스 {len(user.services)}개")
    return {"user": user.to_json()}


@app.post("/pf/api/logout")
def logout(response: Response) -> dict:
    # 지울 때도 같은 `path` 를 줘야 한다. 다르면 브라우저가 다른 쿠키로 보고 남겨 둔다.
    response.delete_cookie(SESSION_COOKIE, path=SESSION_COOKIE_PATH)
    return {"ok": True}


@app.get("/pf/api/me")
def me(user: User) -> dict:
    """로그인한 사람과 **그가 볼 수 있는 서비스**.

    목록이 비어 있으면 화면은 「권한이 없습니다」 를 보여 준다. 로그인은 됐는데
    아무것도 안 보이는 상태를 빈 화면으로 두지 않는다 — 그건 고장으로 읽힌다.
    """
    return {"user": user.to_json()}


# ---------------------------------------------------------------------------
# 상태 — 개요 · 수집/Git · 배치 · 비용
# ---------------------------------------------------------------------------
#
# 넷은 **같은 상태 파일 한 장**에서 나온다. 화면만 나눈다. 파일을 네 번 읽지 않는
# 이유는 네 화면의 판정이 서로 어긋나면 안 되기 때문이다 — 개요는 정상인데 비용
# 화면만 「멈춤」 이면 어느 쪽이 맞는지 판정할 근거가 없다.

def _service_health(user: PFUser, key: str) -> tuple[dict, health.ServiceHealth]:
    row = store.service(key, allowed=user.services)
    if row is None:
        # 권한 밖과 없는 서비스를 같은 404 로 답한다(store.service 의 이유와 같다).
        raise HTTPException(status_code=404, detail="해당 서비스를 찾을 수 없습니다.")
    state_dir = str(row.get("state_dir") or "").strip()
    path = state_dir or (state_root() / key)
    return row, health.read(key, path, stale_after=stale_after_seconds())


def _service_json(row: dict) -> dict:
    return {
        "key": row["key"],
        "label": row["label"],
        "state": row["state"],
        "businessOwner": row.get("business_owner") or "",
        "infraOwner": row.get("infra_owner") or "",
    }


@app.get("/pf/api/services")
def list_services(user: User) -> dict:
    """볼 수 있는 서비스와 각각의 상태 한 줄.

    첫 화면이다. 서비스가 여럿이 되면 여기서 어느 것이 급한지 보인다.
    """
    rows = store.services(user.services)
    out = []
    for row in rows:
        state_dir = str(row.get("state_dir") or "").strip()
        got = health.read(
            row["key"],
            state_dir or (state_root() / row["key"]),
            stale_after=stale_after_seconds(),
        )
        out.append({
            **_service_json(row),
            "roles": sorted(user.roles_on(row["key"])),
            "health": got.to_json(),
        })
    return {"services": out}


@app.get("/pf/api/services/{key}/status")
def service_status(key: str, user: User) -> dict:
    """개요 화면. 무엇이 돌고 있고 마지막으로 언제 무엇을 했나."""
    row, got = _service_health(user, key)
    return {"service": _service_json(row), "health": got.to_json()}


@app.get("/pf/api/services/{key}/archive")
def service_archive(key: str, user: User) -> dict:
    """수집/Git 화면.

    Git 은 **읽기만** 한다. merge·push·force push 는 이 콘솔에 없다(§8.3).
    미push commit 수와 conflict 여부를 보여 주고, 처리는 사람이 서버에서 한다.
    """
    row, got = _service_health(user, key)
    fields = got.fields
    return {
        "service": _service_json(row),
        "status": got.status,
        "reason": got.reason,
        "lastIngest": fields.get("lastIngest", ""),
        "lastSlackConnect": fields.get("lastSlackConnect", ""),
        "lastArchivePull": fields.get("lastArchivePull", ""),
        "lastArchivePush": fields.get("lastArchivePush", ""),
        "unpushedCommits": fields.get("unpushedCommits"),
        "archiveConflict": fields.get("archiveConflict", ""),
        "errors": [e for e in got.errors if e["code"] in {"git_pull", "git_push", "conflict", "archive_invalid", "slack"}],
    }


@app.get("/pf/api/services/{key}/batches")
def service_batches(key: str, user: User) -> dict:
    """배치 화면.

    TYBot 배치 관리와 달리 **systemd 를 만지지 않는다.** PF 프로세스에 다른 서비스의
    unit 을 다룰 권한을 주지 않는다 — 그 권한이 있으면 조회 화면이 아니다.
    여기서는 Hermes 가 남긴 「마지막으로 언제 돌았나」 만 보여 준다.
    """
    row, got = _service_health(user, key)
    fields = got.fields
    return {
        "service": _service_json(row),
        "status": got.status,
        "reason": got.reason,
        "jobs": [
            {"key": "ingest", "label": "Slack 수집", "lastRunAt": fields.get("lastIngest", "")},
            {"key": "archive-pull", "label": "자료 Git 받기", "lastRunAt": fields.get("lastArchivePull", "")},
            {"key": "archive-push", "label": "자료 Git 올리기", "lastRunAt": fields.get("lastArchivePush", "")},
            {"key": "digest", "label": "요약(digest)", "lastRunAt": fields.get("lastDigest", "")},
        ],
    }


@app.get("/pf/api/services/{key}/cost")
def service_cost(key: str, user: User) -> dict:
    """비용 화면.

    **모델 비용의 주인은 프금팀이다**(§7.1). 우리는 상한을 걸지 않고 보기만 한다 —
    걸 수 있는 자리가 우리 쪽에 없고, 있다고 착각하면 초과했을 때 아무도 안 막는다.
    화면이 그 사실을 같이 말한다.
    """
    row, got = _service_health(user, key)
    fields = got.fields
    return {
        "service": _service_json(row),
        "status": got.status,
        "reason": got.reason,
        "callsToday": fields.get("callsToday"),
        "tokensToday": fields.get("tokensToday"),
        "costUsdToday": fields.get("costUsdToday"),
        "lastModelCall": fields.get("lastModelCall", ""),
        "errors": [e for e in got.errors if e["code"] == "anthropic"],
        # 상한을 우리가 못 건다는 사실은 데이터가 아니라 계약이다. 화면이 지어내지
        # 않도록 서버가 준다.
        "limitOwner": "프금팀",
    }


@app.get("/pf/api/audit")
def audit_events(
    user: User,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """감사 화면. 권한 있는 서비스의 기록과 본인의 로그인 기록."""
    return {
        "events": store.audit(actor=user.email, allowed=user.services, limit=limit)
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
#
# **API 를 전부 등록한 뒤에 붙인다.** `/` 에 먼저 마운트하면 정적 라우트가 API 요청을
# 가로채 404 가 난다(TYBot 콘솔에서 2026-09-15 에 겪었다).

def mount_dist() -> None:
    root = dist_dir()
    if root is None:
        return
    if not (root / "index.html").exists():
        logger.warning("PF_CONSOLE_DIST 에 index.html 이 없습니다: %s", root)
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/pf", StaticFiles(directory=str(root), html=True), name="pf-console")
    logger.info("PF 콘솔 화면을 함께 서빙합니다: %s", root)


mount_dist()


@app.get("/pf/api/health")
def liveness() -> dict:
    """프로세스가 떠 있는지. **인증 없이 답한다** — 로그인 전에도 확인해야 한다.

    떠 있다는 것 말고는 아무것도 말하지 않는다. 서비스 상태·버전·경로는 담지 않는다.
    """
    return {"ok": True}


def _log_startup() -> None:
    if not os.getenv("PF_DATABASE_URL", "").strip():
        logger.error(
            "PF_DATABASE_URL 이 없습니다. 로그인과 모든 조회가 503 으로 답합니다."
        )
    if not cookie_secure():
        logger.warning(
            "PF_CONSOLE_COOKIE_SECURE 가 꺼져 있습니다. TLS 를 붙인 뒤 반드시 켜세요"
            " — 켜기 전까지 세션 쿠키가 평문으로 흐릅니다."
        )


_log_startup()
