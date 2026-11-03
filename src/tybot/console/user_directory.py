"""콘솔 「사용자」 화면의 저장소 — 사람 목록과 봇 사용 권한.

사람 한 줄 = 사번 하나. 그 사람의 Slack 계정(워크스페이스별), 콘솔 계정, 봇 권한을
한 줄에 모은다. 판정 규칙은 여기서 다시 만들지 않고 `tybot.bot_access` 를 부른다.

## 하지 않는 것
- **사번 매핑을 만들거나 고치지 않는다.** `user_identity` 는 봇이 이메일로 검증해
  만든다(`identity.py`). 콘솔이 손으로 붙이면 근거 없는 매핑이 생긴다.
- **권한 줄을 지우지 않는다.** 회수만 한다(`revoked_*`).
- **조직·직책 단위 부여는 없다.** 사람 단위만(설계 결정 2026-11-03).

## 오류
`UserDirectoryError` 의 문자열은 코드다(`CODES`). 화면이 문구로 바꾼다. 예외 본문에
DB 메시지를 싣지 않는다 — 접속 문자열이 섞일 수 있다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import bot_access


class UserDirectoryError(RuntimeError):
    """요청을 적용할 수 없다. 문자열은 `CODES` 중 하나."""


#: 코드 → HTTP 상태. 라우트가 이 표로 바꾼다.
CODES: dict[str, int] = {
    "invalid_request": 422,
    "unknown_bot": 422,
    "expires_in_past": 422,
    "employee_not_found": 404,
    "employee_inactive": 422,
    "grant_not_found": 404,
    "already_granted": 409,
    "already_revoked": 409,
    "database_unavailable": 503,
}

VIEWS = ("slack", "granted", "all")
MAX_LIMIT = 200


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise UserDirectoryError("database_unavailable")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise UserDirectoryError("database_unavailable") from exc


def _text(value: str | None, *, limit: int) -> str:
    text = (value or "").strip()
    if not text or len(text) > limit:
        raise UserDirectoryError("invalid_request")
    return text


def _bot(bot: str) -> str:
    bot = (bot or "").strip().lower()
    if bot not in bot_access.BOTS:
        raise UserDirectoryError("unknown_bot")
    return bot


# ---------------------------------------------------------------------------
# 목록
# ---------------------------------------------------------------------------

_PEOPLE_FILTER = {
    "slack": """e.emp_no IN (SELECT ui.emp_no FROM user_identity ui
                              WHERE ui.emp_no IS NOT NULL
                                AND (%(ws)s::text IS NULL OR ui.workspace = %(ws)s))""",
    "granted": """e.emp_no IN (SELECT a.emp_no FROM bot_access a
                                WHERE a.revoked_at IS NULL
                                  AND (%(bot)s::text IS NULL OR a.bot = %(bot)s))""",
    "all": "e.active",
}

_PEOPLE_SQL = """
WITH people AS (
    SELECT e.emp_no, e.name, e.email, e.position, e.active, e.org_code,
           o.name AS org_name
      FROM employee e
      LEFT JOIN org_unit o ON o.code = e.org_code
     WHERE {filter}
       AND (%(q)s::text IS NULL
            OR e.name ILIKE %(like)s OR e.emp_no ILIKE %(like)s
            OR coalesce(e.email, '') ILIKE %(like)s
            OR coalesce(o.name, '') ILIKE %(like)s)
)
SELECT p.*,
       count(*) OVER () AS total,
       coalesce((
           SELECT json_agg(json_build_object(
                      'workspace', ui.workspace, 'slackUser', ui.slack_user,
                      'verifiedBy', ui.verified_by, 'verifiedAt', ui.verified_at,
                      -- NULL: 아직 계정 동기화가 읽지 않음. false: Slack 에서 지워짐
                      'live', sp.active)
                  ORDER BY ui.workspace)
             FROM user_identity ui
             LEFT JOIN slack_presence sp
                    ON sp.workspace = ui.workspace AND sp.slack_user = ui.slack_user
            WHERE ui.emp_no = p.emp_no), '[]') AS slack,
       (SELECT json_build_object('email', cu.email, 'role', cu.role,
                                 'active', cu.active, 'lastSeen', cu.last_seen,
                                 'managed', cu.managed_by IS NOT NULL,
                                 'stoppedAt', cu.stopped_at, 'stopReason', cu.stop_reason)
          FROM console_user cu
         WHERE p.email IS NOT NULL AND lower(cu.email) = lower(btrim(p.email))
         LIMIT 1) AS console,
       coalesce((
           SELECT json_agg(json_build_object(
                      'id', a.id, 'bot', a.bot, 'grantedAt', a.granted_at,
                      'grantedBy', a.granted_by, 'expiresAt', a.expires_at,
                      'reason', a.reason,
                      'live', a.expires_at IS NULL OR a.expires_at > now())
                  ORDER BY a.bot)
             FROM bot_access a
            WHERE a.emp_no = p.emp_no AND a.revoked_at IS NULL), '[]') AS grants
  FROM people p
 ORDER BY p.active DESC, p.org_name NULLS LAST, p.name, p.emp_no
 LIMIT %(limit)s OFFSET %(offset)s
