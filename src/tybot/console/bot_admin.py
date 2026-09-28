"""봇 관리의 **판단** — 하나의 Hermes, 여러 연결.

설계: `docs/design/workspace-service-console-redesign.md` §2·§4·§7 (2026-09-28)
저장소: `bot_repo` · API: `bot_routes`

## 무엇이 문제였나

`workspace_service` 는 `master|archiver|hermes_direct` 세 값을 고정했다. 그래서
콘솔에서 **Hermes 가 둘로 보인다** — PF 가 직접 부르는 것과 Master 가 내부로
부르는 것이 다른 봇처럼 놓인다. 봇은 하나이고 다른 것은 **연결 방식**뿐이다.

    Hermes ─┬─ slack_socket    (PF 공존 기간, Slack 토큰 있음)
            └─ master_internal (Master 가 내부 호출, 토큰 없음)

그래서 읽기 모델이 봇마다 **연결 배열**을 든다. 두 연결을 두 봇으로 세면, 하나를
끄는 사람이 다른 하나도 끈 줄 안다.

## 정본을 합치지 않는다

| 무엇 | 정본 |
|---|---|
| 논리 봇 정체성 | `bot_catalog` |
| Slack 연결 | `bot_connection` |
| Master 내부 호출 | `specialist_route` (+ 배정은 `specialist_workspace`) |
| 런타임 배포·health | `specialist_bot` · specialist runtime 표 |

읽기 모델은 **나란히 보여 줄 뿐** 한 열로 합치지 않는다. 합치면 「정체성이 살아
있나」 와 「지금 켜져 있나」 를 구분할 수 없고, 그때 한쪽을 끈 사람이 다른 쪽까지
껐다고 믿는다.

## 연결을 켠다고 수집이 시작되지 않는다

토큰을 넣고 신원을 확인하는 것과, 그 봇이 운영 원문을 쓰기 시작하는 것은 다른
일이다. 채널 `active` 전환과 writer 인수는 `archiving_admin` 과 release gate 가
계속 소유한다(§9). 이 파일에는 그 손잡이가 **없다.**
"""

from __future__ import annotations

from dataclasses import dataclass

from .bot_repo import BotRepo, default_repo
from .workspace_store import WorkspaceStoreError, _fernet, _mask, _validate_token

#: 감사에 남길 주어. 표 이름이 아니라 **사람이 부르는 이름**이다.
SUBJECT_CONNECTION = "bot_connection"
SUBJECT_ROUTE = "specialist_route"

#: 연결이 갈 수 있는 상태. `enabled` 는 여기서 못 만든다 — 신원 검사를 통과할
#: 때만 켜진다(`record_identity`).
SETTABLE_STATES = ("disabled", "retired")

ROUTE_MODES = ("disabled", "shadow", "active")

#: `active` 라우트를 허용하는 런타임 상태. 나머지는 사람이 먼저 고쳐야 한다.
HEALTHY = "ok"

#: **돌고 있는 봇이 이 표들을 읽는가.**
#:
#: 지금은 아니다. Master 는 `workspace_secret`(`workspaces.load_workspaces`),
#: Archiver 는 `workspace_service`(`archiver_runtime_config`), Master 의 전문 봇
#: 라우팅은 `specialist_workspace`(`specialist_router`)를 읽는다. 새 표를 읽는
#: 런타임은 아직 없다 — credential reader 전환은 별도 단계다(§12.2 8단계).
#:
#: 그래서 이 화면의 저장은 **기록이지 적용이 아니다.** 그 사실을 화면이 말하지
#: 않으면, 연결을 끈 사람은 수집이 멈춘 줄 알고 자리를 뜬다. 그 오해는 조용하고
#: 오래간다 — 아무 오류도 안 나기 때문이다.
#:
#: 전환이 끝나면 이 값을 True 로 바꾼다. `test_bot_admin` 이 실제 reader 를 훑어
#: 값과 코드가 어긋나면 실패시킨다.
RUNTIME_READS_NEW_TABLES = False

#: 런타임에 영향을 주는 것과 주지 않는 것. 화면 문구의 정본이다.
RUNTIME_EFFECT_NOW = (
    "저장·검증은 **콘솔 기록**이다. 돌고 있는 Master·Archiver·Hermes 프로세스는"
    " 아직 옛 표를 읽는다.",
    "연결을 끄거나 그만 써도 **수집이 멈추지 않는다.** 수집을 멈추려면 채널 수집"
    " 설정(모드)과 프로세스를 따로 다뤄야 한다.",
    "라우트 모드를 바꿔도 **지금 답변 경로는 그대로다.** Master 는 아직 배정"
    "(`specialist_workspace`)만 보고 라우팅한다.",
)


