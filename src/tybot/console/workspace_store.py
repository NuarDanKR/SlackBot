"""PostgreSQL-backed workspace registry.

Slack tokens are encrypted before they enter PostgreSQL.  The console can list
only masks; plaintext is returned solely to the bot's startup loader.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
logger = logging.getLogger("tybot.console.workspace_store")


class WorkspaceStoreError(RuntimeError):
    """A workspace registry operation could not be applied safely."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise WorkspaceStoreError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise WorkspaceStoreError(f"워크스페이스 DB 연결 실패: {exc}") from exc


def _secret_key() -> bytes:
    raw = os.getenv("WORKSPACE_SECRET_KEY", "").strip()
    if not raw:
        credentials = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
        explicit = os.getenv("WORKSPACE_SECRET_KEY_FILE", "").strip()
        path = Path(explicit) if explicit else Path(credentials) / "tybot-workspace-secret-key"
        if explicit or credentials:
            try:
                if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise WorkspaceStoreError(
                        f"워크스페이스 암호화 키 권한이 너무 넓습니다(0400 필요): {path}"
                    )
                raw = path.read_text(encoding="ascii").strip()
            except OSError as exc:
                raise WorkspaceStoreError(f"워크스페이스 암호화 키를 읽지 못했습니다: {path}") from exc
    if not raw:
        raise WorkspaceStoreError(
            "WORKSPACE_SECRET_KEY 또는 WORKSPACE_SECRET_KEY_FILE이 필요합니다."
        )
    return raw.encode("ascii")


def _fernet():
    try:
        from cryptography.fernet import Fernet

        return Fernet(_secret_key())
    except WorkspaceStoreError:
        raise
    except ImportError as exc:
        raise WorkspaceStoreError("cryptography가 없습니다. 콘솔 의존성을 다시 설치하세요.") from exc
    except (ValueError, UnicodeError) as exc:
        raise WorkspaceStoreError("워크스페이스 암호화 키 형식이 올바르지 않습니다.") from exc


def _mask(token: str) -> str:
    if len(token) <= 13:
        return f"{token[:5]}…"
    return f"{token[:9]}…{token[-4:]}"


def _validate_token(token: str, prefix: str, label: str) -> str:
    value = token.strip()
    if not value.startswith(prefix) or len(value) < len(prefix) + 8:
        raise WorkspaceStoreError(f"{label} 형식이 올바르지 않습니다.")
    return value


