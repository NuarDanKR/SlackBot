"""콘솔의 요약 검토 DM 즉시 실행 작업."""
from types import SimpleNamespace

import pytest

from tybot.console import review_jobs


def test_start_builds_a_fixed_detached_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(pid=321),
    )

    job = review_jobs.start("tyit", "C1", actor="admin@example.com")

    assert job["status"] == "queued"
    assert calls[0][0][1:3] == ["-m", "tybot.console.review_jobs"]
    saved = (tmp_path / "review-jobs" / f"{job['id']}.json").read_text(encoding="utf-8")
    assert "token" not in saved


def test_command_runs_only_the_fixed_target():
    command = review_jobs._command({"workspace": "tyit", "channelId": "C1"})

    assert command[1:5] == ["-u", "-m", "tybot.daily_review", "--force-now"]
    assert command[-4:] == ["--workspace", "tyit", "--channel", "C1"]


def test_command_like_coordinates_are_refused_before_starting_a_process(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("invalid coordinates must not start a process"),
    )

    with pytest.raises(review_jobs.ReviewJobError, match="형식"):
        review_jobs.start("tyit", "--restart", actor="admin@example.com")


def test_a_second_active_job_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=321),
    )
    monkeypatch.setattr(review_jobs, "_pid_alive", lambda _pid: True)
    review_jobs.start("tyit", "C1", actor="admin@example.com")

    with pytest.raises(review_jobs.ReviewJobError, match="이미 실행 중"):
        review_jobs.start("tyit", "C2", actor="admin@example.com")