"""

_UNMAPPED_SQL = """
SELECT count(*) AS n FROM user_identity
 WHERE emp_no IS NULL AND (%(ws)s::text IS NULL OR workspace = %(ws)s)
"""


def _person(row: dict) -> dict:
    return {
        "empNo": row["emp_no"],
        "name": row["name"],
        "email": row["email"],
        "position": row["position"],
        "active": row["active"],
        "orgCode": row["org_code"],
        "orgName": row["org_name"],
        "slack": row["slack"] or [],
        "console": row["console"],
        "grants": row["grants"] or [],
    }


def list_people(
    *,
    view: str = "slack",
    q: str | None = None,
    workspace: str | None = None,
    bot: str | None = None,
    limit: int = 50,
    offset: int = 0,
    connect=None,
) -> dict:
    """사람 목록. `view`: slack(Slack 사용자) · granted(권한 있는 사람) · all(재직자 전체)."""
    if view not in VIEWS or not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise UserDirectoryError("invalid_request")
    q = (q or "").strip() or None
    if q is not None and len(q) > 80:
        raise UserDirectoryError("invalid_request")
    workspace = (workspace or "").strip().lower() or None
    if bot is not None:
        bot = _bot(bot)
    params = {
        "q": q,
        "like": f"%{q}%" if q else None,
        "ws": workspace,
        "bot": bot,
        "limit": limit,
        "offset": offset,
    }
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(_PEOPLE_SQL.format(filter=_PEOPLE_FILTER[view]), params)
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute(_UNMAPPED_SQL, params)
            unmapped = int(cur.fetchone()["n"])
    except UserDirectoryError:
        raise
    except Exception as exc:
        raise UserDirectoryError("database_unavailable") from exc
    return {
        "view": view,
        "total": int(rows[0]["total"]) if rows else 0,
        "limit": limit,
        "offset": offset,
        "people": [_person(r) for r in rows],
        # 사번을 못 찾은 Slack 계정 수. 이 사람들은 목록에 없고, 권한도 받을 수 없다.
        "unmappedSlack": unmapped,
        "bots": [{"key": k, "label": v} for k, v in bot_access.BOTS.items()],
    }


# ---------------------------------------------------------------------------
# 권한 이력
# ---------------------------------------------------------------------------

_GRANT_COLUMNS = """
a.id, a.bot, a.emp_no, e.name, o.name AS org_name, e.position,
a.reason, a.granted_by, a.granted_at, a.expires_at,
a.revoked_at, a.revoked_by, a.revoke_reason
"""


def _grant(row: dict) -> dict:
    now = datetime.now(UTC)
    if row["revoked_at"] is not None:
        state = "revoked"
    elif row["expires_at"] is not None and row["expires_at"] <= now:
        state = "expired"
    else:
        state = "active"
    return {
        "id": row["id"],
        "bot": row["bot"],
        "empNo": row["emp_no"],
        "name": row.get("name"),
        "orgName": row.get("org_name"),
        "position": row.get("position"),
        "reason": row["reason"],
        "grantedBy": row["granted_by"],
        "grantedAt": row["granted_at"],
        "expiresAt": row["expires_at"],
        "revokedAt": row["revoked_at"],
        "revokedBy": row["revoked_by"],
        "revokeReason": row["revoke_reason"],
        "state": state,
    }


def list_grants(*, bot: str, emp_no: str | None = None, limit: int = 200,
                connect=None) -> dict:
    """한 봇의 부여·회수 이력(최근 순). 회수된 줄도 나온다."""
    bot = _bot(bot)
    if not 1 <= limit <= 500:
        raise UserDirectoryError("invalid_request")
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT {_GRANT_COLUMNS}
                      FROM bot_access a
                      JOIN employee e ON e.emp_no = a.emp_no
                      LEFT JOIN org_unit o ON o.code = e.org_code
                     WHERE a.bot = %s AND (%s::text IS NULL OR a.emp_no = %s)
                     ORDER BY coalesce(a.revoked_at, a.granted_at) DESC, a.id DESC
                     LIMIT %s""",
                (bot, emp_no, emp_no, limit),
            )
            rows = [_grant(dict(r)) for r in cur.fetchall()]
    except UserDirectoryError:
        raise
    except Exception as exc:
        raise UserDirectoryError("database_unavailable") from exc
    return {"bot": bot, "grants": rows}


# ---------------------------------------------------------------------------
# 부여 · 회수
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GrantRequest:
    bot: str
    emp_no: str
    reason: str
    actor: str
    expires_at: datetime | None = None


