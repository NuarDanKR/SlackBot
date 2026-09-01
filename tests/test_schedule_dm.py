"""일정 알림 개인 DM.

설계: docs/design/schedule-dm-reminders.md §11 의 필수 테스트를 그대로 고정한다.

가장 중요한 성질 셋:
- 같은 사람에게 한 번만 (멱등 키가 Slack ID 가 아니라 사번)
- 권한을 추측하지 않는다 (사슬이 전부 이어질 때만)
- 제목·장소·이름을 로그·DB 에 남기지 않는다
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from tybot.schedule_dm import (
    ACTION_ENABLE,
    ACTION_MINUTES,
    ACTION_OFF,
    DEFAULT_MINUTES,
    KST,
    LATE_GRACE,
    MAX_ATTEMPTS,
    NEED_IDENTITY,
    PERMANENT_ERRORS,
    PLAN_SQL,
    Due,
    Preference,
    backoff,
    claim,
    disable,
    enable,
    error_code,
    get_preference,
    is_permanent,
    minutes_label,
    normalize_minutes,
    plan,
    render,
    resolve_emp_no,
    send_due,
    settings_blocks,
)

NOW = datetime(2026, 9, 1, 13, 30, tzinfo=KST)


# --- 가짜 DB ------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.executed.append((sql, params))
        self.c._last = sql

    def fetchone(self):
        rows = self.c.answers.get(self.c._key()) or []
        return rows[0] if rows else None

    def fetchall(self):
        return self.c.answers.get(self.c._key()) or []

    @property
    def rowcount(self):
        return self.c.rowcounts.get(self.c._key(), 0)


class FakeConn:
    autocommit = False

    def __init__(self, **answers):
        self.rowcounts = answers.pop("rowcounts", {})
        self.answers = answers
        self.executed: list[tuple] = []
        self.commits = 0
        self._last = ""

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def _key(self) -> str:
        s = self._last
        if "from user_identity ui" in s:
            return "identity"
        if "from schedule_dm_preference p" in s and "select" in s:
            return "pref"
        if "insert into schedule_dm_delivery" in s:
            return "plan"
        if "with due as" in s:
            return "claim"
        if "from schedule_occurrence" in s and "select subject" in s:
            return "occurrence"
        return "?"

    def sql_for(self, needle: str) -> str:
        for sql, _ in self.executed:
            if needle in sql:
                return sql
        raise AssertionError(f"{needle} 를 실행하지 않았다")

    def params_for(self, needle: str):
        for sql, params in self.executed:
            if needle in sql:
                return params
        raise AssertionError(f"{needle} 를 실행하지 않았다")

    def ran(self, needle: str) -> bool:
        return any(needle in sql for sql, _ in self.executed)


# --- 설정 값 ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([30], (30,)),
        ([10], (10,)),
        ([10, 30], (30, 10)),
        ([], DEFAULT_MINUTES),
        (None, DEFAULT_MINUTES),
        ([5, 60], DEFAULT_MINUTES),   # 허용 밖은 버린다
        ([30, 30], (30,)),
    ],
)
def test_normalize_minutes_matches_the_schema_check(given, expected):
    """DB CHECK 와 어긋나면 저장 시점에야 터진다. 화면에서 먼저 막는다."""
    assert normalize_minutes(given) == expected


def test_minutes_label():
    assert minutes_label([30]) == "30분 전"
    assert minutes_label([10, 30]) == "둘 다"


# --- 신원 ---------------------------------------------------------------------
def test_identity_requires_an_active_employee():
    conn = FakeConn(identity=[{"emp_no": "E1"}])
    assert resolve_emp_no(conn, workspace="tyit", slack_user="U1") == "E1"
    assert "e.active" in conn.sql_for("from user_identity ui")


def test_ambiguous_identity_is_refused():
    """여러 행이면 누구인지 모른다. 모호하면 보내지 않는다."""
    conn = FakeConn(identity=[{"emp_no": "E1"}, {"emp_no": "E2"}])
    assert resolve_emp_no(conn, workspace="tyit", slack_user="U1") is None


def test_missing_identity_is_none():
    assert resolve_emp_no(FakeConn(identity=[]), workspace="tyit", slack_user="U1") is None
    assert resolve_emp_no(FakeConn(), workspace="", slack_user="U1") is None


# --- 켜기·끄기 ----------------------------------------------------------------
def test_enable_moves_the_representative_workspace():
    """한 사람의 대표 수신 위치는 하나다. 다른 곳에서 켜면 옮겨간다."""
    conn = FakeConn()
    pref = enable(conn, emp_no="E1", workspace="tyit", slack_user="U9", minutes=[10, 30])
    assert pref.minutes == (30, 10)
    assert "on conflict (emp_no) do update" in conn.sql_for("insert into schedule_dm_preference")
    # 옛 경로의 미발송 건은 정리한다.
    params = conn.params_for("workspace <> %(workspace)s")
    assert params["workspace"] == "tyit"
    assert conn.commits == 1


def test_disable_keeps_history_but_cancels_pending():
    """이력을 지우면 '이미 보냈다' 는 근거가 사라져 중복 발송이 난다."""
    conn = FakeConn()
    disable(conn, emp_no="E1", actor="U1")
    assert "set enabled = false" in conn.sql_for("update schedule_dm_preference")
    assert "status = 'cancelled'" in conn.sql_for("schedule_dm_delivery")
    assert not conn.ran("delete from")


def test_get_preference_reads_minutes():
    conn = FakeConn(pref=[{
        "emp_no": "E1", "workspace": "tyit", "slack_user": "U1",
        "reminder_minutes": [30, 10], "enabled": True,
    }])
    pref = get_preference(conn, "E1")
    assert pref.minutes == (30, 10)
    assert pref.enabled is True


# --- 큐 생성 ------------------------------------------------------------------
def test_plan_requires_the_whole_permission_chain():
    """폴더 승인 → 조직 ACL → 재직자 → 설정 → 검증된 신원. 하나라도 빠지면 0건."""
    for needle in (
        "schedule_folder f",
        "schedule_folder_org fo",
        "employee e on e.org_code = fo.org_code and e.active",
        "schedule_dm_preference p on p.emp_no = e.emp_no and p.enabled",
        "user_identity ui",
    ):
        assert needle in PLAN_SQL


def test_plan_excludes_all_day_and_deleted():
    assert "not o.is_all_day" in PLAN_SQL
    assert "o.source_deleted_at is null" in PLAN_SQL


def test_plan_does_not_widen_to_the_org_tree():
    """부모·자식으로 넓히면 승인 절차가 무의미해진다."""
    assert "recursive" not in PLAN_SQL.lower()
    assert "parent_code" not in PLAN_SQL


def test_plan_is_idempotent_and_never_touches_sent_rows():
    assert "on conflict (source_folder_id, date_id, emp_no, reminder_minutes)" in PLAN_SQL
    assert "where schedule_dm_delivery.status in ('pending', 'retry', 'expired', 'cancelled')" in (
        PLAN_SQL
    )


def test_plan_creates_one_row_per_selected_minute():
    assert "unnest(p.reminder_minutes)" in PLAN_SQL


def test_plan_runs_the_cleanup_steps_in_order():
    conn = FakeConn(rowcounts={"plan": 3})
    result = plan(conn, now=NOW)
    order = [i for i, (sql, _) in enumerate(conn.executed)]
    assert result.queued == 3
    # 죽은 워커의 락을 먼저 풀어야 그 행이 이번 회차에 다시 잡힌다.
    assert conn.executed[0][0].strip().startswith("update schedule_dm_delivery")
    assert "locked_at <" in conn.executed[0][0]
    assert conn.ran("insert into schedule_dm_delivery")
    assert conn.ran("o.source_deleted_at is not null or o.is_all_day")
    assert conn.ran("not exists")
    assert conn.ran("status = 'expired'")
    assert order  # 실행 순서가 존재한다


def test_plan_cancels_when_the_occurrence_is_gone_or_all_day():
    conn = FakeConn()
    plan(conn, now=NOW)
    sql = conn.sql_for("o.source_deleted_at is not null")
    assert "status = 'cancelled'" in sql


def test_plan_cancels_when_the_person_is_no_longer_eligible():
    """전근·퇴직·설정 해제. 미발송 큐를 취소한다."""
    conn = FakeConn()
    plan(conn, now=NOW)
    sql = conn.sql_for("and not exists")
    assert "status = 'cancelled'" in sql
    assert "e.active" in sql


def test_plan_expires_late_rows():
    conn = FakeConn()
    plan(conn, now=NOW)
    assert conn.params_for("status = 'expired'")["cutoff"] == NOW - LATE_GRACE


# --- 발송 ---------------------------------------------------------------------
def _due(**kw) -> dict:
    base = {
        "id": 1, "source_folder_id": 654, "date_id": 11, "emp_no": "E1",
        "workspace": "tyit", "slack_user": "U1", "reminder_minutes": 30,
        "scheduled_for": NOW, "attempts": 0,
    }
    base.update(kw)
    return base


def _occ(**kw) -> dict:
    base = {
        "subject": "주간회의", "place": "본사 3층",
        "starts_at": NOW + timedelta(minutes=30), "ends_at": NOW + timedelta(minutes=90),
    }
    base.update(kw)
    return base


class FakeClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[dict] = []

    def chat_postMessage(self, **kw):
        if self.error:
            raise self.error
        self.sent.append(kw)
        return {"ts": "1.0"}


def test_claim_skips_locked_rows():
    """여러 워커가 같은 행을 잡으면 같은 사람에게 두 번 간다."""
    conn = FakeConn(claim=[_due()])
    claim(conn, now=NOW, worker="w1")
    sql = conn.sql_for("with due as")
    assert "for update skip locked" in sql
    assert "status = 'sending'" in sql


def test_claim_ignores_rows_that_are_too_late():
    conn = FakeConn(claim=[])
    claim(conn, now=NOW)
    assert conn.params_for("with due as")["floor"] == NOW - LATE_GRACE


def test_send_marks_sent_and_stores_only_the_ts():
    conn = FakeConn(claim=[_due()], occurrence=[_occ()])
    client = FakeClient()
    result = send_due(conn, {"tyit": client}, now=NOW)
    assert result.sent == 1
    assert client.sent[0]["channel"] == "U1"
    assert "주간회의" in client.sent[0]["text"]
    assert "slack_message_ts = %(ts)s" in conn.sql_for("status = 'sent'")


def test_send_fails_only_that_row_when_the_workspace_is_unknown():
    """한 워크스페이스의 설정 오류가 전체 발송을 멈추면 안 된다."""
    conn = FakeConn(claim=[_due(workspace="없는곳")], occurrence=[_occ()])
    result = send_due(conn, {"tyit": FakeClient()}, now=NOW)
    assert result.failed == 1
    assert conn.params_for("set status = %(status)s")["error"] == "workspace_not_configured"


def test_send_cancels_when_the_occurrence_vanished_after_claim():
    conn = FakeConn(claim=[_due()], occurrence=[])
    result = send_due(conn, {"tyit": FakeClient()}, now=NOW)
    assert result.skipped == 1
    assert conn.params_for("set status = %(status)s")["status"] == "cancelled"


def test_transient_error_retries_with_backoff():
    conn = FakeConn(claim=[_due(attempts=1)], occurrence=[_occ()])
    result = send_due(conn, {"tyit": FakeClient(RuntimeError("boom"))}, now=NOW)
    assert result.retried == 1
    params = conn.params_for("status = 'retry'")
    assert params["next_at"] > NOW


def test_permanent_error_does_not_retry():
    class SlackErr(Exception):
        response: ClassVar[dict] = {"error": "user_not_found"}

    conn = FakeConn(claim=[_due()], occurrence=[_occ()])
    result = send_due(conn, {"tyit": FakeClient(SlackErr())}, now=NOW)
    assert result.failed == 1
    assert conn.params_for("set status = %(status)s")["error"] == "user_not_found"


def test_retry_stops_at_the_attempt_cap():
    conn = FakeConn(claim=[_due(attempts=MAX_ATTEMPTS - 1)], occurrence=[_occ()])
    result = send_due(conn, {"tyit": FakeClient(RuntimeError("boom"))}, now=NOW)
    assert result.failed == 1


def test_backoff_grows():
    assert backoff(2, now=NOW) > backoff(1, now=NOW) > backoff(0, now=NOW)


def test_permanent_error_set_covers_the_documented_codes():
    for code in ("channel_not_found", "user_not_found", "account_inactive"):
        assert is_permanent(code)
    assert not is_permanent("ratelimited")
    assert PERMANENT_ERRORS


def test_error_code_never_carries_a_message_body():
    class SlackErr(Exception):
        response: ClassVar[dict] = {"error": "ratelimited"}

    assert error_code(SlackErr()) == "ratelimited"
    assert error_code(RuntimeError("주간회의 본문이 들어간 예외")) == "RuntimeError"


def test_logs_do_not_contain_subject_place_or_names(caplog):
    conn = FakeConn(claim=[_due()], occurrence=[_occ(subject="비밀 회의", place="비밀 장소")])
    with caplog.at_level("INFO"):
        send_due(conn, {"tyit": FakeClient()}, now=NOW)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "비밀 회의" not in logged
    assert "비밀 장소" not in logged
    assert "emp=E1" in logged


def test_delivery_row_never_stores_the_body():
    conn = FakeConn(claim=[_due()], occurrence=[_occ(subject="비밀 회의")])
    send_due(conn, {"tyit": FakeClient()}, now=NOW)
    written = " ".join(str(params) for _, params in conn.executed)
    assert "비밀 회의" not in written


# --- DM 문구 ------------------------------------------------------------------
def test_render_shows_kst_time_and_place():
    text = render(_occ(), 30)
    assert text.startswith("*[30분 전]* 14:00 주간회의")
    assert "장소: 본사 3층" in text
    assert "원본 일정을 확인해 주세요" in text


def test_render_handles_purged_details():
    """보존 기간이 지나면 제목·장소가 비워진다. 지어내지 않는다."""
    text = render(_occ(subject=None, place=None), 10)
    assert "제목 없음" in text
    assert "장소:" not in text


def test_render_uses_kst_for_utc_input():
    text = render(_occ(starts_at=datetime(2026, 9, 1, 5, 0, tzinfo=UTC)), 30)
    assert "14:00" in text


# --- 설정 화면 ----------------------------------------------------------------
def test_panel_says_the_current_state_first():
    on = settings_blocks(Preference("E1", "tyit", "U1", (30,), True))
    assert "켜짐" in on[0]["text"]["text"]
    off = settings_blocks(None)
    assert "꺼짐" in off[0]["text"]["text"]


def test_panel_offers_three_minute_choices_and_a_toggle():
    blocks = settings_blocks(None)
    actions = [b for b in blocks if b["type"] == "actions"]
    labels = [e["text"]["text"] for e in actions[0]["elements"]]
    assert labels == ["30분 전", "10분 전", "둘 다"]
    assert actions[1]["elements"][0]["action_id"] == ACTION_ENABLE


def test_panel_shows_turn_off_when_enabled():
    blocks = settings_blocks(Preference("E1", "tyit", "U1", (30,), True))
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions[1]["elements"][0]["action_id"] == ACTION_OFF


def test_minute_buttons_carry_their_value():
    blocks = settings_blocks(None)
    actions = [b for b in blocks if b["type"] == "actions"]
    values = [e["value"] for e in actions[0]["elements"]]
    assert values == ["30", "10", "30-10"]
    assert all(e["action_id"].startswith(ACTION_MINUTES) for e in actions[0]["elements"])


def test_panel_warns_that_turning_on_moves_the_destination():
    blocks = settings_blocks(
        Preference("E1", "tyit", "U1", (30,), True), workspace_label="전산팀"
    )
    text = " ".join(
        e["text"] for b in blocks if b["type"] == "context" for e in b["elements"]
    )
    assert "옮겨집니다" in text


def test_panel_states_the_all_day_rule():
    text = " ".join(
        e["text"] for b in settings_blocks(None) if b["type"] == "context"
        for e in b["elements"]
    )
    assert "종일 일정은 알리지 않습니다" in text


def test_identity_message_tells_the_user_what_to_do():
    assert "계정 연결" in NEED_IDENTITY
    assert "관리자" in NEED_IDENTITY


# --- Due 계약 -----------------------------------------------------------------
def test_due_carries_no_body_fields():
    """큐 행에 제목·장소가 실리면 그 순간 로그·이력으로 샌다."""
    fields = set(Due.__dataclass_fields__)
    assert "subject" not in fields
    assert "place" not in fields
    assert {"emp_no", "workspace", "slack_user", "reminder_minutes"} <= fields
