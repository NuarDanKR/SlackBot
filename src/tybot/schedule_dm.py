"""일정 알림 개인 DM — 수신 설정 · 큐 생성 · 발송.

설계: [`docs/design/schedule-dm-reminders.md`](../../docs/design/schedule-dm-reminders.md)

    Oracle → schedule_export.py → schedulesync → PostgreSQL
                                              → 여기(플래너/발송) → Slack Web API

워크스페이스마다 조직 단위가 달라 팀별 공지 채널을 다 만들고 관리하는 비용이 크다.
자동 알림의 기본 전달 수단을 개인 DM 으로 옮긴다. `/일정` 수동 조회와 기존 채널 공지는
그대로 둔다 — 이 모듈은 **추가 경로**이지 대체가 아니다.

## 지키는 것 셋

**권한을 추측하지 않는다.** 승인 폴더 → 폴더 ACL 조직 → 재직 중인 employee →
검증된 user_identity → 사용자가 고른 대표 워크스페이스, 이 사슬이 전부 이어질 때만
보낸다. 이름·이메일 일부·표시 이름으로 사람을 맞히지 않는다.

**같은 사람에게 한 번만 보낸다.** 멱등 키는 Slack 사용자 ID 가 아니라
`(source_folder_id, date_id, emp_no, reminder_minutes)` 다. 대표 워크스페이스가 바뀌면
Slack ID 도 바뀌지만 같은 사람이다.

**본문을 남기지 않는다.** 제목·장소·사용자 이름은 로그·DB·오류 열에 넣지 않는다.
남길 것은 식별자·상태·시각·비민감 오류 코드다.

> 설계 문서는 `src/tybot/notify.py` 의 패턴 재사용을 전제했지만 그 모듈은 아직 없다.
> 재시도·비식별 로깅 패턴을 여기서 처음 정의한다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

logger = logging.getLogger("tybot.schedule_dm")

KST = timezone(timedelta(hours=9))

ALLOWED_MINUTES = (10, 30)
DEFAULT_MINUTES = (30,)

# 이만큼 늦은 알림은 보내지 않는다. 봇이 멈췄다 살아나면 지난 알림이 몰려 나가는데,
# 이미 지난 회의 알림은 쓸모가 없고 신뢰만 깎는다.
LATE_GRACE = timedelta(minutes=10)

# 플래너가 미리 큐를 만들어 두는 범위. 30분 알림을 제때 만들려면 그보다 넉넉해야 한다.
PLAN_HORIZON = timedelta(hours=2)

# 일시 오류 재시도. 지수 백오프.
MAX_ATTEMPTS = 5
BACKOFF_BASE = timedelta(seconds=30)

# 다시 시도해도 소용없는 Slack 오류. 재시도하면 한도만 쓴다.
PERMANENT_ERRORS = frozenset({
    "channel_not_found",
    "user_not_found",
    "users_not_found",
    "account_inactive",
    "is_bot",
    "cannot_dm_bot",
    "user_disabled",
    "not_allowed_token_type",
})

# 락이 이보다 오래 걸려 있으면 그 워커가 죽은 것으로 본다.
STALE_LOCK = timedelta(minutes=5)


def normalize_minutes(values) -> tuple[int, ...]:
    """설정 가능한 조합으로 정리한다. 스키마 CHECK 와 같은 규칙이다.

    DB 제약과 어긋나면 저장 시점에야 터진다 — 화면에서 먼저 막는다.
    """
    picked = sorted({int(v) for v in (values or []) if int(v) in ALLOWED_MINUTES}, reverse=True)
    return tuple(picked) if picked else DEFAULT_MINUTES


# --- 수신 설정 ---------------------------------------------------------------
@dataclass(frozen=True)
class Preference:
    emp_no: str
    workspace: str
    slack_user: str
    minutes: tuple[int, ...]
    enabled: bool


IDENTITY_SQL = """
select ui.emp_no
  from user_identity ui
  join employee e on e.emp_no = ui.emp_no and e.active
 where ui.workspace = %(workspace)s
   and ui.slack_user = %(slack_user)s
   and ui.emp_no is not null
