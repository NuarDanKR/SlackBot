"""Load the Archiver's own startup configuration from PostgreSQL."""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from .console.workspace_store import WorkspaceStoreError, _connect, _fernet


class ArchiverRuntimeStoreError(WorkspaceStoreError):
    pass


def load_runtime_config(workspace: str) -> dict:
    """Return one verified Archiver config; never log or persist plaintext tokens."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM archiver_runtime_config(%s)", (workspace,))
            row = cur.fetchone()
            if not row:
                raise ArchiverRuntimeStoreError(
                    f"활성화되고 신원이 확인된 Archiver 서비스가 없습니다: {workspace}"
                )
            if not str(row.get("master_bot_user_id") or ""):
                raise ArchiverRuntimeStoreError(
                    f"신원이 확인된 Master 서비스가 없습니다: {workspace}"
                )
            cur.execute(
                """
                SELECT channel_id
                  FROM archive_channel_mode
                 WHERE workspace = %s AND mode IN ('shadow', 'active')
                 ORDER BY channel_id
                """,
                (workspace,),
            )
            channel_ids = [str(item["channel_id"]) for item in cur.fetchall()]
            cur.execute(
                """
                SELECT name, enabled
                  FROM archive_feature_flag
                 WHERE scope = 'global'
                   AND name IN ('separate_attachments', 'attachment_reader_ready')
                """
            )
            flags = {str(item["name"]): bool(item["enabled"]) for item in cur.fetchall()}
        cipher = _fernet()
        result = dict(row)
        result["bot_token"] = cipher.decrypt(bytes(result.pop("bot_ciphertext"))).decode()
        result["app_token"] = cipher.decrypt(bytes(result.pop("app_ciphertext"))).decode()
        result["channel_ids"] = channel_ids
        result["separate_attachments"] = flags.get("separate_attachments", False)
        if result["separate_attachments"] and not flags.get(
            "attachment_reader_ready", False
        ):
            raise ArchiverRuntimeStoreError(
                "첨부 reader 준비 없이 첨부 분리가 켜져 있어 기동하지 않습니다."
            )
        return result
    except ArchiverRuntimeStoreError:
        raise
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ArchiverRuntimeStoreError(
            "Archiver 토큰을 현재 암호화 키로 읽지 못했습니다."
        ) from exc
    except Exception as exc:
        raise ArchiverRuntimeStoreError(
            f"Archiver 기동 설정 조회 실패: {type(exc).__name__}"
        ) from exc
