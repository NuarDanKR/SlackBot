"""Slack 회사 이메일로 검증된 사번 매핑을 만든다.

이메일은 최초 검증에만 사용한다. 권한 조회는 `user_identity`의 확정된 사번을 사용하며,
이메일·이름은 매핑 테이블이나 로그에 복제하지 않는다.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tybot.identity")

CURRENT_SQL = """
select ui.emp_no
  from user_identity ui
  join employee e on e.emp_no = ui.emp_no and e.active
 where ui.workspace = %(workspace)s and ui.slack_user = %(slack_user)s
"""

EMPLOYEE_BY_EMAIL_SQL = """
select emp_no
  from employee
 where active and email is not null and lower(btrim(email)) = lower(%(email)s)
"""

UPSERT_SQL = """
insert into user_identity (workspace, slack_user, emp_no, verified_by, verified_at)
values (%(workspace)s, %(slack_user)s, %(emp_no)s, 'email_match', now())
on conflict (workspace, slack_user) do update set
    emp_no = excluded.emp_no,
    verified_by = 'email_match',
    verified_at = now()
where user_identity.verified_by = 'email_match'
   or user_identity.emp_no = excluded.emp_no
"""


def _one_emp_no(rows) -> str | None:
    if len(rows) != 1:
        return None
    row = rows[0]
    return str(row["emp_no"] if isinstance(row, dict) else row[0])


def ensure(conn, client, *, workspace: str, slack_user: str) -> str | None:
    """기존 매핑을 반환하거나 Slack 이메일로 새 매핑을 검증해 저장한다."""
    if not workspace or not slack_user:
        return None
    with conn.cursor() as cur:
        cur.execute(CURRENT_SQL, {"workspace": workspace, "slack_user": slack_user})
        existing = _one_emp_no(cur.fetchall())
    if existing:
        return existing

    try:
        user = (client.users_info(user=slack_user) or {}).get("user") or {}
    except Exception as exc:  # noqa: BLE001 - Slack 장애로 명령 전체를 죽이지 않는다
        logger.warning("[%s] Slack 이메일 조회 실패 user=%s code=%s", workspace, slack_user,
                       exc.__class__.__name__)
        return None
    email = str((user.get("profile") or {}).get("email") or "").strip()
    if not email:
        logger.info("[%s] Slack 이메일 없음 user=%s", workspace, slack_user)
        return None

    with conn.cursor() as cur:
        cur.execute(EMPLOYEE_BY_EMAIL_SQL, {"email": email})
        emp_no = _one_emp_no(cur.fetchall())
        if not emp_no:
            logger.info("[%s] 회사 이메일과 일치하는 활성 직원 없음 user=%s", workspace, slack_user)
            return None
        cur.execute(UPSERT_SQL, {
            "workspace": workspace,
            "slack_user": slack_user,
            "emp_no": emp_no,
        })
    if not getattr(conn, "autocommit", False):
        conn.commit()
    logger.info("[%s] 이메일로 사번 매핑 완료 user=%s emp=%s", workspace, slack_user, emp_no)
    return emp_no


# --- 전체 훑기 ----------------------------------------------------------------
#
# `ensure` 는 **사람이 봇을 쓸 때** 불린다. 그래서 아직 봇을 써 보지 않은 사람은
# 매핑이 없고, 매핑이 없으면 일정 DM 같은 자동 발송에서 그대로 빠진다 — 이메일이
# 맞아도 그렇다. 실제로 8명 팀에서 4명만 잡혀 있었다(2026-09-11).
#
# 그 사람이 봇을 쓸 이유가 없어도 알림은 받아야 한다. 그래서 워크스페이스 멤버를
# 한 번에 훑어 같은 규칙으로 이어 준다.
#
# 이메일은 **여기서도 저장하지 않는다.** 사번을 찾는 데만 쓰고 버린다.
class BackfillResult:
    """무엇이 이어졌고 무엇이 왜 안 이어졌는가. 건수만 담는다."""

    __slots__ = ("already", "linked", "no_email", "no_match", "skipped", "unmatched")

    def __init__(self) -> None:
        self.linked = 0      # 이번에 새로 이어졌다
        self.already = 0     # 이미 이어져 있었다
        self.no_email = 0    # Slack 프로필에 이메일이 없다
        self.no_match = 0    # 이메일과 맞는 재직자가 없다
        self.skipped = 0     # 봇·삭제된 계정
        # 사람이 손으로 확인해야 하는 목록. **Slack 사용자 ID 만** 담는다 —
        # 이메일·이름을 담으면 로그·화면을 거쳐 복제된다.
        self.unmatched: list[str] = []

    def summary(self) -> str:
        return (
            f"새로 연결 {self.linked} · 기존 {self.already} · "
            f"이메일 없음 {self.no_email} · 일치하는 직원 없음 {self.no_match} · "
            f"건너뜀(봇·삭제) {self.skipped}"
        )


def _members(client):
    """워크스페이스 사람 목록. 페이지를 끝까지 따라간다."""
    cursor = None
    while True:
        page = client.users_list(limit=200, cursor=cursor) or {}
        yield from (page.get("members") or [])
        cursor = ((page.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            return


def backfill(
    conn, client, *, workspace: str, dry_run: bool = False
) -> BackfillResult:
    """워크스페이스 멤버 전체를 이메일로 이어 본다.

    `ensure` 와 **같은 SQL** 을 쓴다. 규칙이 갈라지면 어떤 사람은 명령으로는
    이어지고 훑기로는 안 이어지는 상태가 된다.

    이미 이어진 사람은 Slack 을 다시 묻지 않는다 — `users_list` 한 번으로 끝낸다.

    `dry_run` 이면 **쓰지 않는다.** 롤백에 의존하지 않고 아예 실행하지 않는 편이
    안전하다 — 읽기 전용 연결로도 돌려 볼 수 있어야 한다.
    """
    result = BackfillResult()
    if not workspace:
        return result

    with conn.cursor() as cur:
        cur.execute(
            "select slack_user from user_identity"
            " where workspace = %(workspace)s and emp_no is not null",
            {"workspace": workspace},
        )
        known = {
            str(r["slack_user"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()
        }

    for member in _members(client):
        slack_user = str(member.get("id") or "")
        if not slack_user:
            continue
        if member.get("is_bot") or member.get("deleted") or slack_user == "USLACKBOT":
            result.skipped += 1
            continue
        if slack_user in known:
            result.already += 1
            continue

        email = str((member.get("profile") or {}).get("email") or "").strip()
        if not email:
            result.no_email += 1
            result.unmatched.append(slack_user)
            continue

        with conn.cursor() as cur:
            cur.execute(EMPLOYEE_BY_EMAIL_SQL, {"email": email})
            emp_no = _one_emp_no(cur.fetchall())
            if not emp_no:
                result.no_match += 1
                result.unmatched.append(slack_user)
                continue
            if not dry_run:
                cur.execute(UPSERT_SQL, {
                    "workspace": workspace,
                    "slack_user": slack_user,
                    "emp_no": emp_no,
                })
        result.linked += 1

    if not dry_run and not getattr(conn, "autocommit", False):
        conn.commit()
    # 이메일·이름은 남기지 않는다. 건수와 Slack ID 만.
    logger.info("[%s] 사번 매핑 훑기 — %s", workspace, result.summary())
    return result
