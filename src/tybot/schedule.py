"""`/일정` — 그룹웨어 팀 일정 조회.

설계: [`docs/design/schedule-command.md`](../../docs/design/schedule-command.md)

## 데이터
Oracle 을 직접 조회하지 않는다. 1분마다 동기화된 `schedule_occurrence` 를 읽는다.
사람 수만큼 내부망 조회가 늘면 내부망 장애가 Slack 응답 실패로 그대로 번진다.

## 권한 — 설계 문서와 다르게 간 곳
설계 문서 §1 의 예시 SQL 은 `schedule_channel` 을 **워크스페이스 단위로만** 조인한다.
그대로 하면 워크스페이스 안 어느 채널에서 물어도 **다른 팀 폴더까지 보인다.**
원칙 3(막는 쪽이 기본값)에 맞게 좁혔다. 범위를 좁은 것부터 시도한다:

1. 명령을 실행한 채널이 `schedule_channel` 에 등록돼 있으면 **그 채널에 연결된 폴더만**
2. 아니면 신원 매핑(`user_identity` → `employee.org_code` → `schedule_folder.org_code`)
3. 둘 다 없으면 **아무것도 보여주지 않는다.** 추측하면 남의 팀 일정이 보인다

## 남기지 않는 것
`subject`·`place` 는 로그에 남기지 않는다. 아카이브에도 쓰지 않는다(원칙 1·5).
남길 것은 건수·`date_id`·`source_folder_id` 다.

## 비어 있는 제목
일정 종료 7일 뒤 제목·장소가 NULL 로 비워진다(`details_purged_at`). 정상 상태로
처리하고 대체 문구를 쓴다 — 예외를 던지지 않는다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

logger = logging.getLogger("tybot.schedule")

KST = timezone(timedelta(hours=9))
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")

# 이 시간을 넘게 동기화가 밀리면 사용자에게 알린다.
# 조용히 낡은 데이터를 보여주는 것이 가장 나쁘다.
STALE_AFTER = timedelta(minutes=5)

# 한 번에 보여줄 상한. 넘치면 잘라내고 그 사실을 알린다.
MAX_ROWS = 40

NO_SUBJECT = "(제목 없음)"

SCOPE_CHANNEL = "channel"
SCOPE_ORG = "org"
SCOPE_NONE = "none"


@dataclass(frozen=True)
class Occurrence:
    starts_at: datetime
    ends_at: datetime
    subject: str | None
    place: str | None
    is_all_day: bool
    source_folder_id: int
    folder_label: str = ""


@dataclass(frozen=True)
class Window:
    label: str
    start: datetime
    end: datetime


# --- 기간 해석 ---------------------------------------------------------------
_MONTH_RE = re.compile(r"^(\d{1,2})\s*월$")


def _day(now: datetime, offset: int = 0) -> datetime:
    base = now.astimezone(KST) + timedelta(days=offset)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_window(text: str, *, now: datetime | None = None) -> Window | None:
    """`/일정` 뒤에 적은 기간 표현을 해석한다. 모르는 표현은 None.

    None 을 돌려주는 이유: 임의로 기본값을 쓰면 사용자는 자기가 물은 기간을 봤다고
    착각한다. 무엇을 모르는지 말하는 편이 낫다.
    """
    now = (now or datetime.now(UTC)).astimezone(KST)
    t = (text or "").strip().replace(" ", "")

    if t in ("", "오늘내일"):
        start = _day(now)
        return Window("오늘·내일", start, start + timedelta(days=2))
    if t in ("오늘", "today"):
        start = _day(now)
        return Window("오늘", start, start + timedelta(days=1))
    if t in ("내일", "tomorrow"):
        start = _day(now, 1)
        return Window("내일", start, start + timedelta(days=1))
    if t in ("모레",):
        start = _day(now, 2)
        return Window("모레", start, start + timedelta(days=1))
    if t in ("이번주", "금주", "thisweek"):
        start = _day(now, -now.weekday())
        return Window("이번주", start, start + timedelta(days=7))
    if t in ("다음주", "내주", "nextweek"):
        start = _day(now, 7 - now.weekday())
        return Window("다음주", start, start + timedelta(days=7))
    if t in ("이번달", "이달", "금월"):
        start = _day(now).replace(day=1)
        return Window(f"{start.year}년 {start.month}월", start, _next_month(start))
    if t in ("7일", "일주일", "한주"):
        start = _day(now)
        return Window("앞으로 7일", start, start + timedelta(days=7))

    m = _MONTH_RE.match(t)
    if m:
        month = int(m.group(1))
        if not 1 <= month <= 12:
            return None
        # 연도는 올해로 고정하고 **라벨에 연도를 적는다.** 다음 해로 추측하면
        # 사용자가 어느 해를 봤는지 알 수 없다.
        start = _day(now).replace(year=now.year, month=month, day=1)
        return Window(f"{start.year}년 {month}월", start, _next_month(start))
    return None


def _next_month(d: datetime) -> datetime:
    return d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)


# --- 조회 -------------------------------------------------------------------
_SELECT = """
    SELECT o.starts_at, o.ends_at, o.subject, o.place, o.is_all_day,
           o.source_folder_id, f.label AS folder_label
      FROM schedule_occurrence o
      JOIN schedule_folder f ON f.source_folder_id = o.source_folder_id