"""

PREF_BY_USER_SQL = """
select p.emp_no, p.workspace, p.slack_user, p.reminder_minutes, p.enabled
  from schedule_dm_preference p
 where p.emp_no = %(emp_no)s
"""

# emp_no 가 기본키다. 다른 워크스페이스에서 켜면 이 행이 그쪽으로 **옮겨간다** —
# 그래서 대표 수신 위치는 언제나 하나다.
PREF_UPSERT_SQL = """
insert into schedule_dm_preference (
    emp_no, workspace, slack_user, reminder_minutes, enabled, updated_by
) values (
    %(emp_no)s, %(workspace)s, %(slack_user)s, %(minutes)s, true, %(actor)s
)
on conflict (emp_no) do update set
    workspace = excluded.workspace,
    slack_user = excluded.slack_user,
    reminder_minutes = excluded.reminder_minutes,
    enabled = true,
    updated_by = excluded.updated_by,
    updated_at = now()
"""

PREF_DISABLE_SQL = """
update schedule_dm_preference
   set enabled = false, updated_by = %(actor)s, updated_at = now()
 where emp_no = %(emp_no)s
"""

# 대표 위치가 옮겨가면 옛 경로로 예약된 미발송 건은 의미가 없다.
CANCEL_OTHER_WORKSPACE_SQL = """
update schedule_dm_delivery
   set status = 'cancelled', cancelled_at = now(), updated_at = now()
 where emp_no = %(emp_no)s
   and status in ('pending', 'retry')
   and (workspace <> %(workspace)s or slack_user <> %(slack_user)s)
"""

CANCEL_ALL_PENDING_SQL = """
update schedule_dm_delivery
   set status = 'cancelled', cancelled_at = now(), updated_at = now()
 where emp_no = %(emp_no)s and status in ('pending', 'retry')
