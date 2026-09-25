import pytest
from cryptography.fernet import Fernet

from tybot import archiver_runtime_store as runtime


class Cursor:
    def __init__(self, config, channels, flags):
        self.config = config
        self.channels = channels
        self.flags = flags
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, _params=None):
        self.query = query

    def fetchone(self):
        return self.config

    def fetchall(self):
        if "archive_channel_mode" in self.query:
            return [{"channel_id": value} for value in self.channels]
        return [{"name": name, "enabled": value} for name, value in self.flags.items()]


class Connection:
    def __init__(self, config, channels, flags):
        self.values = config, channels, flags

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return Cursor(*self.values)


def _install(monkeypatch, *, separate=True, reader=True, master="U_MASTER"):
    cipher = Fernet(Fernet.generate_key())
    config = {
        "state": "enabled",
        "team_id": "T123",
        "bot_user_id": "U_ARCHIVER",
        "master_bot_user_id": master,
        "bot_ciphertext": cipher.encrypt(b"xoxb-archiver"),
        "app_ciphertext": cipher.encrypt(b"xapp-archiver"),
    }
    monkeypatch.setattr(
        runtime,
        "_connect",
        lambda: Connection(
            config,
            ["C111", "C222"],
            {
                "separate_attachments": separate,
                "attachment_reader_ready": reader,
            },
        ),
    )
    monkeypatch.setattr(runtime, "_fernet", lambda: cipher)


def test_runtime_config_returns_only_decrypted_archiver_pair(monkeypatch):
    _install(monkeypatch)

    result = runtime.load_runtime_config("tyit")

    assert result["bot_token"] == "xoxb-archiver"
    assert result["app_token"] == "xapp-archiver"
    assert result["channel_ids"] == ["C111", "C222"]
    assert result["separate_attachments"] is True
    assert "bot_ciphertext" not in result
    assert "app_ciphertext" not in result


def test_runtime_config_fails_closed_without_attachment_reader(monkeypatch):
    _install(monkeypatch, separate=True, reader=False)

    with pytest.raises(runtime.ArchiverRuntimeStoreError, match="reader"):
        runtime.load_runtime_config("tyit")


def test_runtime_config_requires_verified_master_identity(monkeypatch):
    _install(monkeypatch, master="")

    with pytest.raises(runtime.ArchiverRuntimeStoreError, match="Master"):
        runtime.load_runtime_config("tyit")
