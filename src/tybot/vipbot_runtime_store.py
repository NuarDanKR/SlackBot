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
                            'public.vipbot_connection_config(text)',
                            'EXECUTE'
                          ) AS may_execute,
                          has_function_privilege(
                            current_user,
                            'public.vipbot_authorization(text,text)',
                            'EXECUTE'
                          ) AS may_authorize,
                          EXISTS (
                            SELECT 1
                              FROM unnest(ARRAY[
                                'public.bot_connection',
                                'public.bot_connection_secret',
                                'public.employee',
                                'public.user_identity',
                                'public.bot_access',
                                'public.bot_source_channel'
                              ]) AS tables(name)
                              CROSS JOIN unnest(ARRAY[
                                'SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                'TRUNCATE', 'REFERENCES', 'TRIGGER'
                              ]) AS privileges(name)
                             WHERE has_table_privilege(
                               current_user, tables.name, privileges.name
                             )
                          ) AS may_access_base_tables,
                          EXISTS (
                            SELECT 1
                              FROM pg_catalog.pg_proc p
                              JOIN pg_catalog.pg_namespace n
                                ON n.oid = p.pronamespace
                             WHERE n.nspname = 'public'
                               AND p.prosecdef
                               AND p.oid NOT IN (
                                 'public.vipbot_connection_config(text)'::regprocedure,
                                 'public.vipbot_authorization(text,text)'::regprocedure
                               )
                               AND has_function_privilege(
                                 current_user, p.oid, 'EXECUTE'
                               )
                          ) AS may_execute_other_definers"""
            )
            identity = cur.fetchone()
    except psycopg.Error as exc:
        error = f"VIPBot DB 권한 확인 실패: {type(exc).__name__}"
    if error:
        connection.close()
        raise VipbotRuntimeStoreError(error) from None
    if (
        not isinstance(identity, dict)
        or identity.get("role") != "tybot_vipbot_runtime"
        or identity.get("may_execute") is not True
        or identity.get("may_authorize") is not True
        or identity.get("may_access_base_tables") is not False
        or identity.get("may_execute_other_definers") is not False
    ):
        connection.close()
        raise VipbotRuntimeStoreError("vipbot_database_privilege_mismatch")
    return connection


def connect_runtime():
    """Open a connection only after verifying the dedicated runtime capability."""
    return _connect()


def _try_load_runtime_config(workspace: str) -> tuple[dict | None, str]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM public.vipbot_connection_config(%s)", (workspace,)
            )
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
