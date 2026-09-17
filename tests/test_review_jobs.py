"""콘솔의 요약 검토 DM 즉시 실행 작업."""
import json
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

    job = review_jobs.start([("tyit", "C1")], actor="admin@example.com")

    assert job["status"] == "queued"
    assert calls[0][0][1:3] == ["-m", "tybot.console.review_jobs"]
    saved = (tmp_path / "review-jobs" / f"{job['id']}.json").read_text(encoding="utf-8")
    assert "token" not in saved


def test_command_runs_only_the_fixed_targets(tmp_path):
    command = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"},
                     {"workspace": "mgmt", "channelId": "C2"}]},
        tmp_path / "out.result",
    )

    assert command[1:5] == ["-u", "-m", "tybot.daily_review", "--force-now"]
    assert command[5:9] == ["--target", "tyit:C1", "--target", "mgmt:C2"]
    assert command[-2] == "--result-json"


def test_command_reads_an_old_single_channel_job(tmp_path):
    """옛 작업 파일이 남아 있어도 화면이 죽지 않아야 한다."""
    command = review_jobs._command({"workspace": "tyit", "channelId": "C1"},
                                   tmp_path / "out.result")

    assert "--target" in command and "tyit:C1" in command


def test_resend_is_off_unless_the_operator_asked(tmp_path):
    """타이머가 재발송을 켜면 같은 DM 이 하루 종일 간다."""
    plain = review_jobs._command({"targets": [{"workspace": "tyit", "channelId": "C1"}]},
                                 tmp_path / "out.result")
    again = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"}], "resend": True},
        tmp_path / "out.result",
    )

    assert "--resend" not in plain
    assert "--resend" in again


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
        review_jobs.start([("tyit", "--restart")], actor="admin@example.com")


def test_a_second_active_job_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=321),
    )
    monkeypatch.setattr(review_jobs, "_pid_alive", lambda _pid: True)
    review_jobs.start([("tyit", "C1")], actor="admin@example.com")

    with pytest.raises(review_jobs.ReviewJobError, match="이미 실행 중"):
        review_jobs.start([("tyit", "C2")], actor="admin@example.com")


def _job(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=321)
    )
    return review_jobs.start([("tyit", "C1")], actor="admin@example.com")


def _run(tmp_path, monkeypatch, *, returncode=0, result=None):
    job = _job(tmp_path, monkeypatch)

    def _fake_run(command, **kwargs):
        if result is not None:
            path = command[command.index("--result-json") + 1]
            pathlib_write(path, result)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(review_jobs.subprocess, "run", _fake_run)
    review_jobs.run_worker(job["id"])
    return review_jobs._read(review_jobs._job_path(job["id"]))


def pathlib_write(path, payload):
    import pathlib as _p

    _p.Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_a_run_that_sent_nothing_is_not_reported_as_a_plain_success(
    tmp_path, monkeypatch
):
    """0 으로 끝났다고 DM 이 간 것은 아니다 — 이게 「실행 완료」 오해의 원인이었다."""
    saved = _run(
        tmp_path, monkeypatch,
        result={
            "sent": 0, "skipped": 1, "failed": 0, "generated": 0, "canvasFallback": 0,
            "channels": [{"workspace": "tyit", "channelId": "C1", "channelName": "주간보고",
                          "code": "already-sent", "reason": "오늘 이미 보낸 검토자뿐입니다",
                          "sent": 0, "skipped": 1, "failed": 0}],
        },
    )

    assert saved["outcome"] == "nothing-sent"
    assert saved["result"]["channels"][0]["code"] == "already-sent"


def test_a_run_that_sent_is_reported_as_sent(tmp_path, monkeypatch):
    saved = _run(
        tmp_path, monkeypatch,
        result={"sent": 2, "skipped": 0, "failed": 0, "generated": 1,
                "canvasFallback": 0, "channels": []},
    )

    assert saved["status"] == "completed"
    assert saved["outcome"] == "sent"


def test_a_missing_result_file_fails_closed(tmp_path, monkeypatch):
    """결과를 못 읽었으면 「보냈다」 로 넘어가지 않는다."""
    saved = _run(tmp_path, monkeypatch, returncode=0, result=None)

    assert saved["status"] == "failed"
    assert saved["errorCode"] == "result-missing"