def runtime_effect() -> dict:
    """이 화면의 변경이 **지금 돌고 있는 것**에 무엇을 하나.

    사람이 제일 자주 틀리는 자리다. 콘솔에서 껐는데 봇이 계속 도는 것을 보면
    「콘솔이 고장났다」 로 읽고, 그때 서버에 들어가 프로세스를 죽인다.
    """
    return {
        "appliesNow": RUNTIME_READS_NEW_TABLES,
        "summary": (
            "지금은 설정만 기록됩니다. 돌고 있는 봇에는 반영되지 않습니다."
            if not RUNTIME_READS_NEW_TABLES
            else "저장 즉시 런타임에 반영됩니다."
        ),
        "details": list(RUNTIME_EFFECT_NOW) if not RUNTIME_READS_NEW_TABLES else [],
    }


class BotAdminRefused(WorkspaceStoreError):
    """콘솔 조작이 거절됐다. **사유를 사람 말로 들고 있다.**"""


class IdentityConflict(BotAdminRefused):
    """다른 워크스페이스의 토큰이거나 이미 쓰는 봇 사용자다(§7.5 → 409)."""


@dataclass(frozen=True)
class Actor:
    """누가 왜 바꿨나. 빈 값을 허용하지 않는 이유가 이 클래스의 전부다."""

    name: str
    reason: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BotAdminRefused("바꾼 사람이 비어 있습니다.")
        if not self.reason.strip():
            raise BotAdminRefused(
                "사유가 비어 있습니다. 사고가 났을 때 범위를 정하려면 왜 바꿨는지가 필요합니다."
            )


# ---------------------------------------------------------------------------
# 읽기 — 통합 read model
# ---------------------------------------------------------------------------

def _connection_view(row: dict) -> dict:
    """화면이 보는 Slack 연결 하나. **토큰은 mask 만 나간다.**"""
    return {
        "id": row.get("id"),
        "workspace": row.get("workspace"),
        "type": "slack_socket",
        "state": row.get("state"),
        "teamId": row.get("team_id") or "",
        "botUserId": row.get("bot_user_id") or "",
        "identityOk": row.get("identity_ok"),
        "identityError": row.get("identity_error") or "",
        "identityCheckedAt": row.get("identity_checked_at"),
        "manifestId": row.get("manifest_id") or "",
        "manifestAttestedSha256": row.get("manifest_attested_sha256") or "",
        "manifestAttestedAt": row.get("manifest_attested_at"),
        "manifestAttestedBy": row.get("manifest_attested_by") or "",
        "lastHeartbeatAt": row.get("last_heartbeat_at"),
        "lastEventAt": row.get("last_event_at"),
        "runtimeVersion": row.get("runtime_version") or "",
        "runtimeError": row.get("runtime_error") or "",
        "note": row.get("note") or "",
        "botTokenMask": row.get("bot_mask") or "",
        "appTokenMask": row.get("app_mask") or "",
        "tokenCount": int(row.get("token_count") or 0),
        "updatedAt": row.get("updated_at"),
        "updatedBy": row.get("updated_by") or "",
    }


def _route_view(row: dict) -> dict:
    """Master 내부 호출 하나. **토큰 칸이 없다** — 내부 호출에는 Slack 토큰이 없다."""
    return {
        "workspace": row.get("workspace"),
        "type": "master_internal",
        "mode": row.get("route_mode") or "disabled",
        "fallbackBotKey": row.get("fallback_bot_key") or "master",
        "assigned": bool(row.get("assigned")),
        "lastShadowCheckedAt": row.get("last_shadow_checked_at"),
        "lastShadowResult": row.get("last_shadow_result") or "",
        "updatedAt": row.get("updated_at"),
        "updatedBy": row.get("updated_by") or "",
    }


def _runtime_view(row: dict | None) -> dict | None:
    """전문 봇 런타임. 카탈로그와 **다른 칸**이다(§4.2)."""
    if row is None:
        return None
    return {
        "state": row.get("state"),
        "health": row.get("health"),
        "version": row.get("version") or "",
        "contractVersion": row.get("contract_version") or "",
        "errorCode": row.get("error_code") or "",
        "lastCheckedAt": row.get("last_checked_at"),
        "domain": row.get("domain") or "",
        "adapter": row.get("adapter") or "",
    }


