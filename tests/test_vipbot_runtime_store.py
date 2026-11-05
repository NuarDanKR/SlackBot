import sys
import traceback
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from tybot import vipbot_runtime_store as runtime


class Cursor:
    def __init__(self, row):
        self.row = row
        self.query = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.cursor_value = Cursor(row)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def _install(monkeypatch, row=None):
    cipher = Fernet(Fernet.generate_key())
    if row is None:
        row = {
            "state": "enabled",
            "team_id": "T123",
            "bot_user_id": "U_VIPBOT",
            "bot_ciphertext": cipher.encrypt(b"xoxb-vipbot"),
            "app_ciphertext": cipher.encrypt(b"xapp-vipbot"),
        }
    connection = Connection(row)
    monkeypatch.setattr(runtime, "_connect", lambda: connection)
    monkeypatch.setattr(runtime, "_fernet", lambda: cipher)
    return connection


def _install_driver(monkeypatch, identity):
    connection = Connection(identity)

    class DriverError(Exception):
        pass

    driver = SimpleNamespace(
        Error=DriverError,
        rows=SimpleNamespace(dict_row=object()),
        connect=lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    monkeypatch.setattr(runtime, "resolve_vipbot_database_url", lambda: "postgresql://x")
    return connection


def test_connect_verifies_the_effective_runtime_identity_and_acl(monkeypatch):
    connection = _install_driver(
        monkeypatch,
        {
            "role": "tybot_vipbot_runtime",
            "may_execute": True,
            "may_read_connection": False,
            "may_read_secret": False,
        },
    )

    assert runtime._connect() is connection
    assert "current_user AS role" in connection.cursor_value.query


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "tyslackai"),
        ("may_execute", False),
        ("may_read_connection", True),
        ("may_read_secret", True),
    ],
)
def test_connect_rejects_the_wrong_identity_or_effective_acl(
    monkeypatch, field, value
):
    identity = {
        "role": "tybot_vipbot_runtime",
        "may_execute": True,
        "may_read_connection": False,
        "may_read_secret": False,
    }
    identity[field] = value
    connection = _install_driver(monkeypatch, identity)

    with pytest.raises(
        runtime.VipbotRuntimeStoreError,
        match="vipbot_database_privilege_mismatch",
    ):
        runtime._connect()

    assert connection.closed is True


def test_runtime_config_returns_only_the_vipbot_token_pair(monkeypatch):
    _install(monkeypatch)

    result = runtime.load_runtime_config("tyit")

    assert result["bot_token"] == "xoxb-vipbot"
    assert result["app_token"] == "xapp-vipbot"
    assert result["bot_user_id"] == "U_VIPBOT"
    assert "bot_ciphertext" not in result
    assert "app_ciphertext" not in result


def test_runtime_config_uses_only_the_restricted_function(monkeypatch):
    connection = _install(monkeypatch)

    runtime.load_runtime_config("tyit")

    cursor = connection.cursor_value
    assert cursor.query == "SELECT * FROM vipbot_connection_config(%s)"
    assert cursor.params == ("tyit",)


def test_runtime_config_fails_closed_without_a_verified_connection(monkeypatch):
    connection = Connection(None)
    monkeypatch.setattr(runtime, "_connect", lambda: connection)

    with pytest.raises(runtime.VipbotRuntimeStoreError, match="VIPBot 연결"):
        runtime.load_runtime_config("tyit")


def test_decryption_failure_does_not_expose_ciphertext(monkeypatch):
    marker = b"secret-ciphertext-marker"
    _install(
        monkeypatch,
        {
            "state": "enabled",
            "team_id": "T123",
            "bot_user_id": "U_VIPBOT",
            "bot_ciphertext": marker,
            "app_ciphertext": marker,
        },
    )

    with pytest.raises(runtime.VipbotRuntimeStoreError) as caught:
        runtime.load_runtime_config("tyit")

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert marker.decode() not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
