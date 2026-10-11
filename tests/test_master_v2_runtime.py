from types import SimpleNamespace

import pytest

from tybot.slack import pilot


def test_external_runtime_is_not_touched_when_rollout_is_empty(monkeypatch):
    monkeypatch.delenv("TYBOT_MASTER_V2_WORKSPACES", raising=False)
    monkeypatch.delenv("TYBOT_SPECIALIST_V3_HMAC_DIR", raising=False)

    assert pilot._start_external_v3([SimpleNamespace(key="tyit")]) is None


def test_enabled_rollout_requires_a_credential_directory(monkeypatch):
    monkeypatch.setenv("TYBOT_MASTER_V2_WORKSPACES", "tyit")
    monkeypatch.delenv("TYBOT_SPECIALIST_V3_HMAC_DIR", raising=False)

    with pytest.raises(RuntimeError, match="HMAC_DIR"):
        pilot._start_external_v3([SimpleNamespace(key="tyit")])


def test_tyit_rollout_builds_the_external_factory(monkeypatch, tmp_path):
    from tybot import specialist_v3_tool_server

    (tmp_path / "hermes-tyit.hmac").write_bytes(b"x" * 32)
    monkeypatch.setenv("TYBOT_MASTER_V2_WORKSPACES", "tyit")
    monkeypatch.setenv("TYBOT_SPECIALIST_V3_HMAC_DIR", str(tmp_path))
    monkeypatch.setattr(specialist_v3_tool_server.ToolServer, "start", lambda self: None)

    factory = pilot._start_external_v3([SimpleNamespace(key="tyit")])

    assert callable(factory)


def test_no_specialist_workspace_does_not_require_a_hermes_secret(monkeypatch, tmp_path):
    from tybot import specialist_v3_tool_server

    monkeypatch.setenv("TYBOT_MASTER_V2_WORKSPACES", "invest")
    monkeypatch.setenv("TYBOT_SPECIALIST_V3_HMAC_DIR", str(tmp_path))
    monkeypatch.setattr(specialist_v3_tool_server.ToolServer, "start", lambda self: None)

    factory = pilot._start_external_v3([SimpleNamespace(key="invest")])

    assert callable(factory)