def bots(repo: BotRepo | None = None) -> dict:
    """봇 목록. **Hermes 는 한 번만 나온다.**

    연결이 둘이어도 봇은 하나다 — 배열로 들고 있을 뿐이다. 두 행으로 나누면 화면이
    「Hermes Direct」 라는 없는 봇을 만들어 내고, 그걸 끄는 사람이 런타임까지 껐다고
    믿는다(§13).
    """
    store = repo or default_repo()
    connections = store.connections()
    routes = store.routes()
    runtimes = {str(row["key"]): row for row in store.specialists()}

    out: list[dict] = []
    for bot in store.catalog():
        key = str(bot["key"])
        bindings = [
            _connection_view(row) for row in connections if str(row["bot_key"]) == key
        ]
        bindings += [
            _route_view(row) for row in routes if str(row["specialist"]) == key
        ]
        out.append({
            "key": key,
            "displayName": bot.get("display_name") or key,
            "category": bot.get("category"),
            "ownerTeam": bot.get("owner_team") or "",
            "slackConnectable": bool(bot.get("slack_connectable")),
            "internallyInvokable": bool(bot.get("internally_invokable")),
            "state": bot.get("state"),
            # 런타임은 **별도 칸**이다. 카탈로그 state 와 합치지 않는다.
            "runtime": _runtime_view(runtimes.get(key)),
            "bindings": bindings,
        })
    return {"bots": out, "runtimeEffect": runtime_effect()}


def bot_detail(bot_key: str, repo: BotRepo | None = None) -> dict:
    found = [row for row in bots(repo)["bots"] if row["key"] == bot_key]
    if not found:
        raise BotAdminRefused(f"등록되지 않은 봇입니다: {bot_key}")
    return found[0]


def workspace_connections(workspace: str, repo: BotRepo | None = None) -> dict:
    """한 워크스페이스의 연결 전부 — Slack 직접과 Master 호출을 **함께** 준다.

    화면이 두 번 부르면 보는 순간의 상태가 서로 다른 시각의 것이 되고, 그때 사람은
    자기가 누른 것이 안 먹었다고 생각해 한 번 더 누른다.
    """
    store = repo or default_repo()
    catalog = {str(row["key"]): row for row in store.catalog()}
    runtimes = {str(row["key"]): row for row in store.specialists()}
    connections = store.connections(workspace)
    routes = store.routes(workspace)

    rows: list[dict] = []
    for key, bot in catalog.items():
        slack = [_connection_view(r) for r in connections if str(r["bot_key"]) == key]
        internal = [_route_view(r) for r in routes if str(r["specialist"]) == key]
        if not slack and not internal and not bot.get("slack_connectable"):
            # 연결도 배정도 없고 Slack 에 붙을 수도 없는 봇은 이 워크스페이스와
            # 아무 관계가 없다. 빈 행을 늘리면 정작 볼 것이 밀린다.
            continue
        rows.append({
            "key": key,
            "displayName": bot.get("display_name") or key,
            "category": bot.get("category"),
            "slackConnectable": bool(bot.get("slack_connectable")),
            "internallyInvokable": bool(bot.get("internally_invokable")),
            "runtime": _runtime_view(runtimes.get(key)),
            "slack": slack,
            "internal": internal,
        })
    return {
        "workspace": workspace, "bots": rows, "runtimeEffect": runtime_effect(),
    }


def workspace_routes(workspace: str, repo: BotRepo | None = None) -> dict:
    store = repo or default_repo()
    runtimes = {str(row["key"]): row for row in store.specialists()}
    rows = []
    for row in store.routes(workspace):
        view = _route_view(row)
        view["key"] = str(row["specialist"])
        view["runtime"] = _runtime_view(runtimes.get(view["key"]))
        rows.append(view)
    return {
        "workspace": workspace, "routes": rows, "runtimeEffect": runtime_effect(),
    }


def audit(workspace: str = "", limit: int = 100, repo: BotRepo | None = None) -> dict:
    store = repo or default_repo()
    rows = store.audit(workspace, max(1, min(int(limit), 500)))
    return {"entries": [
        {
            "at": row.get("at"),
            "actor": row.get("actor") or "",
            "subject": row.get("subject") or "",
            "workspace": row.get("workspace") or "",
            "field": row.get("field") or "",
            "oldValue": row.get("old_value") or "",
            "newValue": row.get("new_value") or "",
            "reason": row.get("reason") or "",
        }
        for row in rows
    ]}


