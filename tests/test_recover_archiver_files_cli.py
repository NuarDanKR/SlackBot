"""Apply cannot run alongside the live collector, and indexing uses no Slack token."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/recover_archiver_files.py"
    spec = importlib.util.spec_from_file_location("recover_archiver_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_env_file", lambda: None)
    monkeypatch.setenv("ARCHIVER_SHADOW_LAYOUT", "per-channel-v1")
    monkeypatch.setenv("ARCHIVER_LIVE_WRITES_ENABLED", "1")
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "shadow_archive_dir", lambda env: tmp_path / "shadow")
    monkeypatch.setattr(module, "default_repo", lambda: object())
    monkeypatch.setattr(module, "OwnerLookup", lambda *args, **kwargs: object())
    cfg = SimpleNamespace(key="mgmt", bot_token="fixture")
    monkeypatch.setattr(module, "load_archiver_workspaces", lambda env: [cfg])
    monkeypatch.setattr(module, "WebClient", lambda **kwargs:
                        SimpleNamespace(auth_test=lambda: {}))
    monkeypatch.setattr(module, "validate_slack_identity", lambda *args: None)
    return module


ARGS = ["--workspace", "mgmt", "--channel", "C0123456789", "--file-id", "F123"]


def test_apply_requires_an_actor(cli):
    with pytest.raises(SystemExit) as exc:
        cli.main([*ARGS, "--apply"])
    assert exc.value.code == 2


def test_running_collector_prevents_recovery(cli, monkeypatch, capsys):
    from tybot.lock import AlreadyRunning

    def acquire():
        raise AlreadyRunning("fixture collector is running")

    lock = SimpleNamespace(acquire=acquire, release=lambda: None)

    def instance_lock(name):
        assert name == "archiving-bot-shadow-mgmt"
        return lock

    monkeypatch.setattr(cli, "instance_lock", instance_lock)
    monkeypatch.setattr(cli, "recover", lambda *args, **kwargs: pytest.fail("no writes"))
    assert cli.main([*ARGS, "--apply", "--by", "operator"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "AlreadyRunning"


def test_collector_lock_is_held_for_entire_apply_and_released(cli, monkeypatch):
    events = []
    monkeypatch.setattr(cli, "instance_lock", lambda name:
                        SimpleNamespace(acquire=lambda: events.append("lock"),
                                        release=lambda: events.append("unlock")))

    def recover(*args, **kwargs):
        assert events == ["lock"]
        assert kwargs["apply"] is True
        assert "indexer" not in kwargs  # Archiver role must not write the index.
        return {"results": [{"status": "published-index-pending"}]}

    monkeypatch.setattr(cli, "recover", recover)
    assert cli.main([*ARGS, "--apply", "--by", "operator"]) == 0
    assert events == ["lock", "unlock"]


def test_index_only_never_loads_archiver_credentials_or_slack(cli, monkeypatch):
    monkeypatch.setattr(cli, "load_archiver_workspaces", lambda *args: pytest.fail("no tokens"))
    monkeypatch.setattr(cli, "WebClient", lambda **kwargs: pytest.fail("no Slack"))
    monkeypatch.setattr(cli, "index_recovered", lambda *args, **kwargs:
                        {"results": [{"status": "recovered"}]})
    assert cli.main([*ARGS, "--index-only"]) == 0


def test_plan_has_no_lock_or_write_mode(cli, monkeypatch):
    monkeypatch.setattr(cli, "instance_lock", lambda name: pytest.fail("read only"))

    def recover(*args, **kwargs):
        assert kwargs["apply"] is False
        return {"results": [{"status": "missing"}]}

    monkeypatch.setattr(cli, "recover", recover)
    assert cli.main(ARGS) == 0


def test_plan_accepts_legacy_workspace_without_modifying_it(cli, monkeypatch, tmp_path):
    legacy = tmp_path / "workspaces/mgmt/keep.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("preserve original", encoding="utf-8")
    monkeypatch.delenv("ARCHIVER_LIVE_WRITES_ENABLED")
    monkeypatch.setattr(cli, "recover", lambda *args, **kwargs:
                        {"results": [{"status": "existing-needs-review"}]})
    assert cli.main(ARGS) == 2
    assert legacy.read_text(encoding="utf-8") == "preserve original"


def test_apply_still_requires_env_live_gate(cli, monkeypatch, capsys):
    monkeypatch.setenv("ARCHIVER_LIVE_WRITES_ENABLED", "0")
    monkeypatch.setattr(cli, "recover", lambda *args, **kwargs: pytest.fail("no writes"))
    assert cli.main([*ARGS, "--apply", "--by", "operator"]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "live-write-env"


def test_errors_report_safe_stage_not_exception_body(cli, monkeypatch, capsys):
    def fail(values):
        raise RuntimeError("sensitive configuration details")

    monkeypatch.setattr(cli, "load_archiver_workspaces", fail)
    assert cli.main(ARGS) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "archiver-config"
    assert "sensitive" not in str(report)
