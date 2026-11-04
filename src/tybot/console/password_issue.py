"""콘솔 임시 비밀번호 — TYBot DM 으로 발급하고, 첫 로그인에서 바꾸게 한다.

표: `deploy/sql/console_password_schema.sql` (규칙 설명도 거기 있다)

## 흐름
- **발급**(관리자 버튼만): 6자 임시 비밀번호 → 해시 저장 + `must_change_password` →
  TYBot 이 본인 Slack DM 으로 보낸다. DM 이 실패하면 트랜잭션을 되돌린다 — 아무도 모르는
  비밀번호로 계정이 잠기면 안 된다
- **로그인**: 임시 비밀번호가 맞으면 세션 대신 `password_change_required` — 먼저 바꾼다.
  72시간이 지났거나 5번 틀렸으면 그 임시 비밀번호는 끝. 관리자가 다시 발급한다
- **변경**: 지금 비밀번호(임시 포함) 확인 → 새 비밀번호 규칙 → 저장 → 그 DM 을 지운다
- **정리**: 기한이 지난 발급을 닫고, 닫힌 발급의 DM 을 지운다(계정 동기화 타이머가 부른다)

## 원문이 남지 않게
임시 비밀번호 원문은 DM 본문 한 번에만 있다. 로그·감사·예외 문구·DB 어디에도 쓰지 않는다.
DM 은 봇 메시지라 수집기가 싣지 않고(원칙 1, `bot_id`), 바꾸거나 기한이 지나면 지운다.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .auth import hash_password, verify_password

logger = logging.getLogger("tybot.console.password_issue")

TEMP_LENGTH = 6
TEMP_HOURS = 72
TEMP_MAX_FAILED = 5
NEW_MIN = 6
NEW_MAX = 13
KST = ZoneInfo("Asia/Seoul")

# 헷갈리는 글자(0/O, 1/l/I)는 뺀다. 특수문자는 Slack 서식과 안 부딪히는 것만.
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
LOWER = "abcdefghijkmnpqrstuvwxyz"
DIGIT = "23456789"
SPECIAL = "!@#$%^&+=?"

CONTACT = "전산팀 최수연 사원"

#: 코드 → HTTP 상태
CODES: dict[str, int] = {
    "invalid_request": 422,
    "account_not_found": 404,
    "no_slack": 422,          # 살아 있는 Slack 계정이 없어 DM 을 보낼 곳이 없다
    "dm_failed": 503,
    "database_unavailable": 503,
    "bad_password": 401,
    "temp_expired": 401,
    "temp_locked": 401,
    "password_change_required": 409,
    "too_short": 422, "too_long": 422, "too_simple": 422,
    "same_as_email": 422, "same_as_current": 422,
}


class PasswordError(RuntimeError):
    """문자열은 `CODES` 중 하나. 비밀번호 원문을 절대 싣지 않는다."""


# ---------------------------------------------------------------------------
# 비밀번호 규칙
# ---------------------------------------------------------------------------

def generate_temp() -> str:
    """6자. 대·소문자·숫자·특수문자를 각각 하나 이상."""
    rng = secrets.SystemRandom()
    chars = [rng.choice(UPPER), rng.choice(LOWER), rng.choice(DIGIT), rng.choice(SPECIAL)]
    pool = UPPER + LOWER + DIGIT + SPECIAL
    chars += [rng.choice(pool) for _ in range(TEMP_LENGTH - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def _classes(text: str) -> int:
    return sum((
        any(c.isupper() for c in text), any(c.islower() for c in text),
        any(c.isdigit() for c in text), any(not c.isalnum() for c in text),
    ))


def validate_new(new: str, *, email: str, current: str) -> None:
    """6~13자, 대문자·소문자·숫자·특수문자 중 3종류 이상, 이메일·지금 비밀번호와 다를 것."""
    if len(new) < NEW_MIN:
        raise PasswordError("too_short")
    if len(new) > NEW_MAX:
        raise PasswordError("too_long")
    if _classes(new) < 3 or new.strip() != new:
        raise PasswordError("too_simple")
    lowered = new.lower()
    local = email.split("@", 1)[0].lower()
    if lowered == email.lower() or (len(local) >= 3 and local in lowered):
        raise PasswordError("same_as_email")
    if new == current:
        raise PasswordError("same_as_current")


def dm_text(email: str, temp: str, expires_at: datetime, emp_no: str | None = None) -> str:
    until = expires_at.astimezone(KST).strftime("%m/%d %H:%M")
    return (
        ":closed_lock_with_key: *TYBot 관리 콘솔 임시 비밀번호가 발급되었습니다.*\n"
        f"{f'아이디(사번): {emp_no}' if emp_no else f'아이디: {email}'}\n"
        f"임시 비밀번호: `{temp}`  (유효기간 {until} 까지)\n\n"
        "최초 로그인 시 새로운 비밀번호를 설정해야 합니다. 발급된 임시 비밀번호는 "
        "발급일로부터 72시간 동안 유효하므로, 유효기간 내에 로그인하여 비밀번호를 변경해 "
        f"주시기 바랍니다. 비밀번호 관련 문의사항은 {CONTACT}에게 문의해 주시기 바랍니다.\n"
        ":warning: 이 대화창에 비밀번호를 입력하지 마세요."
    )


# ---------------------------------------------------------------------------
# Slack DM
# ---------------------------------------------------------------------------

class SlackSender:
    """TYBot 봇 토큰으로 DM 을 보내고 지운다. 테스트는 같은 모양의 가짜를 넘긴다."""

    def __init__(self, client_factory=None):
        if client_factory is None:
            from slack_sdk import WebClient

            client_factory = WebClient
        self._factory = client_factory

    def send(self, token: str, slack_user: str, text: str) -> tuple[str, str]:
        client = self._factory(token=token, timeout=15)
        opened = client.conversations_open(users=slack_user)
        channel = str(opened["channel"]["id"])
        posted = client.chat_postMessage(channel=channel, text=text, unfurl_links=False,
                                         unfurl_media=False)
        return channel, str(posted["ts"])

    def delete(self, token: str, channel: str, ts: str) -> None:
        self._factory(token=token, timeout=15).chat_delete(channel=channel, ts=ts)


def _tokens() -> dict[str, str]:
    from .license_store import linked_workspaces

    return {ws: token for ws, (_label, token) in linked_workspaces().items() if token}


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise PasswordError("database_unavailable")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise PasswordError("database_unavailable") from exc


@dataclass
class _Deps:
    connect: object
    tokens: dict[str, str]
    sender: SlackSender


def _deps(connect=None, tokens=None, sender=None) -> _Deps:
    return _Deps(connect or _connect, _tokens() if tokens is None else tokens,
                 sender or SlackSender())


_TARGET_SQL = """
SELECT ui.workspace, ui.slack_user, sp.active AS live
  FROM console_user u
  JOIN employee e ON e.emp_no = u.emp_no
                  OR (u.emp_no IS NULL AND lower(btrim(e.email)) = lower(u.email))
  JOIN user_identity ui ON ui.emp_no = e.emp_no
  LEFT JOIN slack_presence sp ON sp.workspace = ui.workspace AND sp.slack_user = ui.slack_user
 WHERE u.email = %s AND e.active
 ORDER BY (sp.active IS TRUE) DESC, ui.workspace
