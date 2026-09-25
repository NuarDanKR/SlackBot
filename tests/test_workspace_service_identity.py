import pytest
from cryptography.fernet import Fernet

from tybot.console import workspace_service_identity as identity


class Cursor:
    def __init__(self, ciphertexts):
        self.ciphertexts = ciphertexts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query, _params):
        return None

    def fetchall(self):
        return [
            {"kind": kind, "ciphertext": ciphertext}
            for kind, ciphertext in self.ciphertexts.items()
        ]


class Connection:
    def __init__(self, ciphertexts):
        self.ciphertexts = ciphertexts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return Cursor(self.ciphertexts)


def test_identity_check_uses_stored_token_without_returning_it(monkeypatch):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    bot_secret = "xoxb-" + "super-secret"
    app_secret = "xapp-" + "super-secret"
    monkeypatch.setattr(
        identity,
        "_connect",
        lambda: Connection({
            "bot": cipher.encrypt(bot_secret.encode()),
            "app": cipher.encrypt(app_secret.encode()),
        }),
    )
    monkeypatch.setattr(identity, "_fernet", lambda: cipher)
    observed = {}

    class Client:
        def __init__(self, *, token):
            observed.setdefault("tokens", []).append(token)

        def auth_test(self):
            return {"team_id": "T123", "user_id": "U456"}

        def apps_connections_open(self):
            return {"ok": True, "url": "wss://example.invalid/secret"}

    def record(workspace, service, actual, *, actor):
        observed.update(
            workspace=workspace,
            service=str(service),
            team=actual.team_id,
            user=actual.bot_user_id,
            actor=actor,
        )
        return ""

    monkeypatch.setattr(identity, "record_identity", record)

    result = identity.verify_service(
        "tyit", "archiver", actor="dan", client_factory=Client
    )

    assert result == ""
    assert observed == {
        "tokens": [bot_secret, app_secret],
        "workspace": "tyit",
        "service": "archiver",
        "team": "T123",
        "user": "U456",
        "actor": "dan",
    }


def test_identity_check_requires_both_tokens(monkeypatch):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    monkeypatch.setattr(
        identity,
        "_connect",
        lambda: Connection({"bot": cipher.encrypt(b"xoxb-only")}),
    )

    with pytest.raises(identity.ServiceStoreError, match="토큰 쌍"):
        identity.verify_service("tyit", "archiver", actor="dan")