"""
_RANGE = """
       AND o.source_deleted_at IS NULL
       AND o.starts_at < %(end)s
       AND o.ends_at   > %(start)s
     ORDER BY o.starts_at, o.source_folder_id
     LIMIT %(limit)s
"""

CHANNEL_SQL = (
    _SELECT
    + """      JOIN schedule_channel c ON c.source_folder_id = o.source_folder_id
     WHERE c.workspace = %(workspace)s
       AND c.slack_channel_id = %(channel_id)s
       AND c.enabled
       AND f.enabled
"""
    + _RANGE
)

# 신원 매핑 경로. 매핑이 없으면 조인에서 0건이 되어 아무것도 나오지 않는다 - 의도한 동작이다.
ORG_SQL = (
    _SELECT
    + """      JOIN user_identity ui
        ON ui.workspace = %(workspace)s AND ui.slack_user = %(slack_user)s
      JOIN employee e ON e.emp_no = ui.emp_no AND e.active
     WHERE f.enabled
       AND (
            f.org_code = e.org_code
            OR EXISTS (
                SELECT 1 FROM schedule_folder_org fo
                 WHERE fo.source_folder_id = f.source_folder_id
                   AND fo.org_code = e.org_code
                   AND fo.enabled
            )
       )
"""
    + _RANGE
)

LAST_SYNC_SQL = """
    SELECT max(applied_at) AS applied_at
      FROM schedule_sync_run
     WHERE mode = 'live' AND status = 'applied'
"""

CHANNEL_REGISTERED_SQL = """
    SELECT 1
      FROM schedule_channel
     WHERE workspace = %(workspace)s AND slack_channel_id = %(channel_id)s AND enabled
     LIMIT 1
