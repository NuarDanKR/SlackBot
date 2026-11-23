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
        if "EXCLUDED" in normalized:  # pragma: no cover - 암호문 SELECT 권한을 요구한다
            raise AssertionError("ON CONFLICT ... EXCLUDED 는 콘솔 계정에서 permission denied")
        if normalized.startswith("UPDATE archiver_image_provider_secret SET ciphertext"):
            ciphertext, mask, actor, provider = params
            row = self.connection.rows.get(provider)
            if row:
                row.update(ciphertext=ciphertext, mask=mask, enabled=True, updated_by=actor)
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif normalized.startswith("INSERT INTO archiver_image_provider_secret"):
            ciphertext, mask, actor, provider = params
            self.connection.rows[provider] = {
                "provider": provider,
                "ciphertext": ciphertext,
                "mask": mask,
                "enabled": True,
                "updated_by": actor,
                "updated_at": "now",
            }
            self.rowcount = 1
        elif normalized.startswith("UPDATE archiver_image_provider_secret SET enabled"):
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



# --- 2026-11-12 보안 리뷰: 전용 키·좁은 마스크 --------------------------------

import os  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402

KEY = "AIza" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345"


def _key_file(tmp_path, monkeypatch, *, mode=0o400):
    path = tmp_path / "vision-provider-secret.key"
    path.write_text(Fernet.generate_key().decode(), encoding="ascii")
    os.chmod(path, mode)
    monkeypatch.setenv(secret.KEY_FILE_ENV, str(path))
    return path


def test_round_trip_uses_only_the_dedicated_key(tmp_path, monkeypatch):
    _key_file(tmp_path, monkeypatch)
    # 워크스페이스 키가 있어도 쓰지 않는다.
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode())
    connection = Connection()
    secret.save_provider_key(connection, "google", KEY, actor="admin@example.com")
    assert secret.resolve_provider_key(connection, "google") == KEY


def test_no_dedicated_key_never_falls_back_to_the_workspace_key(tmp_path, monkeypatch):
    monkeypatch.setenv(secret.KEY_FILE_ENV, str(tmp_path / "missing.key"))
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(secret.ImageProviderSecretError, match="provider_secret_key_unavailable"):
        secret.save_provider_key(Connection(), "google", KEY, actor="admin@example.com")


@pytest.mark.skipif(os.name != "posix", reason="파일 권한 비트는 POSIX 에서만 의미가 있다")
def test_a_group_readable_key_file_is_refused(tmp_path, monkeypatch):
    _key_file(tmp_path, monkeypatch, mode=0o640)
    with pytest.raises(secret.ImageProviderSecretError, match="provider_secret_key_permissions"):
        secret.save_provider_key(Connection(), "google", KEY, actor="admin@example.com")


def test_a_row_encrypted_with_the_old_workspace_key_is_unreadable_not_guessed(
    tmp_path, monkeypatch
):
    """예전 판은 워크스페이스 키로 암호화했다. 새 키로 못 풀면 **보류**다 — 콘솔에서 다시 넣는다."""
    _key_file(tmp_path, monkeypatch)
    old = Fernet(Fernet.generate_key())
    connection = Connection()
    connection.rows["google"] = {
        "provider": "google", "ciphertext": old.encrypt(KEY.encode()), "mask": "x",
        "enabled": True, "updated_by": "admin", "updated_at": "now",
    }
    with pytest.raises(secret.ImageProviderSecretError, match="provider_secret_unreadable"):
        secret.resolve_provider_key(connection, "google")


def test_the_mask_shows_only_the_tail_and_a_fingerprint(monkeypatch):
    monkeypatch.setattr(secret, "_fernet", Cipher)
    connection = Connection()
    secret.save_provider_key(connection, "google", KEY, actor="admin@example.com")
    mask = connection.rows["google"]["mask"]
    assert mask.startswith("…" + KEY[-4:] + " (sha256:")
    assert KEY[:9] not in mask and KEY[4:12] not in mask
    visible = mask.split(" ", 1)[0].lstrip("…")
    assert len(visible) == 4


def test_short_provider_keys_are_refused():
    with pytest.raises(secret.ImageProviderSecretError, match="provider_key_invalid"):
        secret.save_provider_key(Connection(), "google", "A" * 19, actor="admin@example.com")


def test_the_module_no_longer_imports_the_workspace_key():
    source = (
        __import__("pathlib").Path(secret.__file__).read_text(encoding="utf-8")
    )
    assert "workspace_store" not in source
    assert "WORKSPACE_SECRET_KEY" not in source.split('"""', 2)[2]


def test_console_writes_with_its_own_connection_not_the_vision_dsn():
    from tybot.console import image_description_secret_admin as admin

    source = __import__("inspect").getsource(admin._connect)
    assert "DATABASE_URL" in source
    assert "resolve_vision_database_url" not in source


def test_replacing_a_key_updates_in_place_without_reading_ciphertext(monkeypatch):
    """콘솔 계정은 암호문 열 SELECT 가 없다. 교체가 EXCLUDED 를 쓰면 permission denied 다."""
    monkeypatch.setattr(secret, "_fernet", Cipher)
    connection = Connection()
    secret.save_provider_key(connection, "google", "AIza" + "a" * 32, actor="first")
    secret.save_provider_key(connection, "google", "AIza" + "b" * 32, actor="second")
    assert len(connection.rows) == 1
    assert connection.rows["google"]["updated_by"] == "second"
    assert secret.resolve_provider_key(connection, "google") == "AIza" + "b" * 32
    for sql in (secret._UPDATE_KEY, secret._INSERT_KEY):
        assert "EXCLUDED" not in sql and "ON CONFLICT" not in sql
