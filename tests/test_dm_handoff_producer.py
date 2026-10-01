from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from tybot.archive.dm_file_handoff import DmFileVault
from tybot.archive.dm_inbox import DmInbox
from tybot.slack.pilot import WorkspaceBot


def _bot():
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.cfg = SimpleNamespace(bot_token="xoxb-test")
    bot._bot_user_id = lambda: "U87654321"
    bot._user_name = lambda client, user: "Alice"
    return bot


class _Client:
    def __init__(self, **changes):
        self.conversation = {
            "id": "D12345678", "is_im": True, "is_mpim": False,
            "user": "U12345678", "num_members": 2,
        }
        self.conversation.update(changes)

    def conversations_info(self, **kwargs):
        assert kwargs == {"channel": "D12345678", "include_num_members": True}
        return {"ok": True, "channel": self.conversation}


def _event(**changes):
    event = {
        "channel_type": "im",
        "channel": "D12345678",
        "user": "U12345678",
        "ts": "1790800000.000001",
        "text": "private message",
    }
    event.update(changes)
    return event


def test_mirror_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ARCHIVER_DM_MIRROR_ENABLED", raising=False)
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    _bot()._mirror_dm_handoff(None, _event())
    assert not list(tmp_path.rglob("*.bin"))


def test_pii_screened_dm_is_not_queued(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr("tybot.archive.writer.screen", lambda text: "pii" if text else None)
    _bot()._mirror_dm_handoff(None, _event())
    assert not list(tmp_path.rglob("*.bin"))


def test_master_dm_mirror_preserves_event_and_still_answers(monkeypatch, tmp_path):
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr("tybot.console.workspace_store._fernet", lambda: cipher)
    monkeypatch.setattr(WorkspaceBot, "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(WorkspaceBot, "_handle_correction", lambda self, e, s: False)
    calls = []
    monkeypatch.setattr(
        WorkspaceBot, "_ingest_dm", lambda self, c, e: calls.append("legacy") or []
    )
    monkeypatch.setattr(
        WorkspaceBot, "_handle", lambda self, e, c, s, *, in_channel: calls.append("answer")
    )

    assert _bot().route_message(_event(), _Client(), None) == "answered"
    assert calls == ["legacy", "answer"]
    inbox = DmInbox(tmp_path, cipher)
    key, = inbox.pending_keys("tyit")
    assert inbox.read("tyit", key).event["text"] == "private message"
    assert b"private message" not in (tmp_path / "tyit" / "pending" / f"{key}.bin").read_bytes()


@pytest.mark.parametrize("changes", [
    {"id": "D99999999"}, {"is_im": False}, {"is_mpim": True},
    {"user": "U99999999"}, {"num_members": 3}, {"is_member": False},
])
def test_unverified_dm_is_not_queued(monkeypatch, tmp_path, changes):
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    _bot()._mirror_dm_handoff(_Client(**changes), _event())
    assert not list(tmp_path.rglob("*.bin"))


def test_dm_file_bytes_are_sealed_before_event_is_queued(monkeypatch, tmp_path):
    cipher = Fernet(Fernet.generate_key())
    data = b"private attachment bytes"
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr("tybot.console.workspace_store._fernet", lambda: cipher)
    monkeypatch.setattr("tybot.archive.files.download_bytes", lambda f, token, limit: data)
    file_event = {"id": "F12345678", "name": "note.txt", "size": len(data),
                  "url_private_download": "https://files.slack.com/private"}

    _bot()._mirror_dm_handoff(_Client(), _event(files=[file_event]))

    inbox = DmInbox(tmp_path, cipher)
    key, = inbox.pending_keys("tyit")
    handoff = inbox.read("tyit", key)
    file_key, = handoff.event["_dm_file_keys"]
    meta, original = DmFileVault(tmp_path, cipher).open("tyit", file_key)
    assert meta.file_id == "F12345678"
    assert meta.channel_id == handoff.channel_id
    assert meta.message_ts == handoff.message_ts
    assert original == data
    assert data not in (tmp_path / "tyit" / "files" / f"{file_key}.bin").read_bytes()


def test_dm_file_download_failure_does_not_queue_incomplete_event(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    def fail_download(f, token, limit):
        raise ValueError("private attachment bytes")
    monkeypatch.setattr("tybot.archive.files.download_bytes", fail_download)
    _bot()._mirror_dm_handoff(
        _Client(), _event(files=[{"id": "F12345678", "name": "note.txt"}]),
    )
    assert not list(tmp_path.rglob("pending/*.bin"))


def test_optional_file_mirror_runs_after_legacy_answer(monkeypatch, tmp_path):
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr("tybot.console.workspace_store._fernet", lambda: cipher)
    monkeypatch.setattr(WorkspaceBot, "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(WorkspaceBot, "_handle_correction", lambda self, e, s: False)
    calls = []
    monkeypatch.setattr(WorkspaceBot, "_ingest_dm", lambda self, c, e: calls.append("legacy") or [])
    monkeypatch.setattr(
        WorkspaceBot, "_handle", lambda self, e, c, s, *, in_channel: calls.append("answer"),
    )
    def download(f, token, limit):
        calls.append("mirror-download")
        return b"private bytes"
    monkeypatch.setattr("tybot.archive.files.download_bytes", download)

    event = _event(files=[{"id": "F12345678", "name": "note.txt"}])
    assert _bot().route_message(event, _Client(), None) == "answered"
    assert calls == ["legacy", "answer", "mirror-download"]


def test_mirror_failure_cannot_block_legacy_collection_or_answer(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(WorkspaceBot, "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(WorkspaceBot, "_handle_correction", lambda self, e, s: False)
    calls = []
    monkeypatch.setattr(
        WorkspaceBot, "_ingest_dm", lambda self, c, e: calls.append("legacy") or []
    )
    monkeypatch.setattr(
        WorkspaceBot, "_handle", lambda self, e, c, s, *, in_channel: calls.append("answer")
    )

    assert _bot().route_message(_event(), None, None) == "answered"
    assert calls == ["legacy", "answer"]
    assert "private message" not in caplog.text


def test_non_dm_and_bot_events_never_reach_mirror(monkeypatch):
    monkeypatch.setattr(WorkspaceBot, "_is_human_request", lambda self, e, c: not e.get("bot_id"))
    calls = []
    monkeypatch.setattr(WorkspaceBot, "_mirror_dm_handoff", lambda self, c, e: calls.append(e))
    bot = _bot()
    assert bot.route_message(_event(bot_id="B12345678"), None, None) == "ignored"
    assert bot.route_message(_event(channel_type="mpim"), None, None) == "skipped"
    assert calls == []
