"""Audit CLI discovers Archiver registrations, not Master allowlists."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/audit_archiver_files.py"
    spec = importlib.util.spec_from_file_location("audit_archiver_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setenv("ARCHIVE_DIR", str(live))
    monkeypatch.setenv("ARCHIVER_SHADOW_LAYOUT", "per-channel-v1")
    monkeypatch.setattr(module, "load_env_file", lambda: None)
    monkeypatch.setattr(module, "shadow_archive_dir", lambda: tmp_path / "shadow")
    monkeypatch.setattr(module, "default_repo", lambda: object())
    return module


def test_all_workspaces_uses_archiver_registry(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "registered_workspaces", lambda: ["mgmt", "tec"])

    def audit(workspaces, **kwargs):
        assert workspaces == ["mgmt", "tec"]
        return {"read_only": True, "complete": True}

    monkeypatch.setattr(cli, "audit", audit)
    assert cli.main(["--all-workspaces"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"] is True


def test_explicit_workspace_does_not_list_other_registrations(cli, monkeypatch):
    monkeypatch.setattr(cli, "registered_workspaces", lambda: pytest.fail("no global query"))

    def audit(workspaces, **kwargs):
        assert workspaces == ["tec"]
        return {"complete": False}

    monkeypatch.setattr(cli, "audit", audit)
    assert cli.main(["--workspace", "tec"]) == 2


def test_output_cannot_be_written_inside_archive(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "audit", lambda *args, **kwargs: pytest.fail("no requests"))
    assert cli.main(["--workspace", "mgmt", "--result-json", str(tmp_path / "live/result.json")]) == 1
    assert not (tmp_path / "live/result.json").exists()


def test_result_file_never_overwrites_existing_operator_file(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "audit", lambda *args, **kwargs: {"complete": True})
    target = tmp_path / "keep.json"
    target.write_text("keep")
    assert cli.main(["--workspace", "mgmt", "--result-json", str(target)]) == 1
    assert target.read_text() == "keep"


def test_result_json_written_only_when_explicitly_requested(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "audit", lambda *args, **kwargs:
                        {"read_only": True, "complete": True})
    target = tmp_path / "result.json"
    assert cli.main(["--workspace", "mgmt", "--result-json", str(target)]) == 0
    assert json.loads(target.read_text())["read_only"] is True


@pytest.mark.parametrize("pace", ["nan", "inf", "-1"])
def test_invalid_pace_rejected(cli, pace):
    with pytest.raises(SystemExit):
        cli.main(["--workspace", "mgmt", "--pause", pace])