"""


def _target(cur, email: str, tokens: dict[str, str]) -> tuple[str, str] | None:
    """DM 을 보낼 (워크스페이스, Slack 사용자). Slack 에서 지워진 계정과 토큰 없는 곳은 뺀다."""
    cur.execute(_TARGET_SQL, (email,))
    for row in cur.fetchall():
        if row["live"] is not False and row["workspace"] in tokens:
            return row["workspace"], row["slack_user"]
    return None


_EMP_SQL = """
SELECT e.emp_no FROM console_user u
  JOIN employee e ON e.emp_no = u.emp_no
                  OR (u.emp_no IS NULL AND lower(btrim(e.email)) = lower(u.email))
 WHERE u.email = %s AND e.active
"""

_RESOLVE_SQL = """
SELECT DISTINCT u.email FROM console_user u
  JOIN employee e ON e.emp_no = u.emp_no
                  OR (u.emp_no IS NULL AND lower(btrim(e.email)) = lower(u.email))
 WHERE upper(e.emp_no) = upper(%s)
"""


def _emp_no(cur, email: str) -> str | None:
    cur.execute(_EMP_SQL, (email,))
    rows = cur.fetchall()
    return str(rows[0]["emp_no"]) if len(rows) == 1 else None


def login_email(identifier: str | None, *, connect=None) -> str:
    """로그인 아이디(사번 또는 회사 이메일) → 계정 이메일.

    계정의 열쇠는 이메일 그대로다(감사 기록·담당 범위가 이메일을 가리킨다). 사번은 입구일 뿐이다.
    `@` 가 있으면 이메일로 본다. 사번이 한 계정으로 딱 이어지지 않으면 받은 값을 그대로 돌려준다 —
    그러면 로그인은 「아이디 또는 비밀번호가 맞지 않습니다」 로 끝나고, 계정 유무가 드러나지 않는다.
    """
    ident = (identifier or "").strip()
    if not ident or "@" in ident or len(ident) > 40:
        return ident.lower()
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(_RESOLVE_SQL, (ident,))
            rows = cur.fetchall()
            conn.rollback()
    except Exception:  # noqa: BLE001 - 못 찾으면 그대로 — 로그인이 거절한다
        logger.warning("사번 로그인 아이디 조회 실패")
        return ident
    return str(rows[0]["email"]).lower() if len(rows) == 1 else ident


def _delete_dms(deps: _Deps, ids: list[int]) -> None:
    """닫힌 발급의 DM 을 지운다. 실패는 기록만 하고 다음 정리 때 다시 시도한다."""
    if not ids:
        return
    with deps.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, workspace, dm_channel, dm_ts FROM console_password_issue
                        WHERE id = ANY(%s) AND dm_deleted_at IS NULL""", (ids,))
        for row in cur.fetchall():
            token = deps.tokens.get(row["workspace"])
            error = None
            try:
                if not token:
                    raise RuntimeError("no_token")
                deps.sender.delete(token, row["dm_channel"], row["dm_ts"])
            except Exception as exc:  # noqa: BLE001 - 지우기 실패로 변경을 무르지 않는다
                response = getattr(exc, "response", None)
                try:
                    error = str(response.get("error")) if response is not None else ""
                except Exception:  # noqa: BLE001
                    error = ""
                if str(exc) == "no_token":
                    error = "no_token"
                error = error or type(exc).__name__
            if error in (None, "message_not_found"):
                cur.execute("UPDATE console_password_issue SET dm_deleted_at = now(), "
                            "dm_delete_error = NULL WHERE id = %s", (row["id"],))
            else:
                cur.execute("UPDATE console_password_issue SET dm_delete_error = %s "
                            "WHERE id = %s", (error[:80], row["id"]))
        conn.commit()


