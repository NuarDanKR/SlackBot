"""Scheduled inventory never writes archives or hides failed scans as clean."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot.archive import file_audit_job as job
from tybot.lock import FileLock


def report(*, complete=True, missing=None):
    return {
        "read_only": True, "scope": job.SCOPE, "complete": complete,
        "workspaces": [{"workspace": "tec", "status": "ok" if complete else "partial",
                        "target_channels": 1,
                        "channels": [{"channel_id": "C0123456789",
                                      "status": "ok" if complete else "partial",
                                      "list_complete": complete, "live_metadata_records": 2,
                                      "files_seen": 5,
                                      "missing_after_record_check": missing or [],
                                      "shadow_only_ids": [], "existing_needs_review": []}]}],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    args = {"live_root": tmp_path / "live", "shadow_root": tmp_path / "shadow"}
    responses = [report()]

    def invoke(command, **kwargs):
        assert "--all-workspaces" in command
        assert "--apply" not in command
        assert kwargs["timeout"] == 3600
        assert kwargs["stdout"] == subprocess.DEVNULL
        response = responses[-1]
        if isinstance(response, Exception):
            raise response
        output = Path(command[command.index("--result-json") + 1])
        output.write_text(json.dumps(response), encoding="utf-8")
        return SimpleNamespace(returncode=0 if response.get("complete") else 2)

    monkeypatch.setattr(job.subprocess, "run", invoke)
    return directory, args, responses


def test_completed_missing_files_are_findings_not_a_failed_scan(setup):
    directory, args, responses = setup
    responses.append(report(missing=["F123"]))
    assert job.run(directory, **args) == 0
    state = job.read_status(directory)
    assert state["fresh"] is True
    assert state["last_run"]["status"] == "completed"
    assert state["last_run"]["counts"]["missing"] == 1
    assert state["last_success"] == state["last_run"]
    assert not args["live_root"].exists()
    assert not args["shadow_root"].exists()


@pytest.mark.parametrize("failure,code,error", [
    (report(complete=False), 2, "audit_incomplete"),
    (subprocess.TimeoutExpired("private command", 3600), 1, "audit_timeout"),
    (RuntimeError("private secret body"), 1, "audit_result_invalid"),
])
def test_failed_scan_preserves_last_complete_result(setup, failure, code, error):
    directory, args, responses = setup
    assert job.run(directory, **args) == 0
    previous = job.read_status(directory)["last_success"]
    responses.append(failure)
    assert job.run(directory, **args) == code
    state = job.read_status(directory)
    assert state["last_run"]["error_code"] == error
    assert state["last_success"] == previous
    assert state["last_run"]["id"] != previous["id"]
    assert "private" not in json.dumps(state)


def test_duplicate_does_not_replace_running_state(setup):
    directory, args, _ = setup
    directory.mkdir()
    original = {"schema": job.SCHEMA, "last_run": {"status": "running"}, "last_success": None}
    job._write(directory / "status.json", original)
    with FileLock(directory / ".run.lock", label="other audit"):
        assert job.run(directory, **args) == 3
    assert job._load(directory / "status.json") == original


@pytest.mark.parametrize("age,fresh", [(0, True), (35, True), (37, False), (-1, False)])
def test_freshness_is_not_an_eternal_green_badge(setup, age, fresh):
    directory, args, _ = setup
    job.run(directory, **args)
    stamp = datetime.fromisoformat(job.read_status(directory)["last_success"]["finished_at"])
    assert job.read_status(directory, now=stamp + timedelta(hours=age))["fresh"] is fresh


def test_unrun_status_is_unknown_not_success(tmp_path):
    state = job.read_status(tmp_path)
    assert state["last_run"] is None and state["last_success"] is None
    assert state["fresh"] is False


def test_failed_first_run_does_not_create_success(setup):
    directory, args, responses = setup
    responses.append(report(complete=False))
    job.run(directory, **args)
    assert job.read_status(directory)["last_success"] is None


def test_empty_registry_is_partial_not_success(setup):
    directory, args, responses = setup
    responses.append({"read_only": True, "scope": job.SCOPE,
                      "complete": False, "workspaces": []})
    assert job.run(directory, **args) == 2


def test_projection_discards_body_names_and_file_metadata(setup):
    directory, args, responses = setup
    source = report()
    source["secret"] = "DO_NOT_PERSIST"
    source["workspaces"][0]["error"] = "DO_NOT_PERSIST"
    source["workspaces"][0]["channels"][0]["body"] = "DO_NOT_PERSIST"
    responses.append(source)
    assert job.run(directory, **args) == 0
    assert "DO_NOT_PERSIST" not in (directory / "status.json").read_text()


@pytest.mark.parametrize("target", ["live", "shadow", "parent"])
def test_state_cannot_overlap_archives(setup, target):
    directory, args, _ = setup
    target = args.get(f"{target}_root", directory.parent)
    with pytest.raises(ValueError):
        job.run(target, **args)
    assert not (target / "status.json").exists()


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), 0, -1])
def test_invalid_timeout_rejected_before_writes(setup, budget):
    directory, args, _ = setup
    with pytest.raises(ValueError):
        job.run(directory, **args, timeout=budget)
    assert not directory.exists()


def test_crash_leaves_running_mark_and_previous_success(setup, monkeypatch):
    directory, args, _ = setup
    job.run(directory, **args)
    previous = job.read_status(directory)["last_success"]

    def die(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(job.subprocess, "run", die)
    with pytest.raises(KeyboardInterrupt):
        job.run(directory, **args)
    state = job.read_status(directory)
    assert state["last_run"]["status"] == "running"
    assert state["last_success"] == previous


def test_nonzero_child_exit_is_not_clean(setup, monkeypatch):
    directory, args, _ = setup
    monkeypatch.setattr(job.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert job.run(directory, **args) == 1
    assert job.read_status(directory)["last_run"]["error_code"] == "audit_process_failed"


def test_corrupt_previous_state_fails_closed(setup):
    directory, args, _ = setup
    directory.mkdir()
    target = directory / "status.json"
    target.write_text("broken")
    with pytest.raises(ValueError):
        job.run(directory, **args)
    assert target.read_text() == "broken"


def test_inconsistent_report_does_not_refresh_success(setup):
    directory, args, responses = setup
    source = report()
    source["workspaces"][0]["status"] = "failed"
    responses.append(source)
    assert job.run(directory, **args) == 1
    assert job.read_status(directory)["last_success"] is None


def test_atomic_replace_failure_preserves_existing_json(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    job._write(target, {"before": True})

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(job.os, "replace", fail)
    with pytest.raises(OSError):
        job._write(target, {"after": True})
    assert json.loads(target.read_text()) == {"before": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]


def test_missing_output_after_child_success_is_failed(setup, monkeypatch):
    directory, args, _ = setup
    monkeypatch.setattr(job.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    assert job.run(directory, **args) == 1
    assert job.read_status(directory)["last_success"] is None


def test_wrong_exit_code_does_not_refresh_success(setup, monkeypatch):
    directory, args, _ = setup

    def inconsistent(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(report(complete=False)))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(job.subprocess, "run", inconsistent)
    assert job.run(directory, **args) == 1
    assert job.read_status(directory)["last_success"] is None


def test_workspace_failure_remains_visible_without_exception_body(setup):
    directory, args, responses = setup
    responses.append({"read_only": True, "scope": job.SCOPE, "complete": False,
                      "workspaces": [{"workspace": "tec", "status": "failed",
                                      "channels": [], "error": "SECRET_RESPONSE"}]})
    assert job.run(directory, **args) == 2
    state = job.read_status(directory)
    assert state["last_run"]["workspaces"][0]["status"] == "failed"
    assert "SECRET_RESPONSE" not in json.dumps(state)


def test_membership_mismatch_is_not_silently_successful(setup):
    directory, args, responses = setup
    source = report(complete=False)
    source["workspaces"][0]["channels"] = []
    source["workspaces"][0]["db_joined_not_in_slack"] = ["C0123456789"]
    responses.append(source)
    assert job.run(directory, **args) == 2
    state = job.read_status(directory)
    assert state["last_run"]["workspaces"][0]["membership_mismatches"] == 1


def test_cleanup_failure_cannot_exit_successfully(setup, monkeypatch):
    directory, args, _ = setup
    original = job.tempfile.TemporaryDirectory

    @contextmanager
    def broken_cleanup(**kwargs):
        with original(**kwargs) as temporary:
            yield temporary
        raise OSError("private cleanup details")

    monkeypatch.setattr(job.tempfile, "TemporaryDirectory", broken_cleanup)
    assert job.run(directory, **args) == 1
    state = job.read_status(directory)
    assert state["last_run"]["status"] == "failed"
    assert state["last_success"] is None


def test_coverage_distinguishes_attempted_from_complete_and_unconfirmed(setup):
    directory, args, responses = setup
    source = report(complete=False)
    row = source["workspaces"][0]
    row["target_channels"] = 3
    row["channels"].append({"channel_id": "C0223456789", "status": "failed"})
    responses.append(source)
    assert job.run(directory, **args) == 2
    state = job.read_status(directory)["last_run"]
    assert state["coverage"] == {"target_channels": 3, "attempted_channels": 2,
                                 "complete_channels": 0, "unconfirmed_channels": 3,
                                 "unknown_workspaces": 0}
    failed = state["workspaces"][0]["channels"][1]
    assert failed["files_seen"] is None and failed["missing"] is None
    assert failed["list_complete"] is None
    partial = state["workspaces"][0]["channels"][0]
    assert partial["list_complete"] is False and partial["live_metadata_records"] == 2


def test_failed_workspace_does_not_invent_total_scope(setup):
    directory, args, responses = setup
    source = report(complete=False)
    source["workspaces"].append({"workspace": "mgmt", "status": "failed", "channels": []})
    responses.append(source)
    assert job.run(directory, **args) == 2
    coverage = job.read_status(directory)["last_run"]["coverage"]
    assert coverage["target_channels"] is None
    assert coverage["unconfirmed_channels"] is None
    assert coverage["unknown_workspaces"] == 1


def test_main_uses_dedicated_env_without_master_workspaces(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "load_env_file", lambda: None)
    for key in ("STATE_DIR", "ARCHIVE_DIR", "ARCHIVER_SHADOW_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))

    def run(directory, **kwargs):
        assert directory == tmp_path / "STATE_DIR/file-audit"
        assert kwargs["pause"] == 4
        return 2

    monkeypatch.setattr(job, "run", run)
    assert job.main(["--pause", "4"]) == 2


def test_service_is_readonly_and_timer_is_opt_in():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/tybot-file-audit.service").read_text()
    assert "TYBOT_ENV_FILE=/etc/tybot/archiver.env" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/tybot/file-audit\n" in unit
    assert "KillMode=control-group" in unit
    assert "TimeoutStartSec=3700" in unit
    assert "SuccessExitStatus=3" in unit
    timer = (root / "deploy/tybot-file-audit.timer").read_text()
    assert "OnCalendar=*-*-* 02:30:00" in timer
    installer = (root / "deploy/install.sh").read_text(encoding="utf-8")
    assert " tybot-file-audit)" in installer
    assert "enable --now tybot-file-audit" not in installer
