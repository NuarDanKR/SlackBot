import os

from tybot.console import env_settings
from tybot.specialist_tools import ToolBudget


def test_console_setting_changes_active_tool_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENV_SETTINGS_PATH", str(tmp_path / "managed.env"))
    for key in env_settings.MANAGED_STATIC_KEYS:
        monkeypatch.setenv(key, os.getenv(key, "90" if key == "TYBOT_SPECIALIST_TOOL_SECONDS" else ""))
    monkeypatch.setattr(env_settings, "request_restart", lambda actor, keys: None)
    payload = {
        "realtimeIngest": True,
        "autojoinChannels": True,
        "replyInThread": True,
        "includeRetiredChannels": False,
        "toolBudgetSeconds": 110,
    }

    snapshot, changed = env_settings.save(payload, actor="operator")

    assert snapshot["toolBudgetSeconds"] == 110
    assert "TYBOT_SPECIALIST_TOOL_SECONDS" in changed
    assert ToolBudget().max_seconds == 110


def test_console_setting_bounds_and_old_client_preserves_value(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_SETTINGS_PATH", str(tmp_path / "managed.env"))
    monkeypatch.setenv("TYBOT_SPECIALIST_TOOL_SECONDS", "105")
    payload = {"realtimeIngest": True, "autojoinChannels": True, "replyInThread": True}
    assert env_settings._validate(payload)["TYBOT_SPECIALIST_TOOL_SECONDS"] == "105"
    for invalid in (0, 121, "bad"):
        try:
            env_settings._validate({**payload, "toolBudgetSeconds": invalid})
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid tool budget: {invalid}")