# ---------------------------------------------------------------------------
# 발급
# ---------------------------------------------------------------------------

def issue(email: str, *, kind: str, actor: str, connect=None, tokens=None, sender=None,
          now: datetime | None = None) -> dict:
    """한 사람에게 임시 비밀번호를 발급하고 DM 을 보낸다. DM 이 실패하면 아무것도 바꾸지 않는다."""
    email = (email or "").strip().lower()
    if kind not in ("initial", "reset") or "@" not in email or not actor:
        raise PasswordError("invalid_request")
    deps = _deps(connect, tokens, sender)
    now = now or datetime.now(UTC)
    expires = now + timedelta(hours=TEMP_HOURS)
    replaced: list[int] = []
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT email, active FROM console_user WHERE lower(email) = %s FOR UPDATE",
                        (email,))
            row = cur.fetchone()
            if row is None or not row["active"]:
                raise PasswordError("account_not_found")
            email = row["email"]
            target = _target(cur, email, deps.tokens)
            if target is None:
                raise PasswordError("no_slack")
            workspace, slack_user = target
            temp = generate_temp()
            cur.execute(
                """UPDATE console_user SET password_hash = %s, must_change_password = true,
                          temp_expires_at = %s, temp_failed = 0 WHERE email = %s""",
                (hash_password(temp), expires, email),
            )
            cur.execute(
                """UPDATE console_password_issue SET closed_at = now(), close_reason = 'replaced'
                    WHERE email = %s AND closed_at IS NULL RETURNING id""", (email,))
            replaced = [r["id"] for r in cur.fetchall()]
            try:
                channel, ts = deps.sender.send(deps.tokens[workspace], slack_user,
                                               dm_text(email, temp, expires, _emp_no(cur, email)))
            except Exception as exc:  # noqa: BLE001 - 어떤 Slack 실패든 발급을 되돌린다
                logger.warning("임시 비밀번호 DM 실패 — %s ws=%s err=%s", email, workspace,
                               type(exc).__name__)
                raise PasswordError("dm_failed") from None
            cur.execute(
                """INSERT INTO console_password_issue
                     (email, kind, issued_by, issued_at, expires_at, workspace, dm_channel, dm_ts)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (email, kind, actor, now, expires, workspace, channel, ts),
            )
            issue_id = cur.fetchone()["id"]
            conn.commit()
    except PasswordError:
        raise
    except Exception as exc:
        logger.exception("임시 비밀번호 발급 실패 — %s", email)
        raise PasswordError("database_unavailable") from exc
    _delete_dms(deps, replaced)
    logger.info("임시 비밀번호 발급 — %s kind=%s by=%s ws=%s", email, kind, actor, workspace)
    # 화면은 시각 문자열을 그대로 잘라 쓴다 — 한국 시간으로 돌려준다(UTC 면 9시간 어긋나 보인다).
    return {"id": issue_id, "email": email, "workspace": workspace,
            "expiresAt": expires.astimezone(KST), "kind": kind}


_INITIAL_SQL = """
SELECT u.email, u.name, u.managed_by IS NOT NULL AS managed
  FROM console_user u
 WHERE u.active AND coalesce(u.password_hash, '') = '' AND NOT u.must_change_password
   AND NOT EXISTS (SELECT 1 FROM console_password_issue i
                    WHERE i.email = u.email AND i.closed_at IS NULL)
 ORDER BY u.name, u.email
"""


def initial(*, actor: str, apply: bool, connect=None, tokens=None, sender=None) -> dict:
    """비밀번호를 한 번도 받지 않은 계정에 초기 임시 비밀번호. 미리 보기면 대상만 센다."""
    deps = _deps(connect, tokens, sender)
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            cur.execute(_INITIAL_SQL)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["slack"] = _target(cur, r["email"], deps.tokens) is not None
            conn.rollback()
    except Exception as exc:
        raise PasswordError("database_unavailable") from exc
    result = {"apply": apply, "targets": [{"email": r["email"], "name": r["name"],
                                            "slack": r["slack"]} for r in rows],
              "sent": [], "failed": []}
    if not apply:
        return result
    for r in rows:
        if not r["slack"]:
            result["failed"].append({"email": r["email"], "name": r["name"], "code": "no_slack"})
            continue
        try:
            issue(r["email"], kind="initial", actor=actor, connect=deps.connect,
                  tokens=deps.tokens, sender=deps.sender)
            result["sent"].append({"email": r["email"], "name": r["name"]})
        except PasswordError as exc:
            result["failed"].append({"email": r["email"], "name": r["name"], "code": str(exc)})
    return result


# ---------------------------------------------------------------------------
# 로그인 · 변경
# ---------------------------------------------------------------------------

_STATE_SQL = """SELECT email, password_hash, must_change_password, temp_expires_at, temp_failed
                  FROM console_user WHERE lower(email) = %s AND active FOR UPDATE"""


def _check_current(cur, row: dict, password: str, now: datetime) -> None:
    """지금 비밀번호 확인. 임시면 기한·잠금·틀린 횟수까지. 틀린 횟수는 같은 트랜잭션에 남긴다."""
    if row["must_change_password"]:
        if row["temp_expires_at"] is None or row["temp_expires_at"] <= now:
            raise PasswordError("temp_expired")
        if row["temp_failed"] >= TEMP_MAX_FAILED:
            raise PasswordError("temp_locked")
    if row["password_hash"] and verify_password(password, row["password_hash"]):
        return
    if row["must_change_password"]:
        failed = row["temp_failed"] + 1
        cur.execute("UPDATE console_user SET temp_failed = %s WHERE email = %s",
                    (failed, row["email"]))
        if failed >= TEMP_MAX_FAILED:
            cur.execute("""UPDATE console_password_issue SET closed_at = now(), close_reason = 'locked'
                            WHERE email = %s AND closed_at IS NULL""", (row["email"],))
            raise PasswordError("temp_locked")
    raise PasswordError("bad_password")


def temp_login(email: str, password: str, *, connect=None, now: datetime | None = None) -> bool:
    """임시 비밀번호 계정이면 확인해 예외로 알린다. 아니면 False — 보통 로그인으로 간다.

    맞으면 `password_change_required`, 틀리면 `bad_password`(횟수 기록), 끝났으면
    `temp_expired`/`temp_locked`.
    """
    email = (email or "").strip().lower()
    now = now or datetime.now(UTC)
    with (connect or _connect)() as conn, conn.cursor() as cur:
        cur.execute(_STATE_SQL, (email,))
        row = cur.fetchone()
        if row is None or not row["must_change_password"]:
            conn.rollback()
            return False
        try:
            _check_current(cur, row, password, now)
        except PasswordError:
            conn.commit()      # 틀린 횟수·잠금을 남긴다
            raise
        conn.rollback()
    raise PasswordError("password_change_required")


def change(email: str, current: str, new: str, *, connect=None, tokens=None, sender=None,
           now: datetime | None = None) -> dict:
    """지금 비밀번호(임시 포함)를 확인하고 새 비밀번호로 바꾼다. 발급 DM 은 지운다."""
    email = (email or "").strip().lower()
    if "@" not in email:
        raise PasswordError("invalid_request")
    deps = _deps(connect, tokens, sender)
    now = now or datetime.now(UTC)
    with deps.connect() as conn, conn.cursor() as cur:
        cur.execute(_STATE_SQL, (email,))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            raise PasswordError("bad_password")   # 계정 유무를 드러내지 않는다
        try:
            _check_current(cur, row, current, now)
        except PasswordError:
            conn.commit()
            raise
        validate_new(new, email=row["email"], current=current)
        cur.execute(
            """UPDATE console_user SET password_hash = %s, must_change_password = false,
                      temp_expires_at = NULL, temp_failed = 0, password_changed_at = now()
                WHERE email = %s""",
            (hash_password(new), row["email"]),
        )
        cur.execute("""UPDATE console_password_issue SET closed_at = now(), close_reason = 'changed'
                        WHERE email = %s AND closed_at IS NULL RETURNING id""", (row["email"],))
        closed = [r["id"] for r in cur.fetchall()]
        conn.commit()
    _delete_dms(deps, closed)
    logger.info("콘솔 비밀번호 변경 — %s", row["email"])
    return {"email": row["email"]}


def cleanup(*, connect=None, tokens=None, sender=None) -> dict:
    """기한이 지난 발급을 닫고, 닫힌 발급의 DM 을 지운다(지난 실패분 포함)."""
    deps = _deps(connect, tokens, sender)
    with deps.connect() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE console_password_issue SET closed_at = now(), close_reason = 'expired'
                        WHERE closed_at IS NULL AND expires_at <= now()""")
        expired = cur.rowcount
        cur.execute("""SELECT id FROM console_password_issue
                        WHERE closed_at IS NOT NULL AND dm_deleted_at IS NULL
                          AND closed_at > now() - interval '30 days'""")
        pending = [r["id"] for r in cur.fetchall()]
        conn.commit()
    _delete_dms(deps, pending)
    return {"expired": expired, "dmDeleteTried": len(pending)}


__all__ = [
    "CODES",
    "NEW_MAX",
    "NEW_MIN",
    "TEMP_HOURS",
    "TEMP_LENGTH",
    "TEMP_MAX_FAILED",
    "PasswordError",
    "SlackSender",
    "change",
    "cleanup",
    "dm_text",
    "generate_temp",
    "initial",
    "issue",
    "login_email",
    "temp_login",
    "validate_new",
]