"""


def _commit(conn) -> None:
    """커밋한다. autocommit 연결이면 아무것도 하지 않는다.

    조회용 연결(`db.connect`)은 autocommit 이고, 발송 잡은 자기 연결을 따로 연다.
    두 경로가 같은 함수를 쓰므로 여기서 한 번 흡수한다 — 호출부마다 분기하면
    한 곳을 빠뜨렸을 때 조용히 커밋되지 않는다.
    """
    if getattr(conn, "autocommit", False):
        return
    conn.commit()


def resolve_emp_no(conn, *, workspace: str, slack_user: str) -> str | None:
    """이 Slack 사용자의 사번. 확인되지 않으면 `None`.

    매핑이 없거나 퇴직자면 알림을 켤 수 없다. 이름으로 추측하지 않는다.
    """
    if not workspace or not slack_user:
        return None
    with conn.cursor() as cur:
        cur.execute(IDENTITY_SQL, {"workspace": workspace, "slack_user": slack_user})
        rows = cur.fetchall()
    # 여러 행이면 어느 사번인지 모호하다. 모호하면 보내지 않는다.
    if len(rows) != 1:
        return None
    r = rows[0]
    return str(r["emp_no"] if isinstance(r, dict) else r[0])


def get_preference(conn, emp_no: str) -> Preference | None:
    if not emp_no:
        return None
    with conn.cursor() as cur:
        cur.execute(PREF_BY_USER_SQL, {"emp_no": emp_no})
        row = cur.fetchone()
    if not row:
        return None
    r = row if isinstance(row, dict) else dict(
        zip(("emp_no", "workspace", "slack_user", "reminder_minutes", "enabled"), row,
            strict=False)
    )
    return Preference(
        emp_no=str(r["emp_no"]),
        workspace=str(r["workspace"]),
        slack_user=str(r["slack_user"]),
        minutes=normalize_minutes(r["reminder_minutes"]),
        enabled=bool(r["enabled"]),
    )


def enable(conn, *, emp_no: str, workspace: str, slack_user: str, minutes) -> Preference:
    """알림을 켜고 이 워크스페이스를 대표 수신 위치로 삼는다."""
    picked = normalize_minutes(minutes)
    with conn.cursor() as cur:
        cur.execute(PREF_UPSERT_SQL, {
            "emp_no": emp_no,
            "workspace": workspace,
            "slack_user": slack_user,
            "minutes": list(picked),
            "actor": slack_user,
        })
        # 대표 위치가 바뀌었으면 옛 경로의 미발송 건을 정리한다.
        cur.execute(CANCEL_OTHER_WORKSPACE_SQL, {
            "emp_no": emp_no, "workspace": workspace, "slack_user": slack_user,
        })
    _commit(conn)
    logger.info("일정 DM 수신 설정 emp=%s ws=%s minutes=%s", emp_no, workspace, picked)
    return Preference(emp_no, workspace, slack_user, picked, True)


def disable(conn, *, emp_no: str, actor: str) -> None:
    """알림을 끈다. 발송 이력은 지우지 않는다 — 지우면 중복 방지 근거가 사라진다."""
    with conn.cursor() as cur:
        cur.execute(PREF_DISABLE_SQL, {"emp_no": emp_no, "actor": actor})
        cur.execute(CANCEL_ALL_PENDING_SQL, {"emp_no": emp_no})
    _commit(conn)
    logger.info("일정 DM 수신 해제 emp=%s", emp_no)


# --- 큐 생성 -----------------------------------------------------------------
#
# 조직 트리를 부모·자식으로 넓히지 않는다. Oracle 폴더 ACL 에서 승인된 **정확한**
# 조직코드만 쓴다. 넓히면 승인 절차가 무의미해진다.
PLAN_SQL = """
insert into schedule_dm_delivery (
    source_folder_id, date_id, emp_no, workspace, slack_user,
    reminder_minutes, scheduled_for, status
)
select o.source_folder_id,
       o.date_id,
       p.emp_no,
       p.workspace,
       p.slack_user,
       m.minutes,
       o.starts_at - make_interval(mins => m.minutes),
       'pending'
  from schedule_occurrence o
  join schedule_folder f
    on f.source_folder_id = o.source_folder_id and f.enabled
  join schedule_folder_org fo
    on fo.source_folder_id = o.source_folder_id and fo.enabled
  join employee e on e.org_code = fo.org_code and e.active
  join schedule_dm_preference p on p.emp_no = e.emp_no and p.enabled
  join user_identity ui
    on ui.workspace = p.workspace
   and ui.slack_user = p.slack_user
   and ui.emp_no = p.emp_no
  cross join lateral unnest(p.reminder_minutes) as m(minutes)
 where o.source_deleted_at is null
   and not o.is_all_day
   and o.starts_at > %(now)s
   and o.starts_at <= %(horizon)s
   and o.starts_at - make_interval(mins => m.minutes) > %(now)s - %(grace)s
on conflict (source_folder_id, date_id, emp_no, reminder_minutes) do update set
    -- 이미 보낸 건은 건드리지 않는다. 시각·대표 워크스페이스가 바뀐 **미발송** 건만
    -- 새 값으로 되돌린다.
    scheduled_for = excluded.scheduled_for,
    workspace = excluded.workspace,
    slack_user = excluded.slack_user,
    status = 'pending',
    next_attempt_at = null,
    locked_at = null,
    locked_by = null,
    cancelled_at = null,
    updated_at = now()
  where schedule_dm_delivery.status in ('pending', 'retry', 'expired', 'cancelled')
"""

# 일정이 지워졌거나 종일로 바뀌면 미발송 큐를 취소한다.
CANCEL_GONE_SQL = """
update schedule_dm_delivery d
   set status = 'cancelled', cancelled_at = now(), updated_at = now()
 where d.status in ('pending', 'retry')
   and exists (
        select 1 from schedule_occurrence o
         where o.source_folder_id = d.source_folder_id
           and o.date_id = d.date_id
           and (o.source_deleted_at is not null or o.is_all_day)
   )