def test_the_result_file_is_not_mistaken_for_another_job(tmp_path, monkeypatch):
    """결과 파일이 작업 목록에 섞이면 최근 작업 화면이 통째로 깨진다."""
    _run(
        tmp_path, monkeypatch,
        result={"sent": 1, "skipped": 0, "failed": 0, "generated": 1,
                "canvasFallback": 0, "channels": []},
    )

    assert len(review_jobs.list_jobs()) == 1
    assert review_jobs.latest()["outcome"] == "sent"


# --- 소급 검토 (B-58) ---------------------------------------------------------
def test_backfill_command_carries_the_start_day_and_round_cap(tmp_path):
    command = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"}],
         "backfill": True, "since": "2026-06-01", "rounds": 5},
        tmp_path / "out.result",
    )

    assert "--backfill" in command
    assert command[command.index("--since") + 1] == "2026-06-01"
    assert command[command.index("--rounds") + 1] == "5"


def test_backfill_estimate_does_not_run_the_generator(tmp_path):
    command = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"}],
         "backfill": True, "estimate": True},
        tmp_path / "out.result",
    )

    assert "--estimate" in command


def test_backfill_settings_are_refused_without_a_backfill(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must not start with stray backfill settings"),
    )

    with pytest.raises(review_jobs.ReviewJobError, match="소급"):
        review_jobs.start([("tyit", "C1")], actor="a@b.c", since="2026-06-01")


def test_a_command_like_start_day_is_refused(tmp_path, monkeypatch):
    """시작일은 그대로 명령줄에 들어간다 — 형식을 먼저 막는다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must not start a process"),
    )

    with pytest.raises(review_jobs.ReviewJobError, match="형식"):
        review_jobs.start(
            [("tyit", "C1")], actor="a@b.c", backfill=True, since="--rounds=999",
        )


def test_an_unbounded_round_cap_is_refused(tmp_path, monkeypatch):
    """회차마다 LLM 을 한 번 부른다. 상한이 없으면 채널 하나가 비용 한도를 다 쓴다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess, "Popen", lambda *a, **k: pytest.fail("no process")
    )

    with pytest.raises(review_jobs.ReviewJobError, match="회차"):
        review_jobs.start(
            [("tyit", "C1")], actor="a@b.c", backfill=True,
            rounds=review_jobs.MAX_ROUNDS + 1,
        )


def test_an_estimate_round_is_not_reported_as_nothing_sent(tmp_path, monkeypatch):
    """분량만 센 회차를 「아무도 못 받았다」로 경고하면 매번 빨간 화면이 뜬다."""
    saved = _run(
        tmp_path, monkeypatch,
        result={"sent": 0, "skipped": 0, "failed": 0, "generated": 0,
                "canvasFallback": 0, "estimate": True, "channels": []},
    )

    assert saved["outcome"] == "estimate"


def test_a_backfill_can_also_resend_to_reviewers_who_already_got_today(tmp_path):
    """소급으로 만든 후보도 같은 DM 으로 나간다. 재발송이 「지금 발송」에만 걸리면
    체크는 켜져 있는데 「오늘 이미 보낸 검토자뿐입니다」가 그대로 뜬다."""
    command = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"}],
         "backfill": True, "resend": True},
        tmp_path / "out.result",
    )

    assert "--backfill" in command
    assert "--resend" in command


def test_resend_and_backfill_start_together(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=321)
    )

    job = review_jobs.start(
        [("tyit", "C1")], actor="a@b.c", resend=True, backfill=True, since="2026-08-17",
    )

    assert job["resend"] is True
    assert job["backfill"] is True


def test_redeliver_never_asks_the_generator_to_run(tmp_path):
    """요약은 됐는데 DM 만 실패한 회차를 LLM 없이 복구한다."""
    command = review_jobs._command(
        {"targets": [{"workspace": "tyit", "channelId": "C1"}], "deliverOnly": True},
        tmp_path / "out.result",
    )

    assert "--deliver-only" in command
    assert "--backfill" not in command


def test_redeliver_and_backfill_cannot_be_asked_for_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        review_jobs.subprocess, "Popen", lambda *a, **k: pytest.fail("no process")
    )

    with pytest.raises(review_jobs.ReviewJobError, match="소급"):
        review_jobs.start(
            [("tyit", "C1")], actor="a@b.c", deliver_only=True, backfill=True,
        )
