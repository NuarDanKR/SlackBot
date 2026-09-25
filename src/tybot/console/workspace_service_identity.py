"""Verify a stored Slack service token without returning its plaintext."""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from .workspace_service_store import Identity, Service, ServiceStoreError, record_identity
from .workspace_store import _connect, _fernet


def verify_service(
    workspace: str,
    service: Service | str,
    *,
    actor: str,
    client_factory=None,
) -> str:
    """Verify the stored bot/app token pair and persist the bot identity."""
    name = Service(service)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT sec.kind, sec.ciphertext
                  FROM workspace_service s
                  JOIN workspace_service_secret sec
                    ON sec.workspace = s.workspace AND sec.service = s.service
                   AND sec.kind IN ('bot', 'app')
                 WHERE s.workspace = %s AND s.service = %s
                """,
                (workspace, str(name)),
            )
            rows = cur.fetchall()
        encrypted = {str(row["kind"]): bytes(row["ciphertext"]) for row in rows}
        if set(encrypted) != {"bot", "app"}:
            raise ServiceStoreError("신원을 확인할 봇/앱 토큰 쌍이 등록되지 않았습니다.")
        cipher = _fernet()
        bot_token = cipher.decrypt(encrypted["bot"]).decode("utf-8")
        app_token = cipher.decrypt(encrypted["app"]).decode("utf-8")
    except ServiceStoreError:
        raise
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ServiceStoreError("저장된 Slack 토큰을 현재 키로 읽지 못했습니다.") from exc
    except Exception as exc:
        raise ServiceStoreError(f"서비스 토큰 조회 실패: {type(exc).__name__}") from exc

    if client_factory is None:
        from slack_sdk import WebClient

        client_factory = WebClient
    try:
        result = client_factory(token=bot_token).auth_test()
        client_factory(token=app_token).apps_connections_open()
    except Exception as exc:
        raise ServiceStoreError(f"Slack 토큰 쌍 확인 실패: {type(exc).__name__}") from exc
    finally:
        bot_token = ""
        app_token = ""

    return record_identity(
        workspace,
        name,
        Identity(
            team_id=str(result.get("team_id") or ""),
            bot_user_id=str(result.get("user_id") or ""),
        ),
        actor=actor,
    )