"""

# 전근·퇴직으로 수신 자격이 사라진 미발송 큐를 취소한다.
CANCEL_INELIGIBLE_SQL = """
update schedule_dm_delivery d
   set status = 'cancelled', cancelled_at = now(), updated_at = now()
 where d.status in ('pending', 'retry')
   and not exists (
        select 1
          from schedule_folder_org fo
          join employee e on e.org_code = fo.org_code and e.active
          join schedule_dm_preference p on p.emp_no = e.emp_no and p.enabled
         where fo.source_folder_id = d.source_folder_id
           and fo.enabled
           and p.emp_no = d.emp_no
   )
"""

EXPIRE_SQL = """
update schedule_dm_delivery
   set status = 'expired', updated_at = now()
 where status in ('pending', 'retry')
   and scheduled_for < %(cutoff)s
"""

RELEASE_STALE_SQL = """
update schedule_dm_delivery
   set status = 'retry', locked_at = null, locked_by = null, updated_at = now()
 where status = 'sending' and locked_at < %(cutoff)s
"""


@dataclass
class PlanResult:
    queued: int = 0
    cancelled: int = 0
    expired: int = 0
    released: int = 0


def plan(conn, *, now: datetime | None = None) -> PlanResult:
    """다가오는 일정의 DM 큐를 만들고, 더 이상 유효하지 않은 큐를 정리한다.

    순서가 중요하다: 죽은 워커의 락을 먼저 풀어야 그 행이 이번 회차에 다시 잡힌다.
    """
    now = now or datetime.now(UTC)
    result = PlanResult()
    with conn.cursor() as cur:
        cur.execute(RELEASE_STALE_SQL, {"cutoff": now - STALE_LOCK})
        result.released = max(cur.rowcount or 0, 0)

        cur.execute(PLAN_SQL, {
            "now": now,
            "horizon": now + PLAN_HORIZON,
            "grace": LATE_GRACE,
        })
        result.queued = max(cur.rowcount or 0, 0)

        cur.execute(CANCEL_GONE_SQL)
        result.cancelled += max(cur.rowcount or 0, 0)
        cur.execute(CANCEL_INELIGIBLE_SQL)
        result.cancelled += max(cur.rowcount or 0, 0)

        cur.execute(EXPIRE_SQL, {"cutoff": now - LATE_GRACE})
        result.expired = max(cur.rowcount or 0, 0)
    _commit(conn)
    logger.info(
        "일정 DM 계획 queued=%d cancelled=%d expired=%d released=%d",
        result.queued, result.cancelled, result.expired, result.released,
    )
    return result


# --- 발송 -------------------------------------------------------------------
CLAIM_SQL = """
with due as (
    select id
      from schedule_dm_delivery
     where status in ('pending', 'retry')
       and scheduled_for <= %(now)s
       and scheduled_for >= %(floor)s
       and (next_attempt_at is null or next_attempt_at <= %(now)s)
     order by scheduled_for
     limit %(limit)s
     for update skip locked
)
update schedule_dm_delivery d
   set status = 'sending', locked_at = now(), locked_by = %(worker)s, updated_at = now()
  from due
 where d.id = due.id
returning d.id, d.source_folder_id, d.date_id, d.emp_no, d.workspace, d.slack_user,
          d.reminder_minutes, d.scheduled_for, d.attempts
"""

# 발송 직전에 제목·장소를 읽는다. 큐에는 본문을 저장하지 않는다.
OCCURRENCE_SQL = """
select subject, place, starts_at, ends_at
  from schedule_occurrence
 where source_folder_id = %(folder)s and date_id = %(date_id)s
   and source_deleted_at is null
"""

MARK_SENT_SQL = """
update schedule_dm_delivery
   set status = 'sent', sent_at = now(), slack_message_ts = %(ts)s,
       locked_at = null, locked_by = null, last_error = null, updated_at = now()
 where id = %(id)s
