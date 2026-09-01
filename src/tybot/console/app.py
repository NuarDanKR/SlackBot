"""관리 콘솔 API.

실행:
    uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787

## 보안 전제
- 기본 바인딩은 루프백이다. 외부 노출 경로와 접근 통제는 아직 결정되지 않았다.
- 그래도 사용자 식별은 한다 — 누가 승인했고 누가 원문을 열었는지 남겨야 하기 때문이다(`auth.py`).
- 응답에 담지 않는 것: 사용자 질문·답변 본문, 시크릿 원문.
- 아카이브 원문 본문은 **관리자에게만**, 그리고 **열람 기록을 남기며** 내려보낸다.

환경설정 쓰기는 owner 전용 허용 목록만 제공한다. 원문·시크릿·임의 파일 편집 경로는 없다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..archive.store import ArchiveStore
from . import env_settings, health, reader
from .auth import SESSION_COOKIE, SESSION_HOURS, Authenticator, AuthError, ConsoleUser

logger = logging.getLogger("tybot.console.api")
KST = timezone(timedelta(hours=9))

app = FastAPI(
    title="TYBot 관리 콘솔 API",
    version="0.1.0",
    # 개발·운영 점검용 문서 페이지. 외부 노출 경로를 정할 때 접근 제한도 함께 검토한다.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
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
    username: str
    password: str


class EnvWorkspaceBody(BaseModel):
    key: str
    label: str
    root: bool
    readable: list[str]


class EnvSettingsBody(BaseModel):
    workspaces: list[EnvWorkspaceBody]
    realtimeIngest: bool
    autojoinChannels: bool
    replyInThread: bool


@app.post("/api/login")
def login(
    body: LoginBody,
    response: Response,
    auth: Annotated[Authenticator, Depends(authenticator)],
) -> dict:
    """아이디·비밀번호로 로그인하고 세션 쿠키를 받는다.

    쿠키를 쓰는 이유: 화면이 토큰을 들고 있으면 localStorage 에 남고, 화면 스크립트가
    읽을 수 있는 값이 된다. HttpOnly 쿠키는 스크립트가 읽지 못한다.
    `SameSite=strict` 로 두어 다른 사이트에서 이 콘솔로 요청을 보낼 수 없게 한다.
    """
    try:
        session = auth.login(body.username, body.password)
    except AuthError as e:
        logger.warning("로그인 실패 — 아이디 %r", body.username)
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
    user = auth.by_username(body.username)
    assert user is not None  # login() 이 성공했으면 반드시 있다
    logger.info("로그인 — %s (%s)", body.username, user.user.email)
    return _me(user.user, auth)


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

def _me(user: ConsoleUser, auth: Authenticator) -> dict:
    return {
        "name": user.display(),
        "email": user.email,
        "role": user.role,
        "workspaces": sorted(user.workspaces),
        "allWorkspaces": user.all_workspaces,
        # 임시 계정으로 열려 있으면 화면에도 경고를 띄운다.
        "usingDefaultAccount": auth.using_default,
    }


@app.get("/api/me")
def me(user: User, auth: Annotated[Authenticator, Depends(authenticator)]) -> dict:
    return _me(user, auth)


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
    if not user.is_owner:
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
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="관리자만 볼 수 있습니다.")
    return {"entries": read_audit_entries()}


@app.get("/api/harness")
def harness(user: User) -> dict:
    """봇 규칙 문서 목록과 내용."""
    return {"files": _visible(user, reader.harness_files())}


def _require_owner(user: ConsoleUser) -> None:
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="관리자만 환경변수 설정을 볼 수 있습니다.")


def _check_write_request(request: Request, auth: Authenticator) -> None:
    """쿠키 인증 쓰기 요청의 CSRF와 임시 관리자 계정 사용을 차단한다."""
    if auth.using_default:
        raise HTTPException(
            status_code=403,
            detail="임시 admin/1111 계정에서는 환경설정을 변경할 수 없습니다. CONSOLE_ACCOUNTS를 먼저 설정하세요.",
        )
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
    _require_owner(user)
    return env_settings.snapshot()


@app.put("/api/env-settings")
def put_env_settings(
    body: EnvSettingsBody,
    request: Request,
    user: User,
    auth: Annotated[Authenticator, Depends(authenticator)],
) -> dict:
    _require_owner(user)
    _check_write_request(request, auth)
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


@app.get("/api/health-report")
def health_report(user: User) -> dict:
    """헬스 체크 — 봇이 "돌고는 있는데 제 일을 못 하는" 상태를 드러낸다.

    아래 `/api/health` 는 프로세스가 살아 있는지만 답하는 무인증 확인용이고,
    이쪽은 수집·답변 품질·명령 정합·피드백까지 본다. 담당자에게는 자기 워크스페이스
    몫만 보인다 — 범위는 `health.report` 안에서 거른다(합계가 섞이지 않게).
    """
    return health.report(
        allowed=None if user.all_workspaces else user.workspaces,
        store=store(),
    )


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