def list_workspaces() -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.key, w.label, w.role, w.state, w.error, w.limit_usd,
                       w.archive_path, w.created_at, w.created_by,
                       coalesce(array_agg(DISTINCT wr.target ORDER BY wr.target) FILTER (
                           WHERE wr.target IS NOT NULL
                       ), '{}') AS readable,
                       max(ws.mask) FILTER (WHERE ws.kind = 'bot') AS bot_token_mask,
                       max(ws.mask) FILTER (WHERE ws.kind = 'app') AS app_token_mask,
                       max(ws.updated_at) AS secret_updated_at,
                       max(ws.updated_by) FILTER (
                           WHERE ws.updated_at = latest.latest_at
                       ) AS secret_updated_by
                  FROM workspace w
                  LEFT JOIN workspace_readable wr ON wr.reader = w.key
                  LEFT JOIN workspace_secret ws ON ws.workspace = w.key
                  LEFT JOIN LATERAL (
                       SELECT max(updated_at) AS latest_at
                         FROM workspace_secret x
                        WHERE x.workspace = w.key
                  ) latest ON true
                 GROUP BY w.key, w.label, w.role, w.state, w.error, w.limit_usd,
                          w.archive_path, w.created_at, w.created_by
                 ORDER BY w.label, w.key
                """
            )
            rows = []
            for row in cur.fetchall():
                item = dict(row)
                item["key"] = str(item["key"]).lower()
                item["readable"] = [str(value).lower() for value in item["readable"]]
                rows.append(item)
            return rows
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"워크스페이스 조회 실패: {exc}") from exc


def save_workspace(
    *,
    actor: str,
    key: str,
    label: str,
    role: str,
    state: str,
    limit_usd: float,
    readable: list[str],
    bot_token: str | None,
    app_token: str | None,
) -> None:
    key = key.strip().lower()
    label = label.strip()
    if not KEY_RE.fullmatch(key):
        raise WorkspaceStoreError("키는 영문 소문자로 시작하는 2~24자의 소문자·숫자·하이픈이어야 합니다.")
    if not label or len(label) > 80 or "\n" in label or "\r" in label:
        raise WorkspaceStoreError("표시 이름은 1~80자의 한 줄이어야 합니다.")
    if role not in {"root", "member"} or state not in {"enabled", "disabled"}:
        raise WorkspaceStoreError("워크스페이스 등급 또는 상태가 올바르지 않습니다.")
    if limit_usd < 0 or limit_usd > 10000:
        raise WorkspaceStoreError("하루 사용 상한은 0~10000달러여야 합니다.")
    targets = sorted({target.strip().lower() for target in readable if target.strip()})
    if key in targets:
        raise WorkspaceStoreError("자기 자신을 크로스 열람 대상으로 지정할 수 없습니다.")
    if (bot_token is None) != (app_token is None):
        raise WorkspaceStoreError("토큰을 교체할 때는 봇 토큰과 앱 토큰을 함께 입력하세요.")

    encrypted: dict[str, tuple[bytes, str]] = {}
    if bot_token is not None and app_token is not None:
        bot = _validate_token(bot_token, "xoxb-", "봇 토큰")
        app = _validate_token(app_token, "xapp-", "앱 토큰")
        cipher = _fernet()
        encrypted = {
            "bot": (cipher.encrypt(bot.encode("utf-8")), _mask(bot)),
            "app": (cipher.encrypt(app.encode("utf-8")), _mask(app)),
        }

    archive_root = Path(os.getenv("ARCHIVE_DIR", "/var/lib/tybot/archive"))
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('workspace_registry'))")
            cur.execute("SELECT key FROM workspace WHERE lower(key) = %s FOR UPDATE", (key,))
            existing = cur.fetchone()
            exists = existing is not None
            db_key = str(existing["key"]) if existing else key
            if not exists and not encrypted:
                raise WorkspaceStoreError("새 워크스페이스에는 봇 토큰과 앱 토큰이 필요합니다.")
            if exists and not encrypted:
                cur.execute(
                    "SELECT count(*) AS count FROM workspace_secret WHERE workspace = %s",
                    (db_key,),
                )
                if int(cur.fetchone()["count"]) < 2:
                    suffix = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
                    env_bot = os.getenv(f"SLACK_BOT_TOKEN_{suffix}")
                    env_app = os.getenv(f"SLACK_APP_TOKEN_{suffix}")
                    if not env_bot or not env_app:
                        raise WorkspaceStoreError(
                            "DB에 저장된 토큰이 없습니다. 봇 토큰과 앱 토큰을 입력하세요."
                        )
                    bot = _validate_token(env_bot, "xoxb-", "기존 봇 토큰")
                    app = _validate_token(env_app, "xapp-", "기존 앱 토큰")
                    cipher = _fernet()
                    encrypted = {
                        "bot": (cipher.encrypt(bot.encode("utf-8")), _mask(bot)),
                        "app": (cipher.encrypt(app.encode("utf-8")), _mask(app)),
                    }
            if targets:
                cur.execute("SELECT key FROM workspace WHERE lower(key) = any(%s)", (targets,))
                target_keys = {str(row["key"]).lower(): str(row["key"]) for row in cur.fetchall()}
                unknown = sorted(set(targets) - set(target_keys))
                if unknown:
                    raise WorkspaceStoreError(f"알 수 없는 열람 대상입니다: {', '.join(unknown)}")
            cur.execute(
                """
                INSERT INTO workspace
                    (key, label, role, state, error, limit_usd, archive_path, created_by)
                VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    label = excluded.label,
                    role = excluded.role,
                    state = excluded.state,
                    error = NULL,
                    limit_usd = excluded.limit_usd
                """,
                (db_key, label, role, state, limit_usd, str(archive_root / key), actor),
            )
            cur.execute("DELETE FROM workspace_readable WHERE reader = %s", (db_key,))
            for target in targets:
                cur.execute(
                    "INSERT INTO workspace_readable (reader, target) VALUES (%s, %s)",
                    (db_key, target_keys[target]),
                )
            for kind, (ciphertext, mask) in encrypted.items():
                cur.execute(
                    """
                    INSERT INTO workspace_secret
                        (workspace, kind, ciphertext, mask, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (workspace, kind) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        mask = excluded.mask,
                        updated_at = now(),
                        updated_by = excluded.updated_by
                    """,
                    (db_key, kind, ciphertext, mask, actor),
                )
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"워크스페이스 저장 실패: {exc}") from exc


def runtime_workspaces() -> list[dict]:
    """Return registry rows for bot startup, decrypting only complete token pairs."""
    from cryptography.fernet import InvalidToken

    cipher = _fernet()
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.key, w.label, w.role, w.state,
                       bot.ciphertext AS bot_token, app.ciphertext AS app_token,
                       coalesce(array_agg(wr.target ORDER BY wr.target) FILTER (
                           WHERE wr.target IS NOT NULL
                       ), '{}') AS readable
                  FROM workspace w
                  LEFT JOIN workspace_secret bot
                    ON bot.workspace = w.key AND bot.kind = 'bot'
                  LEFT JOIN workspace_secret app
                    ON app.workspace = w.key AND app.kind = 'app'
                  LEFT JOIN workspace_readable wr ON wr.reader = w.key
                 GROUP BY w.key, w.label, w.role, w.state, bot.ciphertext, app.ciphertext
                 ORDER BY w.key
                """
            )
            rows = []
            for row in cur.fetchall():
                item = dict(row)
                item["key"] = str(item["key"]).lower()
                item["readable"] = [str(value).lower() for value in item["readable"]]
                if item["bot_token"] is not None and item["app_token"] is not None:
                    try:
                        item["bot_token"] = cipher.decrypt(bytes(item["bot_token"])).decode()
                        item["app_token"] = cipher.decrypt(bytes(item["app_token"])).decode()
                    except (InvalidToken, UnicodeDecodeError) as exc:
                        logger.error(
                            "%s 토큰을 현재 암호화 키로 복호화하지 못해 제외합니다: %s",
                            item["key"],
                            exc,
                        )
                        continue
                rows.append(item)
            return rows
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"워크스페이스 기동 설정 조회 실패: {exc}") from exc


def record_runtime_result(key: str, error: str | None) -> None:
    """Record one DB-managed bot's connection result without exposing secrets."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            if error:
                cur.execute(
                    "UPDATE workspace SET state = 'error', error = %s WHERE lower(key) = lower(%s)",
                    (error[:500], key),
                )
            else:
                cur.execute(
                    "UPDATE workspace SET state = 'enabled', error = NULL "
                    "WHERE lower(key) = lower(%s) AND state = 'error'",
                    (key,),
                )
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"워크스페이스 연결 상태 기록 실패: {exc}") from exc