"""

MARK_RETRY_SQL = """
update schedule_dm_delivery
   set status = 'retry', attempts = attempts + 1, next_attempt_at = %(next_at)s,
       locked_at = null, locked_by = null, last_error = %(error)s, updated_at = now()
 where id = %(id)s
"""

MARK_FAILED_SQL = """
update schedule_dm_delivery
   set status = %(status)s, attempts = attempts + 1,
       locked_at = null, locked_by = null, last_error = %(error)s, updated_at = now()
 where id = %(id)s
"""


@dataclass(frozen=True)
class Due:
    id: int
    source_folder_id: int
    date_id: int
    emp_no: str
    workspace: str
    slack_user: str
    reminder_minutes: int
    scheduled_for: datetime
    attempts: int


_DUE_FIELDS = (
    "id", "source_folder_id", "date_id", "emp_no", "workspace", "slack_user",
    "reminder_minutes", "scheduled_for", "attempts",
)


def claim(conn, *, now: datetime | None = None, limit: int = 50, worker: str = "") -> list[Due]:
    """보낼 차례가 된 것을 잡는다. 여러 워커가 같은 행을 잡지 않는다.

    `FOR UPDATE SKIP LOCKED` 로 서로를 기다리지 않고 건너뛴다.
    """
    now = now or datetime.now(UTC)
    worker = worker or f"{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute(CLAIM_SQL, {
            "now": now,
            "floor": now - LATE_GRACE,
            "limit": limit,
            "worker": worker[:64],
        })
        rows = cur.fetchall()
    _commit(conn)
    out: list[Due] = []
    for raw in rows:
        r = raw if isinstance(raw, dict) else dict(zip(_DUE_FIELDS, raw, strict=False))
        out.append(Due(
            id=int(r["id"]),
            source_folder_id=int(r["source_folder_id"]),
            date_id=int(r["date_id"]),
            emp_no=str(r["emp_no"]),
            workspace=str(r["workspace"]),
            slack_user=str(r["slack_user"]),
            reminder_minutes=int(r["reminder_minutes"]),
            scheduled_for=r["scheduled_for"],
            attempts=int(r["attempts"] or 0),
        ))
    return out


def load_occurrence(conn, due: Due) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(OCCURRENCE_SQL, {"folder": due.source_folder_id, "date_id": due.date_id})
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return row
    return dict(zip(("subject", "place", "starts_at", "ends_at"), row, strict=False))


def render(occurrence: dict, minutes: int) -> str:
    """DM 본문. 비어 있는 값을 추론하지 않는다.

    제목·장소는 종료 7일 뒤 비워진다(보존 정책). 비면 장소 줄을 빼고 제목은
    '제목 없음' 으로 둔다 — 그럴듯한 말을 지어내면 사람이 그것을 사실로 받는다.
    """
    starts = occurrence.get("starts_at")
    when = starts.astimezone(KST).strftime("%H:%M") if starts else ""
    subject = (occurrence.get("subject") or "").strip() or "제목 없음"
    lines = [f"*[{minutes}분 전]* {when} {subject}".strip()]
    place = (occurrence.get("place") or "").strip()
    if place:
        lines.append(f"장소: {place}")
    lines.append("그룹웨어 팀 일정 기준입니다. 변경 여부는 원본 일정을 확인해 주세요.")
    return "\n".join(lines)


def backoff(attempts: int, *, now: datetime) -> datetime:
    return now + BACKOFF_BASE * (2 ** max(attempts, 0))


def error_code(exc: Exception) -> str:
    """Slack 오류에서 코드만 뽑는다. 본문·사용자 이름은 남기지 않는다."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            code = response.get("error")
        except Exception:  # noqa: BLE001 - 응답 형태가 달라도 기록은 계속한다
            code = None
        if code:
            return str(code)[:80]
    return f"{exc.__class__.__name__}"[:80]