# ---------------------------------------------------------------------------
# 이관 비교 — 옛 표와 새 표가 같은 말을 하나
# ---------------------------------------------------------------------------

def _legacy_bot_key(service: str) -> str:
    """`hermes_direct` 는 `hermes` 다. 이 한 줄이 이관의 요점이다."""
    return "hermes" if service == "hermes_direct" else service


def legacy_diff(workspace: str = "", repo: BotRepo | None = None) -> dict:
    """옛 표와 새 표의 차이. **읽기만 한다**(§5.5).

    이관을 「했다」 로 믿고 넘어가면, 한 워크스페이스만 안 옮겨진 채 남는다. 그건
    오류가 아니라 **그 워크스페이스에서만 봇이 안 뜨는** 상태로 나타난다.
    """
    store = repo or default_repo()
    new = {
        (str(row["workspace"]), str(row["bot_key"])): row
        for row in store.connections(workspace)
    }
    problems: list[dict] = []
    checked = 0
    for old in store.legacy_services(workspace):
        checked += 1
        key = (str(old["workspace"]), _legacy_bot_key(str(old["service"])))
        row = new.get(key)
        if row is None:
            problems.append({
                "workspace": key[0], "botKey": key[1],
                "problem": "새 표에 연결이 없습니다",
            })
            continue
        for field, old_value, new_value in (
            ("state", old.get("state"), row.get("state")),
            ("teamId", old.get("team_id"), row.get("team_id")),
            ("botUserId", old.get("bot_user_id"), row.get("bot_user_id")),
            ("botTokenMask", old.get("bot_mask"), row.get("bot_mask")),
            ("appTokenMask", old.get("app_mask"), row.get("app_mask")),
            ("tokenCount", int(old.get("token_count") or 0),
             int(row.get("token_count") or 0)),
        ):
            if (old_value or "") != (new_value or ""):
                problems.append({
                    "workspace": key[0], "botKey": key[1], "problem": f"{field} 불일치",
                    "legacy": str(old_value or ""), "current": str(new_value or ""),
                })
    return {"checked": checked, "problems": problems, "ok": not problems}


# ---------------------------------------------------------------------------
# 쓰기 — Slack 연결
# ---------------------------------------------------------------------------

def _encrypt_pair(bot_token: str, app_token: str) -> dict:
    """토큰 쌍을 암호문+mask 로. **평문은 여기서 끝난다.**

    한쪽만 받지 않는다 — 봇 토큰만 바꾸면 Socket 은 옛 앱 토큰으로 열리고, 그
    조합이 맞는지 아무도 확인하지 않은 상태가 된다.
    """
    try:
        bot = _validate_token(bot_token, "xoxb-", "봇 토큰")
        app = _validate_token(app_token, "xapp-", "앱 토큰")
    except WorkspaceStoreError as exc:
        # 형식 오류는 **사람이 고칠 수 있는 것**이다(§7.5 → 422). 저장소 오류로
        # 흘려보내면 503 이 되고, 사람은 서버가 죽은 줄 안다.
        raise BotAdminRefused(str(exc)) from exc
    cipher = _fernet()
    return {
        "bot": (cipher.encrypt(bot.encode("utf-8")), _mask(bot)),
        "app": (cipher.encrypt(app.encode("utf-8")), _mask(app)),
    }


def mask_summary(secrets: dict) -> str:
    """감사에 남길 값. **가린 것만** 남는다(§7.5)."""
    return " ".join(f"{kind}={mask}" for kind, (_, mask) in sorted(secrets.items()))


