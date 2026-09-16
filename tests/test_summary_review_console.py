"""요약 검토 콘솔의 빈 상태 진단."""
from datetime import UTC, datetime, time, timedelta, timezone

from tybot.console import summary_review_store as store

KST = timezone(timedelta(hours=9))


def test_schedule_status_reports_no_configured_channels():
    got = store._schedule_status([], now=datetime(2026, 9, 16, 8, 30, tzinfo=KST))

    assert got == {
        "channels": 0,
        "due": 0,
        "waiting": 0,
        "nextSendAt": "",
        "timezone": "Asia/Seoul",
    }


def test_schedule_status_reports_the_next_send_time_before_it_is_due():
    rows = [{"send_at": time(10, 0)}, {"send_at": time(9, 0)}]

    got = store._schedule_status(
        rows, now=datetime(2026, 9, 16, 8, 30, tzinfo=KST)
    )

    assert got["channels"] == 2
    assert got["due"] == 0
    assert got["waiting"] == 2
    assert got["nextSendAt"] == "09:00"


def test_schedule_status_marks_the_channel_due_at_the_configured_minute():
    rows = [{"send_at": time(9, 0)}, {"send_at": time(10, 0)}]

    got = store._schedule_status(
        rows, now=datetime(2026, 9, 16, 9, 0, tzinfo=KST)
    )

    assert got["due"] == 1
    assert got["waiting"] == 1
    assert got["nextSendAt"] == "10:00"


def test_schedule_status_converts_utc_to_kst():
    rows = [{"send_at": time(9, 0)}]

    got = store._schedule_status(
        rows, now=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )

    assert got["due"] == 1
    assert got["waiting"] == 0
