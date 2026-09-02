from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest

from tybot.console import timer_manager


def _helper_output() -> str:
    return "\n".join(
        [
            "tybot-collect.timer\tdisabled\tinactive\tdead\t-\t-\tunknown\tdefault",
            "tybot-schedule-dm.timer\tdisabled\tinactive\tdead\t-\t-\tunknown\tdefault",
            "tybot-schedule-sync.timer\tdisabled\tinactive\tdead\t-\t-\tunknown\tdefault",
            "tybot-tidy.timer\tenabled\tactive\twaiting\tWed 10:30 KST\tWed 10:15 KST\tsuccess\tdefault",
            "tybot-update.timer\tdisabled\tinactive\tdead\t-\t-\tunknown\tbusiness-30m",
        ]
    )


def test_timer_snapshot_reports_real_systemd_state(monkeypatch):
    monkeypatch.setattr(
        timer_manager.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout=_helper_output(), stderr=""),
    )

    rows = timer_manager.snapshot()

    assert len(rows) == 5
    tidy = next(row for row in rows if row["unit"] == "tybot-tidy.timer")
    assert tidy["enabled"] is True
    assert tidy["active"] is True
    assert tidy["nextRun"] == "Wed 10:30 KST"
    assert tidy["lastResult"] == "success"
    update = next(row for row in rows if row["unit"] == "tybot-update.timer")
    assert update["scheduleLabel"] == "평일 업무시간 30분마다"


@pytest.mark.parametrize(
    ("unit", "action", "preset"),
    [
        ("sshd.timer", "enable", None),
        ("tybot-tidy.timer", "restart", None),
        ("tybot-schedule-dm.timer", "schedule", "30m"),
        ("tybot-tidy.timer", "schedule", "2m"),
    ],
)
def test_timer_actions_reject_unapproved_input(unit, action, preset):
    with pytest.raises(timer_manager.TimerManagerError):
        timer_manager.apply(unit, action, preset)


def test_timer_action_uses_fixed_helper_arguments(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        output = _helper_output() if args[-1] == "list" else ""
        return CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(timer_manager.subprocess, "run", fake_run)
    rows = timer_manager.apply("tybot-tidy.timer", "schedule", "30m")

    assert calls[0][-3:] == ["schedule", "tybot-tidy.timer", "30m"]
    assert calls[1][-1] == "list"
    assert len(rows) == 5


def test_timer_action_audit_contains_no_command_or_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(timer_manager.reader, "qa_log_dir", lambda: tmp_path)
    timer_manager.audit(
        "3420-M",
        "dan@taeyoung.com",
        "tybot-tidy.timer",
        "enable",
        None,
        "applied",
    )

    row = json.loads((tmp_path / "timer-actions.jsonl").read_text(encoding="utf-8"))
    assert row["unit"] == "tybot-tidy.timer"
    assert row["action"] == "enable"
    assert set(row) == {"at", "actor", "email", "unit", "action", "preset", "result"}
