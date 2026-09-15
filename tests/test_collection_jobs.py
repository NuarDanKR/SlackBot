"""콘솔 수집 작업은 고정 명령·단일 실행·비민감 상태만 허용한다."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tybot.console import collection_jobs


def test_start_builds_a_fixed_detached_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        collection_jobs.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(pid=321),
    )

    job = collection_jobs.start("files", [("tyit", "C1")], actor="admin@example.com")

    assert job["status"] == "queued"
    assert job["targetCount"] == 1
    assert calls[0][0][1:3] == ["-m", "tybot.console.collection_jobs"]
    assert calls[0][0][-2:] == ["worker", job["id"]]
    assert "token" not in (tmp_path / "collection-jobs" / f"{job['id']}.json").read_text()


def test_a_second_active_job_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        collection_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=321),
    )
    monkeypatch.setattr(collection_jobs, "_pid_alive", lambda pid: True)
    collection_jobs.start("files", [("tyit", "C1")], actor="a@example.com")

    with pytest.raises(collection_jobs.CollectionJobError, match="이미 실행 중"):
        collection_jobs.start("history", [("tyit", "C2")], actor="a@example.com")


def test_command_like_coordinates_are_refused_before_starting_a_process(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        collection_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("invalid coordinates must not start a process"),
    )

    with pytest.raises(collection_jobs.CollectionJobError, match="형식"):
        collection_jobs.start("history", [("tyit", "--restart")], actor="a@example.com")


def test_worker_command_accepts_only_fixed_modes_and_coordinates():
    job = {
        "mode": "history",
        "targets": [
            {"workspace": "tyit", "channelId": "C1"},
            {"workspace": "tyit", "channelId": "C2"},
        ],
    }

    command = collection_jobs._command(job)

    assert command[1] == "-u"
    assert command[2].endswith("scripts\\backfill_channel_history.py") or command[2].endswith(
        "scripts/backfill_channel_history.py"
    )
    assert command[-1] == "--apply"
    assert command.count("--workspace") == 1
    assert command.count("--channel") == 2


def test_latest_returns_only_the_tail_of_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    job = {
        "id": "a" * 32,
        "mode": "files",
        "status": "completed",
        "createdAt": "2026-09-15T01:00:00+00:00",
        "pid": 1,
    }
    collection_jobs._write(job)
    log_path = tmp_path / "collection-jobs" / f"{job['id']}.log"
    log_path.write_text("\n".join(f"line {n}" for n in range(40)), encoding="utf-8")

    got = collection_jobs.latest()

    assert got is not None
    assert got["logTail"][0] == "line 10"
    assert got["logTail"][-1] == "line 39"
