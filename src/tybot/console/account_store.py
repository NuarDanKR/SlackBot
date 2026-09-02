"""관리 콘솔 사용자 DB 저장소.

비밀번호 해시는 이 모듈 밖으로 반환하지 않는다. 마지막 관리자 보호와 권한 변경은
같은 트랜잭션에서 검사해 동시 요청으로 관리자 계정이 모두 사라지지 않게 한다.
"""
from __future__ import annotations

import os

from .auth import ADMIN, MIN_PASSWORD_LENGTH, ROLES, hash_password


class AccountStoreError(RuntimeError):
    """사용자 관리 요청을 안전하게 적용할 수 없다."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise AccountStoreError("DATABASE_URL이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as e:
        raise AccountStoreError(f"콘솔 사용자 DB 연결 실패: {e}") from e


def list_users() -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('console_user_admin_guard'))")
            cur.execute(
                """
                SELECT u.email, u.name, u.role, u.active, u.created_at, u.last_seen,
                       coalesce(array_agg(cuw.workspace ORDER BY cuw.workspace) FILTER (
                           WHERE cuw.workspace IS NOT NULL
                       ), '{}') AS workspaces
                  FROM console_user u
                  LEFT JOIN console_user_workspace cuw ON cuw.email = u.email
                 GROUP BY u.email, u.name, u.role, u.active, u.created_at, u.last_seen
                 ORDER BY u.active DESC, u.role, u.email
                """
            )
            return [dict(row) for row in cur.fetchall()]
    except AccountStoreError:
        raise
    except Exception as e:
        raise AccountStoreError(f"콘솔 사용자 조회 실패: {e}") from e


def save_user(
    *,
    actor_email: str,
    email: str,
    name: str,
    role: str,
    active: bool,
    workspaces: list[str],
    password: str | None,
) -> None:
    email = email.strip().lower()
    actor_email = actor_email.strip().lower()
    if "@" not in email or role not in ROLES:
        raise AccountStoreError("이메일 또는 역할 값이 올바르지 않습니다.")
    if password is not None and len(password) < MIN_PASSWORD_LENGTH:
        raise AccountStoreError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    if email == actor_email and (not active or role != ADMIN):
        raise AccountStoreError("현재 로그인한 관리자 자신의 권한을 낮추거나 비활성화할 수 없습니다.")

    try:
        with _connect() as conn, conn.cursor() as cur:
            # 관리자 변경을 직렬화한다. 서로 다른 관리자 두 명을 동시에 강등하는
            # 요청이 각각 "다른 관리자가 남아 있다"고 판단하는 경쟁을 막는다.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('console_user_admin_guard'))")
            cur.execute(
                "SELECT email, role, active, password_hash "
                "FROM console_user WHERE email = %s FOR UPDATE",
                (email,),
            )
            current = cur.fetchone()
            if (current is None or not current["password_hash"]) and password is None:
                raise AccountStoreError("새 사용자 또는 미이관 계정에는 초기 비밀번호가 필요합니다.")

            removes_admin = bool(
                current
                and current["role"] == ADMIN
                and current["active"]
                and (role != ADMIN or not active)
            )
            if removes_admin:
                cur.execute(
                    "SELECT count(*) AS count FROM console_user "
                    "WHERE role = 'admin' AND active"
                )
                if int(cur.fetchone()["count"]) <= 1:
                    raise AccountStoreError("마지막 활성 관리자는 권한을 낮추거나 비활성화할 수 없습니다.")

            password_hash = (
                hash_password(password)
                if password is not None
                else str(current["password_hash"])
            )
            cur.execute(
                """
                INSERT INTO console_user (email, name, password_hash, role, active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    name = excluded.name,
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    active = excluded.active
                """,
                (email, name.strip() or email.split("@")[0], password_hash, role, active),
            )
            cur.execute("DELETE FROM console_user_workspace WHERE email = %s", (email,))
            if role != ADMIN:
                for workspace in sorted({ws.strip() for ws in workspaces if ws.strip()}):
                    cur.execute(
                        "INSERT INTO console_user_workspace (email, workspace) VALUES (%s, %s)",
                        (email, workspace),
                    )
    except AccountStoreError:
        raise
    except Exception as e:
        raise AccountStoreError(f"콘솔 사용자 저장 실패: {e}") from e
