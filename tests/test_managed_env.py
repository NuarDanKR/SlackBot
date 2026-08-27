from __future__ import annotations

from tybot.envfile import load_env_file
from tybot.managed_env import consume_restart_request, request_restart


def test_managed_env_overrides_base_after_base_path_is_loaded(tmp_path, monkeypatch):
    base = tmp_path / "base.env"
    managed = tmp_path / "state" / "config" / "console-managed.env"
    base.write_text(
        f"STATE_DIR={(tmp_path / 'state').as_posix()}\nREALTIME_INGEST=1\nWORKSPACES=pilot\n",
        encoding="utf-8",
    )
    managed.parent.mkdir(parents=True)
    managed.write_text('REALTIME_INGEST="0"\nREPLY_IN_THREAD="0"\n', encoding="utf-8")
    monkeypatch.setenv("TYBOT_ENV_FILE", str(base))
    monkeypatch.delenv("ENV_SETTINGS_PATH", raising=False)
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.delenv("REALTIME_INGEST", raising=False)
    monkeypatch.delenv("REPLY_IN_THREAD", raising=False)

    source = load_env_file()

    import os

    assert os.environ["STATE_DIR"] == (tmp_path / "state").as_posix()
    assert str(base) in source and str(managed) in source
    assert os.environ["REALTIME_INGEST"] == "0"
    assert os.environ["REPLY_IN_THREAD"] == "0"


def test_restart_request_is_consumed_once(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    request_restart("admin", ["ROOT_WORKSPACES"])

    request = consume_restart_request()

    assert request is not None
    assert request["actor"] == "admin"
    assert request["changed"] == ["ROOT_WORKSPACES"]
    assert consume_restart_request() is None
