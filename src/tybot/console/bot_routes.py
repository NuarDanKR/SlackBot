"""콘솔 API — 봇 관리 (`/api/bots` · `/api/workspaces/{ws}/bot-connections` · `bot-routes`).

설계: `docs/design/workspace-service-console-redesign.md` §7 (2026-09-28)
판단: `bot_admin` · 저장소: `bot_repo`

## 왜 `app.py` 가 아니라 여기 있나

`license_routes` 가 남긴 선례를 따른다. `app.py` 는 3000줄이 넘고, 새 기능을 거기
쌓으면 충돌 지점이 한 파일에 몰린다. 로그인·권한·CSRF·감사 기록은 `app.py` 의
것을 그대로 빌려 쓴다 — 여기서 새로 만들면 규칙이 두 벌이 되고, 한쪽만 고치는 날
이 화면만 느슨해진다.

## 이 API 가 하지 않는 것

- **평문 토큰을 돌려주지 않는다.** 나가는 것은 mask 뿐이다(§7.5)
- **연결을 저장했다고 수집을 켜지 않는다.** 채널 `active` 와 writer 인수는
  `archiving_admin` 과 release gate 가 계속 소유한다(§9)
- **옛 endpoint 를 지우지 않는다.** `/api/workspaces/{key}/archiving/services/*` 는
  `ArchivingPanel` 이 아직 부른다. 호출부가 사라진 뒤에 지운다(§7.4)

## 오류 코드

| 무엇 | 코드 |
|---|---|
| 토큰 형식·모르는 봇·전문 봇이 아닌 내부 route·나쁜 health 로 active | `422` |
| 다른 Team ID · 같은 Bot User ID | `409` |
| Slack 일시 장애 | `503` (저장 여부를 문구에 적는다) |
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import bot_admin
from .app import _audit_event, _check_write_request, _require_admin, current_user
from .auth import ConsoleUser
from .workspace_store import WorkspaceStoreError

logger = logging.getLogger("tybot.console.api")
router = APIRouter()
User = Annotated[ConsoleUser, Depends(current_user)]

#: 409 로 내보낼 신원 충돌 문구. 나머지 거절은 422 다(§7.5).
CONFLICT_MARKERS = ("다른 워크스페이스", "봇 사용자")


def _actor(user: ConsoleUser, reason: str) -> bot_admin.Actor:
    try:
        return bot_admin.Actor(user.email, reason)
    except bot_admin.BotAdminRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _refused(exc: Exception) -> HTTPException:
    """거절을 HTTP 로. **예외 문구에 비밀값이 없다**(`bot_admin` 이 mask 만 든다)."""
    if isinstance(exc, bot_admin.IdentityConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


class SlackConnectionBody(BaseModel):
    botToken: str = ""
    appToken: str = ""
    note: str = ""
    reason: str = Field(min_length=1)


class VerifyBody(BaseModel):
    reason: str = Field(min_length=1)


class ConnectionStateBody(BaseModel):
    state: Literal["disabled", "retired"]
    reason: str = Field(min_length=1)


class ManifestAttestationBody(BaseModel):
    manifestId: str = Field(min_length=1, max_length=120)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)


class RouteBody(BaseModel):
    mode: Literal["disabled", "shadow", "active"]
    fallbackBotKey: str = "master"
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------

@router.get("/api/bots")
def get_bots(user: User) -> dict:
    """봇 목록. **Hermes 는 한 번만 나온다** — 연결을 배열로 든다."""
    _require_admin(user)
    try:
        return bot_admin.bots()
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/bots/{bot_key}")
def get_bot(bot_key: str, user: User) -> dict:
    _require_admin(user)
    try:
        return bot_admin.bot_detail(bot_key.strip().lower())
    except bot_admin.BotAdminRefused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/workspaces/{workspace}/bot-connections")
def get_workspace_connections(workspace: str, user: User) -> dict:
    """한 워크스페이스의 Slack 직접 연결과 Master 내부 호출을 **함께** 준다."""
    _require_admin(user)
    try:
        return bot_admin.workspace_connections(workspace.strip().lower())
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/workspaces/{workspace}/bot-routes")
def get_workspace_routes(workspace: str, user: User) -> dict:
    _require_admin(user)
    try:
        return bot_admin.workspace_routes(workspace.strip().lower())
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/bot-audit")
def get_bot_audit(user: User, workspace: str = "", limit: int = 100) -> dict:
    """연결·라우트 변경 이력. 옛 서비스 표 기록도 같은 줄기로 보인다."""
    _require_admin(user)
    try:
        return bot_admin.audit(workspace.strip().lower(), limit)
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/bot-migration-check")
def get_migration_check(user: User, workspace: str = "") -> dict:
    """옛 표와 새 표가 같은 말을 하나. **읽기만 한다**(§5.5).

    이관을 「했다」 로 믿고 넘어가면 한 워크스페이스만 안 옮겨진 채 남는다. 그건
    오류가 아니라 **그 워크스페이스에서만 봇이 안 뜨는** 상태로 나타난다.
    """
    _require_admin(user)
    try:
        return bot_admin.legacy_diff(workspace.strip().lower())
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 쓰기 — Slack 연결
# ---------------------------------------------------------------------------

@router.put("/api/workspaces/{workspace}/bot-connections/{bot_key}/slack")
def put_slack_connection(
    workspace: str, bot_key: str, body: SlackConnectionBody, request: Request, user: User,
) -> dict:
    """토큰 쌍을 저장한다. **응답에는 mask 만 있다.**

    저장은 켜지 않는다 — 신원 검사를 통과해야 켜진다. 등록과 동시에 켜면 토큰을
    잘못 붙인 채로 수집이 시작되고, 그건 붙인 사람이 자리를 뜬 뒤에 드러난다.
    """
    _require_admin(user)
    _check_write_request(request)
    key, bot = workspace.strip().lower(), bot_key.strip().lower()
    try:
        bot_admin.save_slack_connection(
            key, bot,
            actor=_actor(user, body.reason),
            bot_token=body.botToken.strip(),
            app_token=body.appToken.strip(),
            note=body.note.strip(),
        )
    except bot_admin.BotAdminRefused as exc:
        raise _refused(exc) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="workspace", action="bot_connection_token",
        target_type="bot_connection", target_id=f"{bot}/slack_socket",
        workspace=key, outcome="succeeded", metadata={"reason": body.reason},
    )
    return bot_admin.workspace_connections(key)


@router.post("/api/workspaces/{workspace}/bot-connections/{bot_key}/slack/verify-identity")
def verify_slack_identity(
    workspace: str, bot_key: str, body: VerifyBody, request: Request, user: User,
) -> dict:
    """저장된 토큰 쌍으로 Slack 에 물어 신원을 적는다. **평문은 안 돌려준다.**

    앱 토큰까지 확인하는 이유는 Socket Mode 가 그 토큰으로 열리기 때문이다 —
    봇 토큰만 맞으면 화면은 정상인데 이벤트가 하나도 안 온다.
    """
    _require_admin(user)
    _check_write_request(request)
    key, bot = workspace.strip().lower(), bot_key.strip().lower()
    actor = _actor(user, body.reason)
    from . import bot_identity

    try:
        problem = bot_identity.verify_connection(key, bot, actor=actor)
    except bot_identity.SlackUnavailable as exc:
        # 저장은 그대로다. 「저장이 안 됐나」 를 사람이 다시 묻지 않게 적는다.
        raise HTTPException(
            status_code=503,
            detail=f"{exc} 토큰은 저장돼 있습니다. 잠시 뒤 다시 확인하세요.",
        ) from exc
    except bot_admin.BotAdminRefused as exc:
        raise _refused(exc) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if problem:
        _audit_event(
            actor=user.email, category="workspace", action="bot_connection_identity",
            target_type="bot_connection", target_id=f"{bot}/slack_socket",
            workspace=key, outcome="refused", metadata={"reason": body.reason},
        )
        status = 409 if any(mark in problem for mark in CONFLICT_MARKERS) else 422
        raise HTTPException(status_code=status, detail=problem)
    _audit_event(
        actor=user.email, category="workspace", action="bot_connection_identity",
        target_type="bot_connection", target_id=f"{bot}/slack_socket",
        workspace=key, outcome="succeeded", metadata={"reason": body.reason},
    )
    return bot_admin.workspace_connections(key)


@router.post("/api/workspaces/{workspace}/bot-connections/{bot_key}/slack/state")
def set_slack_connection_state(
    workspace: str, bot_key: str, body: ConnectionStateBody, request: Request, user: User,
) -> dict:
    """연결을 끄거나 그만 쓴다. **런타임은 안 건드린다.**

    PF 직접 연결을 중지해도 Hermes 런타임과 Master 내부 호출은 그대로 돈다(§0).
    """
    _require_admin(user)
    _check_write_request(request)
    key, bot = workspace.strip().lower(), bot_key.strip().lower()
    try:
        bot_admin.set_connection_state(
            key, bot, body.state, actor=_actor(user, body.reason),
        )
    except bot_admin.BotAdminRefused as exc:
        raise _refused(exc) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="workspace", action="bot_connection_state",
        target_type="bot_connection", target_id=f"{bot}/slack_socket",
        workspace=key, outcome="succeeded",
        metadata={"reason": body.reason, "state": body.state},
    )
    return bot_admin.workspace_connections(key)


@router.put("/api/workspaces/{workspace}/bot-connections/{bot_key}/slack/manifest-attestation")
def put_manifest_attestation(
    workspace: str, bot_key: str, body: ManifestAttestationBody,
    request: Request, user: User,
) -> dict:
    """관리자가 **정본 hash 와 Slack 설정을 대조했다**고 적는다(§6.2).

    `auth.test` 성공만으로 Manifest 가 일치한다고 표시하지 않는다. 신원은 「이 토큰이
    누구인가」 이고 Manifest 는 「그 앱이 무슨 권한을 갖고 있나」 다 — 다른 질문이다.
    """
    _require_admin(user)
    _check_write_request(request)
    key, bot = workspace.strip().lower(), bot_key.strip().lower()
    from . import bot_manifest

    try:
        bot_manifest.attest(
            key, bot, manifest_id=body.manifestId, sha256=body.sha256,
            actor=_actor(user, body.reason),
        )
    except bot_admin.BotAdminRefused as exc:
        raise _refused(exc) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="workspace", action="bot_manifest_attestation",
        target_type="bot_connection", target_id=f"{bot}/slack_socket",
        workspace=key, outcome="succeeded",
        metadata={"reason": body.reason, "manifestId": body.manifestId},
    )
    return bot_admin.workspace_connections(key)


@router.get("/api/bot-manifests")
def get_manifests(user: User) -> dict:
    """정본 Manifest 목록과 hash. **파일에서 직접 센다.**

    DB 에 적어 두면 파일이 바뀌어도 그 값이 남고, 그때 화면은 「대조했다」 를
    옛 hash 기준으로 보여 준다.
    """
    _require_admin(user)
    from . import bot_manifest

    return bot_manifest.catalog()


# ---------------------------------------------------------------------------
# 쓰기 — Master 내부 호출 라우트
# ---------------------------------------------------------------------------

@router.put("/api/workspaces/{workspace}/bot-routes/{bot_key}")
def put_route(
    workspace: str, bot_key: str, body: RouteBody, request: Request, user: User,
) -> dict:
    """내부 호출을 끄거나, 그림자로 돌리거나, 실제로 쓴다.

    `active` 는 런타임이 건강할 때만. `shadow` 는 사용자에게 결과를 전달하지
    않으므로 막지 않는다(§7.3).
    """
    _require_admin(user)
    _check_write_request(request)
    key, bot = workspace.strip().lower(), bot_key.strip().lower()
    try:
        bot_admin.set_route(
            key, bot, body.mode,
            actor=_actor(user, body.reason),
            fallback_bot_key=body.fallbackBotKey.strip().lower() or "master",
        )
    except bot_admin.BotAdminRefused as exc:
        raise _refused(exc) from exc
    except WorkspaceStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit_event(
        actor=user.email, category="workspace", action="bot_route_mode",
        target_type="specialist_route", target_id=bot, workspace=key,
        outcome="succeeded", metadata={"reason": body.reason, "mode": body.mode},
    )
    return bot_admin.workspace_routes(key)
