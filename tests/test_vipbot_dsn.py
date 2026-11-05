import os

import pytest

from tybot import vipbot_dsn

VIP_URL = "postgresql://tybot_vipbot_runtime:placeholder@localhost:55432/tyslackai"
MASTER_URL = "postgresql://tyslackai@localhost:55432/tyslackai"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "VIPBOT_DATABASE_URL",
        "VIPBOT_DATABASE_URL_FILE",
        "CREDENTIALS_DIRECTORY",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _code(call) -> str:
    with pytest.raises(vipbot_dsn.VipbotDsnError) as caught:
        call()
    return caught.value.code


def _private(path, text=VIP_URL):
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def test_plaintext_environment_dsn_is_not_supported(monkeypatch):
    monkeypatch.setenv("VIPBOT_DATABASE_URL", VIP_URL)

    assert _code(vipbot_dsn.resolve_vipbot_database_url) == vipbot_dsn.UNCONFIGURED


def test_shared_database_url_is_never_a_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", MASTER_URL)

    assert _code(vipbot_dsn.resolve_vipbot_database_url) == vipbot_dsn.UNCONFIGURED


def test_copying_the_shared_database_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", MASTER_URL)
    path = _private(tmp_path / "dsn", MASTER_URL)
    monkeypatch.setenv("VIPBOT_DATABASE_URL_FILE", str(path))

    assert _code(vipbot_dsn.resolve_vipbot_database_url) == vipbot_dsn.SHARED


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://tyslackai@example/db",
        "postgresql://tybot_vipbot@example/db",
        "postgresql://tybot_vipbot_runtime@example/db?user=postgres",
        "dbname=x user=tybot_vipbot_runtime",
        "not-a-dsn",
    ],
)
def test_only_the_runtime_login_is_accepted(monkeypatch, tmp_path, value):
    path = _private(tmp_path / "dsn", value)
    monkeypatch.setenv("VIPBOT_DATABASE_URL_FILE", str(path))

    assert _code(vipbot_dsn.resolve_vipbot_database_url) == vipbot_dsn.WRONG_ROLE


def test_explicit_file_is_read(monkeypatch, tmp_path):
    path = _private(tmp_path / "dsn")
    monkeypatch.setenv("VIPBOT_DATABASE_URL_FILE", str(path))

    assert vipbot_dsn.resolve_vipbot_database_url() == VIP_URL


def test_systemd_credential_is_read(monkeypatch, tmp_path):
    _private(tmp_path / vipbot_dsn.CREDENTIAL_NAME)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    assert vipbot_dsn.resolve_vipbot_database_url() == VIP_URL


def test_missing_explicit_file_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("VIPBOT_DATABASE_URL_FILE", str(tmp_path / "missing"))

    assert _code(vipbot_dsn.resolve_vipbot_database_url) == vipbot_dsn.UNREADABLE
