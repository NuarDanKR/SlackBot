"""The inventory CLI uses only the explicitly selected Archiver identity."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "preview_archiver_files.py"
    spec = importlib.util.spec_from_file_location("preview_archiver_files", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_env_file", lambda: None)
    monkeypatch.setenv("ARCHIVER_SHADOW_LAYOUT", "per-channel-v1")
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setattr(module, "shadow_archive_dir", lambda env: tmp_path / "shadow")
    monkeypatch.setattr(module, "default_repo", lambda: object())
    return module


def test_cli_selects_db_archiver_and_validates_identity(cli, monkeypatch, capsys):
    seen = {}
    cfg = SimpleNamespace(key="mgmt", bot_token="fake-archiver-credential")

    def load(values):
        assert values["ARCHIVER_CONFIG_SOURCE"] == "db"
        assert values["ARCHIVER_WORKSPACE"] == "mgmt"
        return [cfg]

    class Client:
        def __init__(self, *, token, timeout):
            assert token == cfg.bot_token
            assert timeout == 30

        def auth_test(self):
            return {"team_id": "TEST"}

    monkeypatch.setattr(cli, "load_archiver_workspaces", load)
    monkeypatch.setattr(cli, "WebClient", Client)
    monkeypatch.setattr(cli, "validate_slack_identity",
                        lambda config, identity: seen.update(validated=True))

    def preview(*args, **kwargs):
        assert seen["validated"]
        return {"list_complete": True, "read_only": True}

    monkeypatch.setattr(cli, "preview", preview)
    assert cli.main(["--workspace", "mgmt", "--channel", "C0123456789"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"]


def test_cli_does_not_expose_configuration_or_sdk_exception(cli, monkeypatch, capsys):
    def fail(values):
        raise RuntimeError("sensitive credential or SDK response")

    monkeypatch.setattr(cli, "load_archiver_workspaces", fail)
    assert cli.main(["--workspace", "mgmt", "--channel", "C0123456789"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "RuntimeError", "read_only": True}


def test_cli_has_no_apply_switch(cli):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--workspace", "mgmt", "--channel", "C0123456789", "--apply"])
    assert exc.value.code == 2