def save_slack_connection(
    workspace: str,
    bot_key: str,
    *,
    actor: Actor,
    bot_token: str = "",
    app_token: str = "",
    note: str = "",
    repo: BotRepo | None = None,
) -> None:
    """Slack 연결을 만들거나 토큰을 교체한다. **켜지 않는다.**

    `slack_connectable` 이 아닌 봇에는 만들지 않는다 — 전문 봇이 Slack 에 직접
    붙으면 우리가 권한을 판정할 자리가 사라진다(CLAUDE.md 봇 체계).
    """
    store = repo or default_repo()
    bot = next((row for row in store.catalog() if str(row["key"]) == bot_key), None)
    if bot is None:
        raise BotAdminRefused(f"등록되지 않은 봇입니다: {bot_key}")
    if not bot.get("slack_connectable"):
        raise BotAdminRefused(
            f"{bot.get('display_name') or bot_key} 은 Slack 에 직접 붙지 않습니다."
            " Master 내부 호출로만 부릅니다."
        )
    if bool(bot_token) != bool(app_token):
        raise BotAdminRefused("토큰을 교체할 때는 봇 토큰과 앱 토큰을 함께 입력하세요.")

    secrets = _encrypt_pair(bot_token, app_token) if bot_token else {}
    with store.transaction() as tx:
        connection_id = tx.save_connection({
            "workspace": workspace, "bot_key": bot_key,
            "note": note or actor.reason, "actor": actor.name,
        })
        if not secrets:
            return
        tx.save_secrets(connection_id, secrets, actor.name)
        # 토큰이 바뀌면 이전 신원 검사 결과는 이 토큰의 것이 아니다.
        tx.reset_identity(connection_id)
        tx.add_audit({
            "actor": actor.name, "subject": SUBJECT_CONNECTION,
            "workspace": workspace,
            "field": f"connection.{bot_key}.slack_socket.token",
            "old_value": "", "new_value": mask_summary(secrets),
            "reason": actor.reason,
        })


def identity_problem(
    workspace: str,
    bot_key: str,
    *,
    team_id: str,
    bot_user_id: str,
    repo: BotRepo | None = None,
) -> str:
    """이 토큰이 **이 워크스페이스의 이 봇** 것인가. 아니면 사유.

    토큰을 잘못 붙이면 다른 워크스페이스에 수집한다. 그건 오류가 아니라 유출이고,
    붙일 때 확인하지 않으면 한참 뒤에 발견된다 — 그때는 이미 남의 대화가 우리
    아카이브에 들어와 있다.
    """
    if not team_id or not bot_user_id:
        return "Slack 이 team_id 또는 bot_user_id 를 주지 않았습니다"
    store = repo or default_repo()
    others = [
        row for row in store.connections(workspace)
        if str(row["bot_key"]) != bot_key and str(row.get("state")) != "retired"
    ]
    known_team = next(
        (str(row["team_id"]) for row in others if str(row.get("team_id") or "")), ""
    )
    if known_team and team_id != known_team:
        return f"다른 워크스페이스의 토큰입니다: 기대 {known_team}, 실제 {team_id}"
    if any(str(row.get("bot_user_id") or "") == bot_user_id for row in others):
        # 같은 봇 사용자면 별도 앱이 아니라 **같은 앱을 두 번 등록**한 것이다.
        # 그 상태로 Socket Mode 를 두 곳에서 열면 이벤트를 양쪽이 받는다.
        return f"이미 다른 연결이 쓰는 봇 사용자입니다: {bot_user_id}"
    return ""


def record_identity(
    workspace: str,
    bot_key: str,
    *,
    team_id: str,
    bot_user_id: str,
    actor: Actor,
    repo: BotRepo | None = None,
) -> str:
    """신원 검사 결과를 적고, 통과했으면 켠다. 사유가 있으면 안 켜고 돌려준다."""
    store = repo or default_repo()
    with store.transaction() as tx:
        row = tx.connection(workspace, bot_key)
        if row is None:
            raise BotAdminRefused(
                f"등록되지 않은 연결이라 신원을 기록할 수 없습니다: {workspace}/{bot_key}"
            )
        problem = identity_problem(
            workspace, bot_key, team_id=team_id, bot_user_id=bot_user_id, repo=tx
        )
        tx.record_identity({
            "id": row["id"], "team_id": team_id, "bot_user_id": bot_user_id,
            "ok": not problem, "error": problem, "actor": actor.name,
        })
        tx.add_audit({
            "actor": actor.name, "subject": SUBJECT_CONNECTION, "workspace": workspace,
            "field": f"connection.{bot_key}.slack_socket.identity",
            "old_value": "", "new_value": "ok" if not problem else "refused",
            "reason": problem or actor.reason,
        })
        return problem