def is_permanent(code: str) -> bool:
    return code in PERMANENT_ERRORS


@dataclass
class SendResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0


def send_due(
    conn,
    clients: dict,
    *,
    now: datetime | None = None,
    limit: int = 50,
    worker: str = "",
) -> SendResult:
    """잡은 큐를 실제로 보낸다.

    `clients` 는 워크스페이스 키 → Slack 클라이언트. 큐 행의 워크스페이스와 **정확히
    일치**하는 클라이언트로만 보낸다. 없는 키는 그 행만 실패시키고 나머지는 계속한다 —
    한 워크스페이스의 설정 오류가 전체 발송을 멈추면 안 된다.
    """
    now = now or datetime.now(UTC)
    result = SendResult()
    for due in claim(conn, now=now, limit=limit, worker=worker):
        client = clients.get(due.workspace)
        if client is None:
            _mark_failed(conn, due, "workspace_not_configured", status="failed")
            result.failed += 1
            continue

        occurrence = load_occurrence(conn, due)
        if occurrence is None:
            # 잡은 뒤 일정이 사라졌다. 보내지 않고 취소로 닫는다.
            _mark_failed(conn, due, "occurrence_gone", status="cancelled")
            result.skipped += 1
            continue

        try:
            resp = client.chat_postMessage(
                channel=due.slack_user,
                text=render(occurrence, due.reminder_minutes),
            )
        except Exception as e:  # noqa: BLE001 - 한 건 실패가 나머지를 막지 않는다
            code = error_code(e)
            if is_permanent(code) or due.attempts + 1 >= MAX_ATTEMPTS:
                _mark_failed(conn, due, code, status="failed")
                result.failed += 1
            else:
                _mark_retry(conn, due, code, now=now)
                result.retried += 1
            # 사번·워크스페이스·오류 코드만 남긴다. 제목·장소·이름은 넣지 않는다.
            logger.warning(
                "일정 DM 실패 emp=%s ws=%s folder=%s date=%s code=%s",
                due.emp_no, due.workspace, due.source_folder_id, due.date_id, code,
            )
            continue

        ts = ""
        try:
            ts = str(resp.get("ts") or "")
        except Exception:  # noqa: BLE001 - 응답 형태가 달라도 발송은 성공했다
            ts = ""
        with conn.cursor() as cur:
            cur.execute(MARK_SENT_SQL, {"id": due.id, "ts": ts})
        _commit(conn)
        result.sent += 1
        logger.info(
            "일정 DM 발송 emp=%s ws=%s folder=%s date=%s before=%d분",
            due.emp_no, due.workspace, due.source_folder_id, due.date_id,
            due.reminder_minutes,
        )
    return result


