"""`/일정` — 기간 해석 · 권한 범위 · 표시.

설계: docs/design/schedule-command.md

DB 없이 돌린다. 커서를 흉내내는 가짜 연결로 SQL 파라미터와 범위 선택을 확인하고,
표시 부분은 순수 함수라 그대로 검증한다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tybot.schedule import (
    KST,
    NO_SUBJECT,
    SCOPE_CHANNEL,
    SCOPE_NONE,
    SCOPE_ORG,
    Occurrence,
    fetch,
    format_reply,
    format_when,
    last_sync,
    parse_window,
    sync_note,
    unknown_window,
)

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=KST)  # 2026-08-31 은 월요일


# --- 기간 해석 --------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "label", "days"),
    [
        ("", "오늘·내일", 2),
        ("오늘", "오늘", 1),
        ("내일", "내일", 1),
        ("모레", "모레", 1),
        ("이번주", "이번주", 7),
        ("다음주", "다음주", 7),
        ("7일", "앞으로 7일", 7),
    ],
)
def test_window_labels_and_length(text, label, days):
    w = parse_window(text, now=NOW)
    assert w.label == label
    assert (w.end - w.start) == timedelta(days=days)


def test_window_starts_at_kst_midnight():
    """KST 자정에서 시작해야 '오늘' 이 사용자 기준과 맞는다."""
    w = parse_window("오늘", now=NOW)
    k = w.start.astimezone(KST)
    assert (k.hour, k.minute, k.second) == (0, 0, 0)
    assert (k.year, k.month, k.day) == (2026, 8, 31)


def test_this_week_starts_on_monday():
    w = parse_window("이번주", now=datetime(2026, 9, 3, 9, 0, tzinfo=KST))  # 목요일
    assert w.start.astimezone(KST).day == 31  # 그 주 월요일 = 8/31


def test_month_label_includes_the_year():
    """연도를 추측하지 않고 라벨에 적는다 - 어느 해를 봤는지 알 수 있어야 한다."""
    w = parse_window("9월", now=NOW)
    assert w.label == "2026년 9월"
    assert w.start.astimezone(KST).month == 9
    assert w.end.astimezone(KST).month == 10


def test_december_rolls_over_to_next_january():
    w = parse_window("12월", now=NOW)
    assert w.end.astimezone(KST).year == 2027
    assert w.end.astimezone(KST).month == 1


@pytest.mark.parametrize("text", ["13월", "0월", "다다음주", "작년", "aaa"])
def test_unknown_expressions_return_none(text):
    """기본값으로 때우면 사용자는 자기가 물은 기간을 봤다고 착각한다."""
    assert parse_window(text, now=NOW) is None


def test_unknown_window_message_shows_usage():
    msg = unknown_window("다다음주")
    assert "다다음주" in msg
    assert "/일정 이번주" in msg


# --- 표시 -------------------------------------------------------------------
def _occ(**kw) -> Occurrence:
    base = dict(
        starts_at=datetime(2026, 8, 31, 14, 0, tzinfo=KST),
        ends_at=datetime(2026, 8, 31, 15, 0, tzinfo=KST),
        subject="주간 회의",
        place="본사 3층",
        is_all_day=False,
        source_folder_id=654,
        folder_label="업무(전산팀)",
    )
    base.update(kw)
    return Occurrence(**base)


def test_same_day_shows_start_and_end_time():
    assert format_when(_occ()) == "8/31(월) 14:00~15:00"


def test_all_day_hides_the_clock():
    """종일인데 00:00~00:00 으로 보이면 사용자가 오해한다."""
    text = format_when(
        _occ(
            is_all_day=True,
            starts_at=datetime(2026, 8, 31, 0, 0, tzinfo=KST),
            ends_at=datetime(2026, 9, 1, 0, 0, tzinfo=KST),
        )
    )
    assert text == "8/31(월) 종일"
    assert ":" not in text


def test_multi_day_all_day_shows_both_dates():
    text = format_when(
        _occ(
            is_all_day=True,
            starts_at=datetime(2026, 8, 31, 0, 0, tzinfo=KST),
            ends_at=datetime(2026, 9, 3, 0, 0, tzinfo=KST),
        )
    )
    assert text == "8/31(월)~9/2(수) 종일"


def test_overnight_event_shows_both_ends():
    text = format_when(
        _occ(
            starts_at=datetime(2026, 8, 31, 22, 0, tzinfo=KST),
            ends_at=datetime(2026, 9, 1, 2, 0, tzinfo=KST),
        )
    )
    assert "8/31(월) 22:00" in text and "9/1(화) 02:00" in text


def test_utc_input_is_displayed_in_kst():
    """추출기가 +09:00 을 붙인다. 표시가 KST 가 아니면 9시간 어긋난다(설계 §5)."""
    text = format_when(
        _occ(
            starts_at=datetime(2026, 8, 31, 5, 0, tzinfo=UTC),   # = 14:00 KST
            ends_at=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        )
    )
    assert text == "8/31(월) 14:00~15:00"


def test_purged_details_are_normal_not_an_error():
    """종료 7일 뒤 제목·장소가 NULL 로 비워진다. 예외를 던지지 않는다."""
    text = format_reply(
        [_occ(subject=None, place=None)],
        window=parse_window("오늘", now=NOW),
        scope=SCOPE_CHANNEL,
        synced_at=NOW,
        now=NOW,
    )
    assert NO_SUBJECT in text


def test_reply_includes_place_and_folder():
    text = format_reply(
        [_occ()],
        window=parse_window("오늘", now=NOW),
        scope=SCOPE_CHANNEL,
        synced_at=NOW,
        now=NOW,
    )
    assert "주간 회의" in text
    assert "본사 3층" in text
    assert "업무(전산팀)" in text


def test_reply_states_which_scope_was_used():
    """어느 범위를 본 것인지 모르면 '왜 안 보이지' 를 사용자가 판단할 수 없다."""
    w = parse_window("오늘", now=NOW)
    assert "이 채널에 연결된" in format_reply([_occ()], window=w, scope=SCOPE_CHANNEL,
                                            synced_at=NOW, now=NOW)
    assert "소속 부서" in format_reply([_occ()], window=w, scope=SCOPE_ORG,
                                      synced_at=NOW, now=NOW)


def test_no_scope_explains_both_reasons():
    text = format_reply([], window=parse_window("오늘", now=NOW), scope=SCOPE_NONE,
                        synced_at=NOW, now=NOW)
    assert "연결되지 않았고" in text
    assert "사번 매핑" in text


def test_org_scope_accepts_the_approved_many_to_many_folder_acl():
    from tybot.schedule import ORG_SQL

    assert "schedule_folder_org" in ORG_SQL
    assert "fo.org_code = e.org_code" in ORG_SQL


def test_empty_but_authorized_says_no_events():
    text = format_reply([], window=parse_window("오늘", now=NOW), scope=SCOPE_CHANNEL,
                        synced_at=NOW, now=NOW)
    assert "등록된 일정이 없습니다" in text


def test_truncation_is_announced():
    rows = [_occ() for _ in range(45)]
    text = format_reply(rows, window=parse_window("9월", now=NOW), scope=SCOPE_CHANNEL,
                        synced_at=NOW, now=NOW, limit=40)
    assert "40건까지만" in text
    assert "이상" in text


# --- 최신성 -----------------------------------------------------------------
def test_fresh_sync_is_quiet():
    note = sync_note(NOW - timedelta(minutes=1), now=NOW)
    assert "지연" not in note
    assert "마지막 동기화" in note


def test_stale_sync_is_flagged():
    """조용히 낡은 데이터를 보여주는 것이 가장 나쁘다(설계 §3)."""
    note = sync_note(NOW - timedelta(minutes=42), now=NOW)
    assert "동기화 지연" in note
    assert "42분 전" in note


def test_missing_sync_record_is_flagged():
    assert "동기화 기록이 없습니다" in sync_note(None, now=NOW)


# --- 권한 범위 --------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        self._conn._last = sql

    def fetchone(self):
        return self._conn.answers.get(self._key())

    def fetchall(self):
        return self._conn.answers.get(self._key()) or []

    def _key(self) -> str:
        sql = self._conn._last
        if "FROM schedule_channel" in sql and "SELECT 1" in sql:
            return "registered"
        if "FROM user_identity" in sql and "SELECT 1" in sql:
            return "identity"
        if "JOIN schedule_channel" in sql:
            return "channel_rows"
        if "JOIN user_identity" in sql:
            return "org_rows"
        if "schedule_sync_run" in sql:
            return "sync"
        return "?"


class FakeConn:
    def __init__(self, **answers):
        self.answers = answers
        self.executed: list[tuple] = []
        self._last = ""

    def cursor(self):
        return FakeCursor(self)


def _row(**kw) -> dict:
    base = {
        "starts_at": datetime(2026, 8, 31, 14, 0, tzinfo=KST),
        "ends_at": datetime(2026, 8, 31, 15, 0, tzinfo=KST),
        "subject": "주간 회의",
        "place": None,
        "is_all_day": False,
        "source_folder_id": 654,
        "folder_label": "업무(전산팀)",
    }
    base.update(kw)
    return base


def test_registered_channel_scope_filters_by_channel_id():
    """워크스페이스 단위로만 조인하면 다른 팀 폴더가 보인다 - 채널까지 좁힌다."""
    conn = FakeConn(registered={"?column?": 1}, channel_rows=[_row()])
    rows, scope = fetch(
        conn, workspace="pilot", channel_id="C1", slack_user="U1",
        window=parse_window("오늘", now=NOW),
    )
    assert scope == SCOPE_CHANNEL
    assert len(rows) == 1
    sql, params = conn.executed[-1]
    assert "c.slack_channel_id = %(channel_id)s" in sql
    assert params["channel_id"] == "C1"


def test_registered_channel_with_no_events_does_not_widen_to_org():
    """등록 경계에서 0건이 났다고 조직 전체로 넓히면 경계가 의미를 잃는다."""
    conn = FakeConn(registered={"?column?": 1}, channel_rows=[], org_rows=[_row()])
    rows, scope = fetch(
        conn, workspace="pilot", channel_id="C1", slack_user="U1",
        window=parse_window("오늘", now=NOW),
    )
    assert (rows, scope) == ([], SCOPE_CHANNEL)


def test_unregistered_channel_falls_back_to_identity():
    conn = FakeConn(registered=None, org_rows=[_row()])
    rows, scope = fetch(
        conn, workspace="pilot", channel_id="C9", slack_user="U1",
        window=parse_window("오늘", now=NOW),
    )
    assert scope == SCOPE_ORG
    assert len(rows) == 1
    sql, _ = conn.executed[-1]
    assert "JOIN user_identity" in sql
    assert "f.org_code = e.org_code" in sql


def test_no_identity_shows_nothing():
    """사번을 추측하면 남의 팀 일정이 보인다(설계 §2)."""
    conn = FakeConn(registered=None, org_rows=[], identity=None)
    rows, scope = fetch(
        conn, workspace="pilot", channel_id="C9", slack_user="U1",
        window=parse_window("오늘", now=NOW),
    )
    assert (rows, scope) == ([], SCOPE_NONE)


def test_identity_present_but_no_events_is_distinguished():
    conn = FakeConn(registered=None, org_rows=[], identity={"?column?": 1})
    rows, scope = fetch(
        conn, workspace="pilot", channel_id="C9", slack_user="U1",
        window=parse_window("오늘", now=NOW),
    )
    assert (rows, scope) == ([], SCOPE_ORG)


def test_deleted_and_disabled_rows_are_excluded_by_sql():
    conn = FakeConn(registered={"?column?": 1}, channel_rows=[])
    fetch(conn, workspace="pilot", channel_id="C1", slack_user="U1",
          window=parse_window("오늘", now=NOW))
    sql, _ = conn.executed[-1]
    assert "o.source_deleted_at IS NULL" in sql
    assert "c.enabled" in sql
    assert "f.enabled" in sql


def test_range_uses_overlap_not_containment():
    """진행 중인 일정도 보여야 한다 - 시작만 비교하면 오늘 시작한 장기 일정이 빠진다."""
    conn = FakeConn(registered={"?column?": 1}, channel_rows=[])
    fetch(conn, workspace="pilot", channel_id="C1", slack_user="U1",
          window=parse_window("오늘", now=NOW))
    sql, _ = conn.executed[-1]
    assert "o.starts_at < %(end)s" in sql
    assert "o.ends_at   > %(start)s" in sql


def test_last_sync_reads_applied_live_runs_only():
    conn = FakeConn(sync={"applied_at": NOW})
    assert last_sync(conn) == NOW
    sql, _ = conn.executed[-1]
    assert "mode = 'live'" in sql
    assert "status = 'applied'" in sql


def test_last_sync_handles_empty_table():
    conn = FakeConn(sync=None)
    assert last_sync(conn) is None


# --- Slack 연결부 ------------------------------------------------------------
def test_blocks_offer_share_only_when_there_is_something_to_share():
    from tybot.slack.pilot import schedule_blocks

    body = "*오늘 일정* — 2건 (이 채널에 연결된 일정 폴더)"
    blocks = schedule_blocks(body, share_payload="오늘")
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "actions"]
    assert blocks[1]["elements"][0]["action_id"] == "tybot_schedule_share"
    assert blocks[1]["elements"][0]["value"] == "오늘"


def test_blocks_hide_share_when_nothing_is_visible():
    """볼 수 없는 결과를 채널에 공유하는 버튼은 의미가 없다."""
    from tybot.schedule import UNAVAILABLE
    from tybot.slack.pilot import schedule_blocks

    assert [b["type"] for b in schedule_blocks(UNAVAILABLE)] == ["section"]
    denied = format_reply([], window=parse_window("오늘", now=NOW), scope=SCOPE_NONE,
                          synced_at=NOW, now=NOW)
    assert [b["type"] for b in schedule_blocks(denied)] == ["section"]


def _bot():
    from unittest.mock import Mock

    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "pilot"
    bot.bot_name = "tybot"
    bot.app = Mock()
    return bot


def test_command_says_so_when_db_is_not_configured(monkeypatch):
    """DB 가 없어도 명령이 침묵하지 않는다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from tybot.schedule import UNAVAILABLE

    assert _bot()._schedule_text("오늘", channel_id="C1", user_id="U1") == UNAVAILABLE