def set_connection_state(
    workspace: str,
    bot_key: str,
    state: str,
    *,
    actor: Actor,
    repo: BotRepo | None = None,
) -> None:
    """연결을 끄거나 그만 쓴다. **런타임은 안 건드린다.**

    PF 직접 연결을 중지해도 Hermes 런타임과 Master 내부 호출은 그대로 돈다(§0).
    그 둘을 한 손잡이로 묶으면, 직접 연결을 끄는 사람이 TY 답변까지 끄게 된다.
    """
    if state not in SETTABLE_STATES:
        raise BotAdminRefused(
            f"연결을 {state} 로 바꿀 수 없습니다. 켜는 것은 신원 검사를 통과할 때뿐입니다."
        )
    store = repo or default_repo()
    with store.transaction() as tx:
        row = tx.connection(workspace, bot_key)
        if row is None:
            raise BotAdminRefused(f"없는 연결입니다: {workspace}/{bot_key}")
        tx.set_connection_state(int(row["id"]), state, actor.name)
        tx.add_audit({
            "actor": actor.name, "subject": SUBJECT_CONNECTION, "workspace": workspace,
            "field": f"connection.{bot_key}.slack_socket.state",
            "old_value": str(row.get("state") or ""), "new_value": state,
            "reason": actor.reason,
        })


# ---------------------------------------------------------------------------
# 쓰기 — Master 내부 호출 라우트
# ---------------------------------------------------------------------------

def set_route(
    workspace: str,
    bot_key: str,
    mode: str,
    *,
    actor: Actor,
    fallback_bot_key: str = "master",
    repo: BotRepo | None = None,
) -> None:
    """내부 호출을 끄거나, 그림자로 돌리거나, 실제로 쓴다.

    `active` 는 **런타임이 건강할 때만** 준다. 죽은 런타임으로 라우트를 켜면 그
    워크스페이스의 질문이 전부 대기하다 fallback 으로 떨어진다 — 사람에게는
    「봇이 느려졌다」 로 보이고, 원인은 화면 어디에도 없다.

    `shadow` 는 막지 않는다. 결과를 사용자에게 전달하지 않기 때문이다(§7.3).
    """
    if mode not in ROUTE_MODES:
        raise BotAdminRefused(f"모르는 라우트 모드입니다: {mode}")
    store = repo or default_repo()
    bot = next((row for row in store.catalog() if str(row["key"]) == bot_key), None)
    if bot is None:
        raise BotAdminRefused(f"등록되지 않은 봇입니다: {bot_key}")
    if not bot.get("internally_invokable"):
        raise BotAdminRefused(
            f"{bot.get('display_name') or bot_key} 은 Master 내부 호출 대상이 아닙니다."
        )

    with store.transaction() as tx:
        row = tx.route(bot_key, workspace)
        if row is None:
            raise BotAdminRefused(
                f"{workspace} 에 배정되지 않은 봇입니다. 배정을 먼저 하세요: {bot_key}"
            )
        if mode != "disabled" and not row.get("assigned"):
            # 배정이 없으면 라우터가 그 봇을 못 찾는다. 켠 것처럼 보이는데 아무
            # 질문도 가지 않는 상태가 제일 오래 간다.
            raise BotAdminRefused(
                f"{workspace} 에 배정되지 않은 봇은 켤 수 없습니다: {bot_key}"
            )
        if mode == "active":
            runtime = next(
                (r for r in tx.specialists() if str(r["key"]) == bot_key), None
            )
            problem = _runtime_blocker(runtime)
            if problem:
                raise BotAdminRefused(problem)
        tx.save_route({
            "specialist": bot_key, "workspace": workspace, "route_mode": mode,
            "fallback_bot_key": fallback_bot_key or "master", "actor": actor.name,
        })
        tx.add_audit({
            "actor": actor.name, "subject": SUBJECT_ROUTE, "workspace": workspace,
            "field": f"route.{bot_key}.mode",
            "old_value": str(row.get("route_mode") or "disabled"), "new_value": mode,
            "reason": actor.reason,
        })


def _runtime_blocker(runtime: dict | None) -> str:
    """`active` 를 막을 이유. 없으면 빈 문자열."""
    if runtime is None:
        return "런타임이 등록되지 않은 봇은 실제 호출 대상으로 켤 수 없습니다."
    if str(runtime.get("state")) != "enabled":
        return (
            "런타임이 켜져 있지 않습니다"
            f"(현재 {runtime.get('state')}). 먼저 런타임을 켜세요."
        )
    if str(runtime.get("health")) != HEALTHY:
        return (
            f"런타임 health 가 {runtime.get('health')} 입니다."
            " 그림자로 먼저 대조하세요."
        )
    return ""
