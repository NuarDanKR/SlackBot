"""관리 콘솔 API (읽기).

실행:
    uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787

## 보안 전제
- 기본 바인딩은 루프백이다. 외부 노출 경로와 접근 통제는 아직 결정되지 않았다.
- 그래도 사용자 식별은 한다 — 누가 승인했고 누가 원문을 열었는지 남겨야 하기 때문이다(`auth.py`).
- 응답에 담지 않는 것: 사용자 질문·답변 본문, 시크릿 원문.
- 아카이브 원문 본문은 **관리자에게만**, 그리고 **열람 기록을 남기며** 내려보낸다.

이 파일에는 쓰기(등록·승인·반영) 엔드포인트가 없다. 서버를 바꾸는 동작은 자동 검사·승인·되돌리기
장치와 함께 붙여야 해서 다음 단계로 미룬다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..archive.store import ArchiveStore
from . import reader
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


@app.get("/api/health")
def health() -> dict:
    """인증 없이 열어 두는 확인용 엔드포인트. 상태만 알려 주고 내용은 담지 않는다."""
    return {"ok": True, "at": datetime.now(KST).isoformat(timespec="seconds")}


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
