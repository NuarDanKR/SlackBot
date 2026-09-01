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
