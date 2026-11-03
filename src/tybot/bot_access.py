"""봇 사용 권한 판정 — 「이 Slack 사용자가 이 봇을 쓸 수 있나」 의 유일한 답.

표: `deploy/sql/bot_access_schema.sql` · 관리: 콘솔 > 사용자(`console/user_directory.py`)

## 규칙 (원칙 3: 막는 쪽이 기본값)
1. Slack 사용자 → 사번은 `user_identity` 로만 찾는다. 매핑이 없으면 거절이다.
   봇은 판정 전에 `identity.ensure()` 로 매핑을 만들 기회를 줄 수 있다.
2. 퇴직자(`employee.active = false`)는 권한 줄이 남아 있어도 거절한다.
3. 회수되지 않았고 만료되지 않은 줄이 있어야 허용한다.

DB 오류는 거절로 돌려준다. 판정을 못 했는데 허용하면 권한이 조용히 넓어진다.

## 봇에서 쓰는 법
```python
from tybot import identity, bot_access
identity.ensure(conn, client, workspace=ws, slack_user=user)
decision = bot_access.may_use_bot(conn, bot="vipbot", workspace=ws, slack_user=user)
if not decision.allowed:
    say(bot_access.DENY_MESSAGE)   # 사유 코드는 로그에만 남긴다
```
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("tybot.bot_access")

#: 권한을 관리하는 봇. 여기 없는 이름은 콘솔이 받지 않는다.
BOTS: dict[str, str] = {
    "vipbot": "VIP 봇",
}

BOT_KEY = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")

#: 사람에게 보이는 거절 문구. 사유(매핑 없음·퇴직·권한 없음)는 밝히지 않는다.
DENY_MESSAGE = "이 봇을 사용할 권한이 없습니다. 필요하면 전산팀에 요청해 주세요."

#: 판정 사유 코드. 콘솔 「확인」 과 로그가 같은 코드를 쓴다.
ALLOWED = "allowed"
NO_IDENTITY = "no_identity"          # Slack 사용자 ↔ 사번 매핑 없음
UNKNOWN_EMPLOYEE = "unknown_employee"  # 사번이 employee 에 없음
INACTIVE_EMPLOYEE = "inactive_employee"
NO_GRANT = "no_grant"
EXPIRED = "expired"
UNKNOWN_BOT = "unknown_bot"
CHECK_FAILED = "check_failed"        # DB 오류. 거절로 처리한다


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    emp_no: str | None = None
    grant_id: int | None = None
    expires_at: datetime | None = None


GRANT_SQL = """
SELECT e.emp_no, e.active,
       a.id AS grant_id, a.expires_at,
       (a.id IS NOT NULL AND (a.expires_at IS NULL OR a.expires_at > now())) AS live
  FROM employee e
  LEFT JOIN bot_access a
         ON a.emp_no = e.emp_no AND a.bot = %(bot)s AND a.revoked_at IS NULL
 WHERE e.emp_no = %(emp_no)s
"""

IDENTITY_SQL = """
SELECT emp_no FROM user_identity
 WHERE workspace = %(workspace)s AND slack_user = %(slack_user)s AND emp_no IS NOT NULL
"""


def _row(row, key: str, index: int):
    return row[key] if isinstance(row, dict) else row[index]


def decide_for_emp(conn, *, bot: str, emp_no: str) -> Decision:
    """사번 기준 판정. 콘솔 「확인」 과 `may_use_bot` 이 함께 쓴다."""
    if bot not in BOTS:
        return Decision(False, UNKNOWN_BOT, emp_no)
    with conn.cursor() as cur:
        cur.execute(GRANT_SQL, {"bot": bot, "emp_no": emp_no})
        row = cur.fetchone()
    if row is None:
        return Decision(False, UNKNOWN_EMPLOYEE, emp_no)
    if not _row(row, "active", 1):
        return Decision(False, INACTIVE_EMPLOYEE, emp_no)
    grant_id = _row(row, "grant_id", 2)
    expires_at = _row(row, "expires_at", 3)
    if grant_id is None:
        return Decision(False, NO_GRANT, emp_no)
    if not _row(row, "live", 4):
        return Decision(False, EXPIRED, emp_no, int(grant_id), expires_at)
    return Decision(True, ALLOWED, emp_no, int(grant_id), expires_at)


def may_use_bot(conn, *, bot: str, workspace: str, slack_user: str) -> Decision:
    """Slack 사용자 기준 판정. **예외를 밖으로 내보내지 않고 거절로 바꾼다.**"""
    if bot not in BOTS:
        return Decision(False, UNKNOWN_BOT)
    if not workspace or not slack_user:
        return Decision(False, NO_IDENTITY)
    try:
        with conn.cursor() as cur:
            cur.execute(IDENTITY_SQL, {"workspace": workspace, "slack_user": slack_user})
            rows = cur.fetchall()
        if len(rows) != 1:
            decision = Decision(False, NO_IDENTITY)
        else:
            decision = decide_for_emp(conn, bot=bot, emp_no=str(_row(rows[0], "emp_no", 0)))
    except Exception as exc:  # noqa: BLE001 - 판정 실패는 거절이다
        logger.warning("[%s] 봇 권한 판정 실패 bot=%s user=%s err=%s",
                       workspace, bot, slack_user, exc.__class__.__name__)
        return Decision(False, CHECK_FAILED)
    if not decision.allowed:
        logger.info("[%s] 봇 사용 거절 bot=%s user=%s code=%s",
                    workspace, bot, slack_user, decision.code)
    return decision


SOURCE_SQL = """
SELECT workspace, channel_id FROM bot_source_channel
 WHERE bot = %(bot)s AND removed_at IS NULL
"""


def source_channels(conn, *, bot: str) -> frozenset[tuple[str, str]]:
    """이 봇이 근거로 쓸 수 있는 (워크스페이스, 채널 ID). **이 밖의 채널은 보지 않는다.**

    비어 있거나 읽지 못하면 빈 집합 — 근거 없음(막는 쪽이 기본값). 봇은 검색 결과를
    이 집합으로 거른 뒤에만 답을 만든다.
    """
    if bot not in BOTS:
        return frozenset()
    try:
        with conn.cursor() as cur:
            cur.execute(SOURCE_SQL, {"bot": bot})
            return frozenset((str(_row(r, "workspace", 0)), str(_row(r, "channel_id", 1)))
                             for r in cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - 못 읽으면 근거 없음
        logger.warning("봇 근거 채널 조회 실패 bot=%s err=%s", bot, exc.__class__.__name__)
        return frozenset()


__all__ = [
    "ALLOWED",
    "BOTS",
    "CHECK_FAILED",
    "DENY_MESSAGE",
    "EXPIRED",
    "INACTIVE_EMPLOYEE",
    "NO_GRANT",
    "NO_IDENTITY",
    "UNKNOWN_BOT",
    "UNKNOWN_EMPLOYEE",
    "Decision",
    "decide_for_emp",
    "may_use_bot",
    "source_channels",
]
