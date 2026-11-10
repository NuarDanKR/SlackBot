"""요약 검토 콘솔의 빈 상태 진단."""
from datetime import UTC, datetime, time, timedelta, timezone

from tybot.console import summary_review_store as store

KST = timezone(timedelta(hours=9))


def test_auto_bot_summaries_are_settled_in_round_counts(monkeypatch):
    class Cursor:
        def __init__(self):
            self.sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, _params):
            self.sql = sql

        def fetchall(self):
            return []

    cursor = Cursor()

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def cursor(self):
            return cursor

    monkeypatch.setattr(store, "_connect", Conn)

    assert store.rounds() == []
    assert "'approved','rejected','auto_bot_summary'" in "".join(
        cursor.sql.split()
    )


# --- 전역 회차 (B-88) ----------------------------------------------------------
#
# 전에는 채널마다 `send_at`·`schedule_kind`·`weekday` 로 대기·발송을 셌다. 그 값은
# DB 에 호환용으로만 남았고 실행에 쓰이지 않는다. 옛 계산을 두면 콘솔이 「08:00
# 예정 · 8채널 시각 지남 · 회차 0건」 을 보이고, 관리자는 스케줄러가 죽었다고 읽는다.
#
# 2026-11-10(화) · 11-11(수) · 11-13(금).

TUE = datetime(2026, 11, 10, tzinfo=KST)
WED = datetime(2026, 11, 11, tzinfo=KST)


def test_schedule_status_reports_no_configured_channels():
    got = store._schedule_status([], now=WED.replace(hour=8, minute=30))

    assert got == {
        "channels": 0,
        "due": 0,
        "waiting": 0,
        # 채널이 없어도 다음 회차는 있다 — 정책은 채널 수와 무관하다.
        "nextSendAt": "금 17:00",
        "timezone": "Asia/Seoul",
        "scheduleSource": "global",
    }


def test_before_the_round_every_channel_is_waiting():
    rows = [{"send_at": time(10, 0)}, {"send_at": time(9, 0)}]

    got = store._schedule_status(rows, now=TUE.replace(hour=16, minute=59))

    assert got["channels"] == 2
    assert got["due"] == 0
    assert got["waiting"] == 2
    assert got["nextSendAt"] == "화 17:00"


def test_at_the_round_every_channel_is_due_together():
    """전역 회차는 **한 번에** 모든 채널을 묶는다. 채널마다 다른 시각이 없다."""
    rows = [{"send_at": time(9, 0)}, {"send_at": time(10, 0)}]

    got = store._schedule_status(rows, now=TUE.replace(hour=17, minute=0))

    assert got["due"] == 2
    assert got["waiting"] == 0
    assert got["nextSendAt"] == "화 17:00 (진행 중)"


def test_schedule_status_converts_utc_to_kst():
    rows = [{"send_at": time(9, 0)}]

    # 08:00 UTC = 17:00 KST 화요일.
    got = store._schedule_status(rows, now=datetime(2026, 11, 10, 8, 0, tzinfo=UTC))

    assert got["due"] == 1
    assert got["waiting"] == 0


def test_legacy_per_channel_schedules_are_not_read():
    """뒤집은 시험이다. 예전에는 「매주 수요일 09:00」 채널을 수요일 09:30 에 발송
    대상으로 셌다.

    그 값은 실행되지 않는다. 그런데 콘솔이 그것을 「발송 시각 지남」 으로 세면,
    회차가 0건인 것을 보고 관리자는 스케줄러가 멈췄다고 판단한다.
    """
    rows = [
        {"send_at": time(9, 0), "schedule_kind": "weekly", "weekday": 2},
        {"send_at": time(9, 0), "schedule_kind": "daily", "weekday": None},
        {"send_at": None, "schedule_kind": "weekly", "weekday": 3},
    ]

    got = store._schedule_status(rows, now=WED.replace(hour=9, minute=30))

    assert got["channels"] == 3
    assert got["due"] == 0, "옛 채널별 시각으로 발송 대상을 셌다"
    assert got["waiting"] == 3
    assert got["nextSendAt"] == "금 17:00"
    assert got["scheduleSource"] == "global"


def test_a_round_day_after_the_round_still_reports_the_running_round():
    """17시를 막 지난 화요일에 「다음은 금요일」 이라고 하면 지금 도는 회차가
    화면에서 사라진다. 그날 안이면 늦어도 보낸다(`daily_review.due`)."""
    got = store._schedule_status([{"send_at": None}], now=TUE.replace(hour=23, minute=0))

    assert got["due"] == 1
    assert got["nextSendAt"] == "화 17:00 (진행 중)"