def grant(request: GrantRequest, *, connect=None) -> dict:
    """권한을 준다. 이미 살아 있는 권한이 있으면 `already_granted`.

    만료된 열린 줄은 같은 트랜잭션에서 회수(「만료 후 다시 부여」)하고 새 줄을 만든다 —
    열린 줄은 사람·봇마다 하나라는 인덱스를 지키면서 이력을 남긴다.
    """
    bot = _bot(request.bot)
    emp_no = _text(request.emp_no, limit=40)
    reason = _text(request.reason, limit=500)
    actor = _text(request.actor, limit=200).lower()
    expires_at = request.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise UserDirectoryError("invalid_request")
        if expires_at <= datetime.now(UTC):
            raise UserDirectoryError("expires_in_past")
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute("SELECT active FROM employee WHERE emp_no = %s FOR SHARE", (emp_no,))
            emp = cur.fetchone()
            if emp is None:
                raise UserDirectoryError("employee_not_found")
            if not emp["active"]:
                raise UserDirectoryError("employee_inactive")
            cur.execute(
                """SELECT id, (expires_at IS NULL OR expires_at > now()) AS live
                     FROM bot_access
                    WHERE bot = %s AND emp_no = %s AND revoked_at IS NULL
                    FOR UPDATE""",
                (bot, emp_no),
            )
            open_row = cur.fetchone()
            if open_row is not None:
                if open_row["live"]:
                    raise UserDirectoryError("already_granted")
                cur.execute(
                    """UPDATE bot_access
                          SET revoked_at = now(), revoked_by = %s,
                              revoke_reason = '만료 후 다시 부여'
                        WHERE id = %s""",
                    (actor, open_row["id"]),
                )
            cur.execute(
                """INSERT INTO bot_access (bot, emp_no, reason, granted_by, expires_at)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (bot, emp_no, reason, actor, expires_at),
            )
            new_id = cur.fetchone()["id"]
            cur.execute(
                f"""SELECT {_GRANT_COLUMNS}
                      FROM bot_access a JOIN employee e ON e.emp_no = a.emp_no
                      LEFT JOIN org_unit o ON o.code = e.org_code
                     WHERE a.id = %s""",
                (new_id,),
            )
            return _grant(dict(cur.fetchone()))
    except UserDirectoryError:
        raise
    except Exception as exc:
        if exc.__class__.__name__ == "UniqueViolation":
            # 같은 사람에게 두 관리자가 동시에 준 경우. 하나만 남는다.
            raise UserDirectoryError("already_granted") from exc
        raise UserDirectoryError("database_unavailable") from exc


def revoke(grant_id: int, *, reason: str, actor: str, connect=None) -> dict:
    """권한을 거둔다. 줄은 남는다."""
    if type(grant_id) is not int or grant_id <= 0:
        raise UserDirectoryError("invalid_request")
    reason = _text(reason, limit=500)
    actor = _text(actor, limit=200).lower()
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute("SELECT revoked_at FROM bot_access WHERE id = %s FOR UPDATE",
                        (grant_id,))
            row = cur.fetchone()
            if row is None:
                raise UserDirectoryError("grant_not_found")
            if row["revoked_at"] is not None:
                raise UserDirectoryError("already_revoked")
            cur.execute(
                """UPDATE bot_access
                      SET revoked_at = now(), revoked_by = %s, revoke_reason = %s
                    WHERE id = %s""",
                (actor, reason, grant_id),
            )
            cur.execute(
                f"""SELECT {_GRANT_COLUMNS}
                      FROM bot_access a JOIN employee e ON e.emp_no = a.emp_no
                      LEFT JOIN org_unit o ON o.code = e.org_code
                     WHERE a.id = %s""",
                (grant_id,),
            )
            return _grant(dict(cur.fetchone()))
    except UserDirectoryError:
        raise
    except Exception as exc:
        raise UserDirectoryError("database_unavailable") from exc


# ---------------------------------------------------------------------------
# 확인 — 봇이 실제로 내릴 판정을 그대로 보여 준다
# ---------------------------------------------------------------------------

def check(*, bot: str, emp_no: str | None = None, workspace: str | None = None,
          slack_user: str | None = None, connect=None) -> dict:
    """사번 또는 (워크스페이스, Slack 사용자)로 판정한다. 봇과 같은 함수를 쓴다."""
    bot = _bot(bot)
    emp_no = (emp_no or "").strip() or None
    workspace = (workspace or "").strip().lower() or None
    slack_user = (slack_user or "").strip() or None
    if (emp_no is None) == (workspace is None or slack_user is None):
        raise UserDirectoryError("invalid_request")
    try:
        with (connect or _connect)() as conn:
            if emp_no is not None:
                decision = bot_access.decide_for_emp(conn, bot=bot, emp_no=emp_no)
            else:
                decision = bot_access.may_use_bot(
                    conn, bot=bot, workspace=workspace, slack_user=slack_user)
    except Exception as exc:
        raise UserDirectoryError("database_unavailable") from exc
    return {
        "bot": bot,
        "allowed": decision.allowed,
        "code": decision.code,
        "empNo": decision.emp_no,
        "grantId": decision.grant_id,
        "expiresAt": decision.expires_at,
    }


__all__ = [
    "CODES",
    "VIEWS",
    "GrantRequest",
    "UserDirectoryError",
    "check",
    "grant",
    "list_grants",
    "list_people",
    "revoke",
]
