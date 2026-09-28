"""저장된 Slack 토큰 쌍을 확인하고 **신원만** 적는다.

설계: `docs/design/workspace-service-console-redesign.md` §6.2·§7.2 (2026-09-28)

## 여기가 암호문을 읽는 유일한 자리다

`bot_repo` 는 mask 만 고른다. 복호화가 필요한 곳은 **검증 한 곳뿐**이고, 그
한 곳을 파일 하나로 몰아 둔다. 여러 곳에서 복호화하면 언젠가 그중 하나가
로그를 찍고, 로그는 감사보다 오래 남는다.

평문은 이 함수 안에서 끝난다 — 예외 문구에도 반환값에도 안 들어간다. Slack 호출이
실패하면 예외 **종류만** 적는다(`type(exc).__name__`). 토큰이 잘못됐다는 사실은
남기되 토큰은 남기지 않는다.

## 왜 앱 토큰까지 확인하나

Socket Mode 는 앱 토큰으로 열린다. 봇 토큰만 맞으면 화면은 정상인데 이벤트가
하나도 안 온다 — 「붙었다고 나오는데 아무 일도 안 일어나는」 상태가 제일 오래 간다.
"""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from . import bot_admin
from .bot_repo import BotRepo, default_repo
from .workspace_store import WorkspaceStoreError, _connect, _fernet


class SlackUnavailable(WorkspaceStoreError):
    """Slack 이 답하지 않았다. **저장은 그대로다**(§7.5 → 503)."""


def _tokens(workspace: str, bot_key: str) -> tuple[str, str]:
    """이 연결의 봇·앱 토큰. 쌍이 아니면 거절한다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.kind, s.ciphertext
              FROM bot_connection c
              JOIN bot_connection_secret s ON s.connection_id = c.id
             WHERE c.workspace = %s AND c.bot_key = %s
               AND c.connector_type = 'slack_socket'
               AND s.kind IN ('bot', 'app')
            """,
            (workspace, bot_key),
        )
        rows = cur.fetchall()
    encrypted = {str(row["kind"]): bytes(row["ciphertext"]) for row in rows}
    if set(encrypted) != {"bot", "app"}:
        raise bot_admin.BotAdminRefused(
            "신원을 확인할 봇/앱 토큰 쌍이 등록되지 않았습니다."
        )
    try:
        cipher = _fernet()
        return (
            cipher.decrypt(encrypted["bot"]).decode("utf-8"),
            cipher.decrypt(encrypted["app"]).decode("utf-8"),
        )
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise bot_admin.BotAdminRefused(
            "저장된 Slack 토큰을 현재 키로 읽지 못했습니다."
        ) from exc


def verify_connection(
    workspace: str,
    bot_key: str,
    *,
    actor: bot_admin.Actor,
    client_factory=None,
    repo: BotRepo | None = None,
) -> str:
    """Slack 에 물어 신원을 적는다. 통과하면 연결이 켜지고, 아니면 사유를 돌려준다."""
    bot_token, app_token = _tokens(workspace, bot_key)
    if client_factory is None:
        from slack_sdk import WebClient

        client_factory = WebClient
    try:
        result = client_factory(token=bot_token).auth_test()
        client_factory(token=app_token).apps_connections_open()
    except Exception as exc:
        raise SlackUnavailable(
            f"Slack 토큰 쌍 확인 실패: {type(exc).__name__}."
        ) from exc
    finally:
        bot_token = ""
        app_token = ""

    return bot_admin.record_identity(
        workspace, bot_key,
        team_id=str(result.get("team_id") or ""),
        bot_user_id=str(result.get("user_id") or ""),
        actor=actor,
        repo=repo or default_repo(),
    )
