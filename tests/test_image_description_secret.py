from __future__ import annotations

import pytest

from tybot.archive import image_description_secret as secret


class Cipher:
    def encrypt(self, value: bytes) -> bytes:
        return value[::-1]

    def decrypt(self, value: bytes) -> bytes:
        return value[::-1]


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO archiver_image_provider_secret"):
            provider, ciphertext, mask, actor = params
            self.connection.rows[provider] = {
                "provider": provider,
                "ciphertext": ciphertext,
                "mask": mask,
                "enabled": True,
                "updated_by": actor,
                "updated_at": "now",
            }
            self.rowcount = 1
        elif normalized.startswith("UPDATE archiver_image_provider_secret"):
            enabled, actor, provider = params
            row = self.connection.rows.get(provider)
            if row:
                row.update(enabled=enabled, updated_by=actor)
                self.rowcount = 1
        elif normalized.startswith("SELECT ciphertext"):
            provider = params[0]
            row = self.connection.rows.get(provider)
            self.result = [row] if row and row["enabled"] else []
        elif normalized.startswith("SELECT provider,mask"):
            self.result = [
                {key: row[key] for key in (
                    "provider", "mask", "enabled", "updated_by", "updated_at"
                )}
                for row in sorted(
                    self.connection.rows.values(), key=lambda item: item["provider"]
                )
            ]
        else:  # pragma: no cover - catches query drift loudly
            raise AssertionError(normalized)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class Connection:
    def __init__(self):
        self.rows = {}

    def cursor(self):
        return Cursor(self)


def test_key_is_encrypted_and_only_mask_is_listed(monkeypatch):
    monkeypatch.setattr(secret, "_fernet", Cipher)
    connection = Connection()
    value = "AIza" + "x" * 32

    secret.save_provider_key(connection, "google", value, actor="admin@example.com")

    row = connection.rows["google"]
    assert value.encode() not in row["ciphertext"]
    assert row["mask"] != value
    listed = secret.list_provider_keys(connection)
    assert listed[0]["provider"] == "google"
    assert "ciphertext" not in listed[0]
    assert secret.resolve_provider_key(connection, "google") == value


def test_disabled_or_missing_key_has_no_environment_fallback(monkeypatch):
    monkeypatch.setattr(secret, "_fernet", Cipher)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-read")
    connection = Connection()

    with pytest.raises(secret.ImageProviderSecretError, match="provider_secret_unavailable"):
        secret.resolve_provider_key(connection, "google")

    secret.save_provider_key(
        connection, "google", "AIza" + "x" * 32, actor="admin@example.com"
    )
    secret.set_provider_key_enabled(
        connection, "google", False, actor="admin@example.com"
    )
    with pytest.raises(secret.ImageProviderSecretError, match="provider_secret_unavailable"):
        secret.resolve_provider_key(connection, "google")


@pytest.mark.parametrize(
    "provider,key,code",
    [
        ("../google", "AIza" + "x" * 32, "provider_invalid"),
        ("google", "short", "provider_key_invalid"),
        ("google", "has whitespace in key", "provider_key_invalid"),
    ],
)
def test_invalid_secret_input_is_rejected(provider, key, code):
    with pytest.raises(secret.ImageProviderSecretError, match=code):
        secret.save_provider_key(Connection(), provider, key, actor="admin@example.com")