def test_command_rejects_unknown_period_before_touching_the_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/nodb")
    out = _bot()._schedule_text("작년", channel_id="C1", user_id="U1")
    assert "알 수 없는 기간" in out


def test_command_survives_db_failure(monkeypatch):
    """내부망·DB 장애가 Slack 응답 실패로 번지지 않아야 한다."""
    import tybot.slack.pilot as pilot_mod

    class Boom:
        def cursor(self):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(
        pilot_mod, "db_connect", lambda: __import__("contextlib").nullcontext(Boom())
    )
    out = _bot()._schedule_text("오늘", channel_id="C1", user_id="U1")
    assert "조회하지 못했습니다" in out


def test_log_does_not_carry_subject_or_place(monkeypatch, caplog):
    """subject·place 를 로그에 남기지 않는다(설계 §2)."""
    import contextlib as _ctx

    import tybot.slack.pilot as pilot_mod

    conn = FakeConn(registered={"?column?": 1},
                    channel_rows=[_row(subject="비밀 회의", place="비밀 장소")],
                    sync={"applied_at": datetime.now(UTC)})
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: _ctx.nullcontext(conn))

    with caplog.at_level("INFO"):
        out = _bot()._schedule_text("오늘", channel_id="C1", user_id="U1")

    assert "비밀 회의" in out          # 사용자에게는 보인다
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "비밀 회의" not in logged   # 로그에는 없다
    assert "비밀 장소" not in logged
    assert "rows=1" in logged
    assert "folders=[654]" in logged
