"""Load the VIPBot Slack connection without exposing other bot secrets."""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from .console.workspace_store import WorkspaceStoreError, _fernet
from .vipbot_dsn import VipbotDsnError, resolve_vipbot_database_url


class VipbotRuntimeStoreError(WorkspaceStoreError):
    pass


def _connect():
    try:
        import psycopg
    except ImportError:
        raise VipbotRuntimeStoreError("VIPBot DB 드라이버가 없습니다.") from None
    connection = None
    error = ""
    try:
        connection = psycopg.connect(
            resolve_vipbot_database_url(), row_factory=psycopg.rows.dict_row
        )
    except VipbotDsnError as exc:
        error = exc.code
    except psycopg.Error as exc:
        error = f"VIPBot DB 연결 실패: {type(exc).__name__}"
    if error:
        raise VipbotRuntimeStoreError(error) from None

    try:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT current_user AS role,
                          has_function_privilege(
                            current_user,
                            'vipbot_connection_config(text)',
                            'EXECUTE'
                          ) AS may_execute,
                          has_table_privilege(
                            current_user, 'bot_connection', 'SELECT'
                          ) AS may_read_connection,
                          has_table_privilege(
                            current_user, 'bot_connection_secret', 'SELECT'
                          ) AS may_read_secret"""
            )
            identity = cur.fetchone()
    except psycopg.Error as exc:
        error = f"VIPBot DB 권한 확인 실패: {type(exc).__name__}"
    if error:
        connection.close()
        raise VipbotRuntimeStoreError(error) from None
    if (
        not identity
        or identity["role"] != "tybot_vipbot_runtime"
        or identity["may_execute"] is not True
        or identity["may_read_connection"] is not False
        or identity["may_read_secret"] is not False
    ):
        connection.close()
        raise VipbotRuntimeStoreError("vipbot_database_privilege_mismatch")
    return connection


def _try_load_runtime_config(workspace: str) -> tuple[dict | None, str]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM vipbot_connection_config(%s)", (workspace,))
            row = cur.fetchone()
        if not row:
            raise VipbotRuntimeStoreError(
                f"활성화되고 신원이 확인된 VIPBot 연결이 없습니다: {workspace}"
            )
        cipher = _fernet()
        result = dict(row)
        result["bot_token"] = cipher.decrypt(
            bytes(result.pop("bot_ciphertext"))
        ).decode()
        result["app_token"] = cipher.decrypt(
            bytes(result.pop("app_ciphertext"))
        ).decode()
        return result, ""
    except VipbotRuntimeStoreError as exc:
        return None, str(exc)
    except WorkspaceStoreError:
        return None, "VIPBot 암호화 키를 읽지 못했습니다."
    except (InvalidToken, UnicodeDecodeError):
        return None, "VIPBot 토큰을 현재 암호화 키로 읽지 못했습니다."
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"VIPBot 기동 설정 조회 실패: {type(exc).__name__}"


def load_runtime_config(workspace: str) -> dict:
    """Return one verified VIPBot connection with its decrypted token pair."""
    result, error = _try_load_runtime_config(workspace)
    if error:
        raise VipbotRuntimeStoreError(error) from None
    assert result is not None
    return result