def _mark_retry(conn, due: Due, code: str, *, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(MARK_RETRY_SQL, {
            "id": due.id,
            "next_at": backoff(due.attempts, now=now),
            "error": code,
        })
    _commit(conn)


def _mark_failed(conn, due: Due, code: str, *, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(MARK_FAILED_SQL, {"id": due.id, "status": status, "error": code})
    _commit(conn)

# --- Slack 화면 --------------------------------------------------------------
#
# 복잡한 명령 인자를 외우게 하지 않는다. `/일정 알림` 한 번이면 현재 상태와 버튼이 뜬다.
ACTION_ENABLE = "schedule_dm_on"
ACTION_OFF = "schedule_dm_off"
ACTION_MINUTES = "schedule_dm_minutes"

NEED_IDENTITY = (
    "계정 연결이 필요합니다. 관리자에게 Slack 계정과 사번 연결을 요청해 주세요.\n"
    "연결 전에는 개인 일정 알림을 켤 수 없습니다."
)
UNAVAILABLE = (
    "일정 알림 설정이 아직 준비되지 않았습니다. 관리자에게 문의해 주세요."
)

_MINUTE_CHOICES = (
    ((30,), "30분 전"),
    ((10,), "10분 전"),
    ((30, 10), "둘 다"),
)


def minutes_label(minutes) -> str:
    picked = normalize_minutes(minutes)
    return next((label for value, label in _MINUTE_CHOICES if value == picked), "30분 전")


def settings_blocks(pref: Preference | None, *, workspace_label: str = "") -> list[dict]:
    """`/일정 알림` 화면. 지금 상태를 먼저 말하고 버튼을 준다."""
    on = bool(pref and pref.enabled)
    if on:
        head = (
            f"*일정 알림: 켜짐* · {minutes_label(pref.minutes)}\n"
            "회의 시작 전에 개인 DM 으로 알려 드립니다."
        )
    else:
        head = (
            "*일정 알림: 꺼짐*\n"
            "켜면 회의 시작 전에 개인 DM 으로 알려 드립니다. "
            "그룹웨어 팀 일정 중 승인된 폴더만 대상입니다."
        )

    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    if on and workspace_label:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"받는 곳: {workspace_label}. 다른 워크스페이스에서 켜면 그쪽으로 옮겨집니다.",
            }],
        })

    picked = normalize_minutes(pref.minutes) if pref else DEFAULT_MINUTES
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": f"{ACTION_MINUTES}:{'-'.join(str(m) for m in value)}",
                "text": {"type": "plain_text", "text": label},
                "value": "-".join(str(m) for m in value),
                **({"style": "primary"} if on and value == picked else {}),
            }
            for value, label in _MINUTE_CHOICES
        ],
    })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_OFF if on else ACTION_ENABLE,
                "text": {"type": "plain_text", "text": "알림 끄기" if on else "알림 받기"},
                **({"style": "danger"} if on else {"style": "primary"}),
            }
        ],
    })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "종일 일정은 알리지 않습니다. 지난 알림은 몰아서 보내지 않습니다.",
        }],
    })
    return blocks


def moved_notice(previous_workspace: str) -> str:
    return (
        f"이전에는 `{previous_workspace}` 에서 받고 계셨습니다. "
        "지금부터는 여기로만 보내드립니다."
    )


# --- 실행 진입점 --------------------------------------------------------------
def build_clients(configs) -> dict:
    """워크스페이스 키 → Slack 클라이언트.

    큐 행의 워크스페이스와 **정확히 일치**하는 클라이언트로만 보낸다. DB 에는 있는데
    환경변수에 없는 키는 여기서 빠지고, 그 행만 실패 처리된다(다른 워크스페이스는 계속).
    """
    from slack_sdk import WebClient

    return {c.key: WebClient(token=c.bot_token) for c in configs}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="일정 알림 개인 DM 큐 생성·발송")
    ap.add_argument("--plan-only", action="store_true", help="큐만 만들고 보내지 않는다")
    ap.add_argument("--send-only", action="store_true", help="큐 생성 없이 보내기만 한다")
    ap.add_argument("--limit", type=int, default=50, help="한 회차 최대 발송 건수")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")

    from .envfile import load_env_file

    load_env_file()

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL 이 없다. 일정 DM 은 동기화된 PostgreSQL 만 쓴다.")
        return 2

    from .workspaces import ConfigError, load_workspaces

    try:
        configs = load_workspaces()
    except ConfigError as e:
        logger.error("워크스페이스 설정 오류: %s", e)
        return 2

    try:
        import psycopg
    except ImportError:
        logger.error("psycopg 가 없다:  pip install 'psycopg[binary]'")
        return 2

    # 조회용 연결(db.connect)은 autocommit 이다. 발송은 상태 전이를 트랜잭션으로
    # 묶어야 하므로 여기서 자기 연결을 연다.
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row) as conn:
        if not args.send_only:
            plan(conn)
        if args.plan_only:
            return 0
        result = send_due(conn, build_clients(configs), limit=args.limit)
    logger.info(
        "일정 DM 발송 sent=%d retry=%d failed=%d skipped=%d",
        result.sent, result.retried, result.failed, result.skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