"""


# SELECT 순서와 같아야 한다. 튜플 커서로 조회해도 같은 필드를 얻기 위한 대응표다.
_FIELDS = (
    "starts_at", "ends_at", "subject", "place", "is_all_day",
    "source_folder_id", "folder_label",
)


def _as_dict(row) -> dict:
    """dict 커서와 튜플 커서를 모두 받는다 - 호출자의 psycopg 설정에 의존하지 않는다."""
    if isinstance(row, dict):
        return row
    return dict(zip(_FIELDS, row, strict=False))


def _rows(cur) -> list[Occurrence]:
    out: list[Occurrence] = []
    for raw in cur.fetchall():
        r = _as_dict(raw)
        out.append(
            Occurrence(
                starts_at=r["starts_at"],
                ends_at=r["ends_at"],
                subject=r["subject"],
                place=r["place"],
                is_all_day=bool(r["is_all_day"]),
                source_folder_id=int(r["source_folder_id"]),
                folder_label=str(r["folder_label"] or ""),
            )
        )
    return out


def channel_is_registered(conn, *, workspace: str, channel_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(CHANNEL_REGISTERED_SQL, {"workspace": workspace, "channel_id": channel_id})
        return cur.fetchone() is not None


def fetch(
    conn,
    *,
    workspace: str,
    channel_id: str,
    slack_user: str,
    window: Window,
    limit: int = MAX_ROWS,
) -> tuple[list[Occurrence], str]:
    """(일정, 적용된 범위)를 돌려준다. 좁은 범위부터 시도한다.

    범위를 넓히지 않는 이유: 채널이 등록돼 있으면 그 채널의 경계가 곧 권한이다.
    거기서 0건이 나왔다고 조직 전체로 넓히면, 등록 경계가 의미를 잃는다.
    """
    params = {
        "workspace": workspace,
        "channel_id": channel_id,
        "slack_user": slack_user,
        "start": window.start,
        "end": window.end,
        "limit": limit + 1,  # 잘렸는지 알기 위해 하나 더 받는다
    }
    if channel_id and channel_is_registered(conn, workspace=workspace, channel_id=channel_id):
        with conn.cursor() as cur:
            cur.execute(CHANNEL_SQL, params)
            return _rows(cur), SCOPE_CHANNEL

    if slack_user:
        with conn.cursor() as cur:
            cur.execute(ORG_SQL, params)
            rows = _rows(cur)
        if rows:
            return rows, SCOPE_ORG
        # 매핑이 없는 것과 매핑은 있는데 일정이 없는 것을 구분한다.
        return [], (SCOPE_ORG if _has_identity(conn, workspace, slack_user) else SCOPE_NONE)
    return [], SCOPE_NONE


def _has_identity(conn, workspace: str, slack_user: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM user_identity WHERE workspace = %s AND slack_user = %s LIMIT 1",
            (workspace, slack_user),
        )
        return cur.fetchone() is not None


def last_sync(conn) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(LAST_SYNC_SQL)
        row = cur.fetchone()
    if not row:
        return None
    value = row["applied_at"] if isinstance(row, dict) else row[0]
    return value


# --- 표시 -------------------------------------------------------------------
def _stamp(d: datetime) -> str:
    k = d.astimezone(KST)
    return f"{k.month}/{k.day}({WEEKDAYS[k.weekday()]})"


def _time(d: datetime) -> str:
    k = d.astimezone(KST)
    return f"{k.hour:02d}:{k.minute:02d}"


def format_when(o: Occurrence) -> str:
    """시각 표기. 모두 KST 로 보여준다.

    종일 일정은 시각을 적지 않는다 - `00:00~00:00` 처럼 보이면 사용자가 오해한다.
    """
    if o.is_all_day:
        start = o.starts_at.astimezone(KST)
        end = (o.ends_at - timedelta(seconds=1)).astimezone(KST)
        if start.date() == end.date():
            return f"{_stamp(o.starts_at)} 종일"
        return f"{_stamp(o.starts_at)}~{_stamp(end)} 종일"
    if o.starts_at.astimezone(KST).date() == o.ends_at.astimezone(KST).date():
        return f"{_stamp(o.starts_at)} {_time(o.starts_at)}~{_time(o.ends_at)}"
    return (
        f"{_stamp(o.starts_at)} {_time(o.starts_at)} ~ "
        f"{_stamp(o.ends_at)} {_time(o.ends_at)}"
    )


def format_reply(
    rows: list[Occurrence],
    *,
    window: Window,
    scope: str,
    synced_at: datetime | None,
    now: datetime | None = None,
    limit: int = MAX_ROWS,
) -> str:
    """사용자에게 보낼 문장. 여기서만 제목·장소를 다룬다(로그로는 나가지 않는다)."""
    now = now or datetime.now(UTC)

    if scope == SCOPE_NONE:
        return (
            f"*{window.label} 일정* — 보여드릴 수 있는 일정이 없습니다.\n"
            "이 채널은 일정 폴더에 연결되지 않았고, 회원님의 사번 매핑도 아직 없습니다.\n"
            "공지 채널에서 실행하시거나 관리자에게 계정 연결을 요청해 주세요."
        )

    truncated = len(rows) > limit
    rows = rows[:limit]
    head = f"*{window.label} 일정* — {len(rows)}건" + (" 이상" if truncated else "")
    if scope == SCOPE_CHANNEL:
        head += " (이 채널에 연결된 일정 폴더)"
    else:
        head += " (회원님 소속 부서)"

    lines = [head]
    if not rows:
        lines.append("해당 기간에 등록된 일정이 없습니다.")
    else:
        last_day = ""
        for o in rows:
            day = _stamp(o.starts_at)
            if day != last_day:
                lines.append(f"\n*{day}*")
                last_day = day
            when = format_when(o)
            # 종료 7일 뒤 제목·장소가 비워진다. 정상 상태다.
            subject = (o.subject or "").strip() or NO_SUBJECT
            row = f"• {when.split(' ', 1)[1] if ' ' in when else when} {subject}"
            place = (o.place or "").strip()
            if place:
                row += f" _({place})_"
            if o.folder_label:
                row += f" · {o.folder_label}"
            lines.append(row)

    if truncated:
        lines.append(f"\n_{limit}건까지만 표시했습니다. 기간을 좁혀 다시 조회해 주세요._")

    lines.append("")
    lines.append(sync_note(synced_at, now=now))
    return "\n".join(lines)


def sync_note(synced_at: datetime | None, *, now: datetime | None = None) -> str:
    """최신성 표기. 밀렸으면 반드시 말한다."""
    now = now or datetime.now(UTC)
    if synced_at is None:
        return "⚠️ _동기화 기록이 없습니다. 표시된 내용이 최신이 아닐 수 있습니다._"
    delay = now - synced_at
    stamp = synced_at.astimezone(KST).strftime("%m-%d %H:%M")
    if delay > STALE_AFTER:
        minutes = int(delay.total_seconds() // 60)
        return f"⚠️ _동기화 지연: 마지막 반영 {stamp} ({minutes}분 전)_"
    return f"_마지막 동기화 {stamp}_"


HELP = (
    "사용법: `/일정` · `/일정 오늘` · `/일정 내일` · `/일정 이번주` · `/일정 9월`\n"
    "명령만 입력하면 오늘·내일 일정을 보여드립니다.\n"
    "표시는 회원님만 볼 수 있으며, 일정 내용은 아카이브에 저장되지 않습니다."
)

UNAVAILABLE = (
    "일정 조회가 아직 준비되지 않았습니다.\n"
    "그룹웨어 일정 동기화가 설정되면 이 명령으로 조회할 수 있습니다. "
    "관리자에게 문의해 주세요."
)


def unknown_window(text: str) -> str:
    return (
        f"`{text[:40]}` 은 알 수 없는 기간 표현입니다.\n" + HELP
    )
