"""일정 알림 개인 DM.

설계: docs/design/schedule-dm-reminders.md §11 의 필수 테스트를 그대로 고정한다.

가장 중요한 성질 셋:
- 같은 사람에게 한 번만 (멱등 키가 Slack ID 가 아니라 사번)
- 권한을 추측하지 않는다 (사슬이 전부 이어질 때만)
- 제목·장소·이름을 로그·DB 에 남기지 않는다
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import Mock

import pytest

from tybot import schedule_dm as dm
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
    client_message_id,
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
        # **구체적인 표시를 먼저 본다.** PLAN_SQL 과 DIAGNOSE_SQL 은 둘 다 CTE·하위
        # 질의에서 `user_identity` 를 읽으므로, 그 검사가 앞에 오면 계획·진단 쿼리가
        # 신원 조회로 잘못 분류된다. 그러면 fake 가 빈 결과를 주고, 테스트는
        # 「아무것도 안 했다」 로 조용히 통과하거나 엉뚱한 곳에서 깨진다.
        if "insert into schedule_dm_delivery" in s:
            return "plan"
        if "as upcoming" in s:
            return "diagnose"
        if "from user_identity ui" in s:
            return "identity"
        if "from schedule_dm_preference p" in s and "select" in s:
            return "pref"
        if "with due as" in s:
            return "claim"
        if "from schedule_occurrence" in s and "select subject" in s:
            return "occurrence"
        if "join schedule_folder_org fo on fo.org_code = e.org_code" in s:
            return "entitled"
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
    """폴더 활성 → 조직 ACL → 재직자 → 검증된 신원. 하나라도 빠지면 0건.

    2026-09-11 부터 **설정은 사슬에 없다** — 기본이 수신이다. 남은 사슬은 전부
    「그 부서가 그 폴더를 볼 수 있는가」 와 「보낼 곳이 확인됐는가」 다.
    """
    for needle in (
        "schedule_folder f",
        "schedule_folder_org fo",
        "recipient r on r.org_code = fo.org_code",
        "e.active",
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
    assert "unnest(r.reminder_minutes)" in PLAN_SQL


# --- 기본 수신 (2026-09-11 오너 결정) ----------------------------------------
#
# 예전에는 `/일정 알림` 을 켠 사람만 받았다. 그래서 기능이 있는데도 수신자가 0명인
# 상태가 오래 갔다. 소속 부서에 열린 폴더면 그룹웨어에서 이미 볼 수 있는 일정이므로,
# 기본을 수신으로 두고 **끄는 쪽**을 사람이 고른다.
def test_no_preference_row_still_receives():
    """설정 행이 없어도 받는다. `left join` 이 그 보장이다."""
    assert "left join schedule_dm_preference p on p.emp_no = e.emp_no" in PLAN_SQL
    assert "p.emp_no is null or p.enabled" in PLAN_SQL


def test_explicit_opt_out_is_respected():
    """사람이 끈 것을 기본값이 되살리면 끌 방법이 없다."""
    assert "p.emp_no is null or p.enabled" in PLAN_SQL
    # 켜진 설정만 통과하는 예전 조인은 사라졌다.
    assert "schedule_dm_preference p on p.emp_no = e.emp_no and p.enabled" not in PLAN_SQL


def test_default_minutes_is_ten():
    from tybot.schedule_dm import DEFAULT_MINUTES

    assert DEFAULT_MINUTES == (10,)


def test_default_minutes_is_bound_not_hardcoded():
    """기본값이 SQL 안에 박히면 코드와 DB 가 다른 값을 쓰게 된다."""
    assert "%(default_minutes)s" in PLAN_SQL


def test_one_recipient_per_person():
    """여러 워크스페이스에 있어도 한 곳만. 아니면 같은 사람이 여러 번 받는다."""
    assert "distinct on (ui.emp_no)" in PLAN_SQL
    assert "order by ui.emp_no, ui.verified_at desc" in PLAN_SQL


def test_preference_wins_over_the_default():
    for fragment in (
        "coalesce(p.workspace, c.workspace)",
        "coalesce(p.slack_user, c.slack_user)",
        "coalesce(p.reminder_minutes, %(default_minutes)s)",
    ):
        assert fragment in PLAN_SQL, fragment


def test_send_target_is_revalidated():
    """설정에 남은 옛 워크스페이스로 보내지 않는다. 그 계정은 이미 없을 수 있다."""
    assert "ui.workspace = r.workspace" in PLAN_SQL
    assert "ui.slack_user = r.slack_user" in PLAN_SQL
    assert "ui.emp_no = r.emp_no" in PLAN_SQL


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
    assert client.sent[0]["client_msg_id"] == client_message_id(Due(**_due()))
    assert "slack_message_ts = %(ts)s" in conn.sql_for("status = 'sent'")


def test_client_message_id_is_stable_for_the_delivery_key():
    first = Due(**_due(id=1, workspace="tyit", slack_user="U1"))
    retried = Due(**_due(id=99, workspace="mgmt", slack_user="U2", attempts=4))
    assert client_message_id(first) == client_message_id(retried)
    assert client_message_id(first) != client_message_id(
        Due(**_due(reminder_minutes=10))
    )


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


def test_normalize_minutes_ignores_malformed_values():
    assert normalize_minutes(["30", None, "invalid", 10]) == (30, 10)


# --- 설정 화면 ----------------------------------------------------------------
def test_panel_says_the_current_state_first():
    on = settings_blocks(Preference("E1", "tyit", "U1", (30,), True))
    assert "켜짐" in on[0]["text"]["text"]
    off = settings_blocks(Preference("E1", "tyit", "U1", (10,), False))
    assert "꺼짐" in off[0]["text"]["text"]


# 설정 행이 없는 것을 「꺼짐」 으로 보이면 화면이 거짓말한다 — 실제로는 받고 있다.
# 그 사람은 「켜기」 를 누르고 달라진 것이 없어 혼란스러워한다(2026-09-11).
def test_panel_without_a_preference_says_it_is_already_receiving():
    head = settings_blocks(None)[0]["text"]["text"]
    assert "받는 중" in head
    assert "꺼짐" not in head
    assert "기본" in head
    assert "10분 전" in head


def test_panel_without_a_preference_offers_turning_it_off():
    """받고 있으니 줄 버튼은 끄기다. 「켜기」 는 누를 이유가 없다."""
    actions = [b for b in settings_blocks(None) if b["type"] == "actions"]
    assert actions[1]["elements"][0]["action_id"] == ACTION_OFF


def test_panel_offers_three_minute_choices():
    blocks = settings_blocks(None)
    actions = [b for b in blocks if b["type"] == "actions"]
    labels = [e["text"]["text"] for e in actions[0]["elements"]]
    assert labels == ["30분 전", "10분 전", "둘 다"]


def test_panel_offers_turning_it_back_on_only_when_off():
    off = Preference("E1", "tyit", "U1", (10,), False)
    actions = [b for b in settings_blocks(off) if b["type"] == "actions"]
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


def test_workspace_bot_opens_the_reminder_panel(monkeypatch):
    """슬래시 핸들러가 호출하는 메서드가 실제로 설정 화면까지 이어져야 한다."""
    import tybot.slack.pilot as pilot_mod

    bot = pilot_mod.WorkspaceBot.__new__(pilot_mod.WorkspaceBot)
    bot.workspace = "tyit"
    conn = FakeConn(identity=[{"emp_no": "E1"}], pref=[])
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(conn))
    respond = Mock()

    bot._schedule_reminder_panel(respond, "U1")

    sent = respond.call_args.kwargs
    assert sent["response_type"] == "ephemeral"
    # 설정 행이 없는 사람도 기본으로 받는 중이다.
    assert "받는 중" in sent["blocks"][0]["text"]["text"]


def test_workspace_bot_moves_the_reminder_destination(monkeypatch):
    """다른 워크스페이스에서 켜면 현재 워크스페이스가 대표 수신 위치가 된다."""
    import tybot.slack.pilot as pilot_mod

    bot = pilot_mod.WorkspaceBot.__new__(pilot_mod.WorkspaceBot)
    bot.workspace = "tyit"
    previous = Preference("E1", "mgmt", "U2", (30,), True)
    current = Preference("E1", "tyit", "U1", (30,), True)
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(pilot_mod.schedule_dm, "resolve_emp_no", lambda *a, **kw: "E1")
    monkeypatch.setattr(pilot_mod.schedule_dm, "get_preference", lambda *a, **kw: previous)
    enable_mock = Mock(return_value=current)
    monkeypatch.setattr(pilot_mod.schedule_dm, "enable", enable_mock)
    respond = Mock()

    bot._set_schedule_dm(
        {"user": {"id": "U1"}}, respond, minutes=None, turn_on=True
    )

    assert enable_mock.call_args.kwargs["workspace"] == "tyit"
    sent = respond.call_args.kwargs
    assert sent["replace_original"] is True
    assert "mgmt" in str(sent["blocks"])


# --- Due 계약 -----------------------------------------------------------------
def test_due_carries_no_body_fields():
    """큐 행에 제목·장소가 실리면 그 순간 로그·이력으로 샌다."""
    fields = set(Due.__dataclass_fields__)
    assert "subject" not in fields
    assert "place" not in fields
    assert {"emp_no", "workspace", "slack_user", "reminder_minutes"} <= fields


# --- 큐가 빈 이유 --------------------------------------------------------------
# queued=0 은 정상일 때가 많다(앞으로 2시간에 회의가 없으면 당연히 0이다).
# 문제는 **정상과 고장을 구별할 수 없다**는 것이었다. 실제로 폴더-조직 매핑이 비어
# 영원히 0건인 상태를 '오늘은 회의가 없나 보다' 로 넘길 뻔했다(2026-09-02).
def _diag(caplog, **counts):
    row = {
        "upcoming": 0, "folders": 0, "folder_orgs": 0, "opted_out": 0, "identities": 0,
    }
    row.update(counts)
    conn = FakeConn(diagnose=[row])
    with caplog.at_level("INFO", logger="tybot.schedule_dm"):
        dm._log_why_empty(conn, NOW)
    return caplog.text


def test_empty_queue_reports_missing_folder_org_mapping(caplog):
    text = _diag(caplog, folders=2, identities=3)
    assert "schedule_folder_org" in text
    assert "DM 이 나가지 않습니다" in text


def test_empty_queue_reports_missing_identity_mapping(caplog):
    """기본 수신이라 설정은 관문이 아니다. 관문은 **누구에게 보낼지** 다."""
    text = _diag(caplog, folders=2, folder_orgs=1, identities=0)
    assert "사번" in text
    assert "DM 이 나가지 않습니다" in text
    # 없는 설정 단계를 하라고 보내지 않는다.
    assert "`/일정 알림` 으로 켭니다" not in text


def test_quiet_period_is_not_reported_as_a_problem(caplog):
    """설정이 다 갖춰졌고 단지 회의가 없는 것은 고장이 아니다."""
    text = _diag(caplog, folders=2, folder_orgs=1, identities=3, upcoming=0)
    assert "정상" in text
    assert "DM 이 나가지 않습니다" not in text


def test_settings_gap_wins_over_quiet_period(caplog):
    """일정이 없어도 설정이 비어 있으면 그쪽을 알려야 한다.

    '지금 일정 없음' 으로 끝내면 영원히 0건인 상태를 눈치채지 못한다.
    """
    text = _diag(caplog, folders=2, upcoming=0)
    assert "DM 이 나가지 않습니다" in text
    assert "정상" not in text


def test_diagnosis_failure_does_not_break_planning(caplog):
    class Broken(FakeConn):
        def cursor(self):
            raise RuntimeError("db down")

    with caplog.at_level("WARNING", logger="tybot.schedule_dm"):
        dm._log_why_empty(Broken(), NOW)
    assert "확인하지 못했습니다" in caplog.text


# --- 개인 자격 ----------------------------------------------------------------
#
# 2026-09-11: 사용자가 `/일정 알림` 을 켰는데 DM 이 오지 않았다. 설정은 저장됐고
# 일정도 있었지만 `schedule_folder_org` 가 비어 있어 자격이 0이었다.
#
# 진단(`_log_empty_reason`)은 같은 사실을 이미 판단하지만 **서버 로그로만** 나간다.
# 로그는 켠 사람이 볼 수 없는 곳이다. 그래서 켤 때 그 사람에게 말해야 한다.
def test_entitled_counts_only_approved_and_enabled():
    from tybot.schedule_dm import entitled_folders

    conn = FakeConn(entitled=[{"n": 2}])
    assert entitled_folders(conn, "3420-M") == 2
    sql = conn.sql_for("schedule_folder_org")
    # 승인(fo.enabled)과 폴더 사용(f.enabled) 둘 다 봐야 한다.
    assert "fo.enabled" in sql
    assert "f.enabled" in sql
    assert "e.active" in sql


def test_entitled_is_zero_when_nothing_is_approved():
    from tybot.schedule_dm import entitled_folders

    assert entitled_folders(FakeConn(entitled=[{"n": 0}]), "3420-M") == 0
    assert entitled_folders(FakeConn(entitled=[]), "3420-M") == 0


def test_entitled_needs_an_employee_number():
    from tybot.schedule_dm import entitled_folders

    conn = FakeConn()
    assert entitled_folders(conn, "") == 0
    assert conn.executed == []  # 빈 사번으로 DB 를 때리지 않는다


def test_entitlement_uses_the_same_joins_as_the_planner():
    """갈라지면 화면은 「받을 수 있다」 고 하는데 큐는 안 생긴다 — 조용한 침묵이다."""
    from tybot.schedule_dm import ENTITLED_SQL, PLAN_SQL

    for fragment in ("schedule_folder_org", "fo.enabled", "f.enabled", "e.active"):
        assert fragment in ENTITLED_SQL, fragment
        assert fragment in PLAN_SQL, fragment


def test_not_entitled_notice_points_at_the_groupware():
    """자격의 근거는 그룹웨어 폴더 ACL 이다. 우리 승인이 아니다(2026-09-11).

    「관리자 승인을 기다려라」 라고 하면 조치할 수 없는 곳으로 보내는 것이 된다 —
    우리 쪽에는 승인 단계가 없다.
    """
    from tybot.schedule_dm import NOT_ENTITLED

    assert "오지 않습니다" in NOT_ENTITLED
    assert "그룹웨어" in NOT_ENTITLED
    assert "다시 켤 필요는 없습니다" in NOT_ENTITLED
    # 없는 승인 절차를 기다리게 하지 않는다.
    assert "승인" not in NOT_ENTITLED
