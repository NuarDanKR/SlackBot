from __future__ import annotations

import pytest

pytest.importorskip("cryptography", reason="워크스페이스 토큰 암호화 선택 의존성")

from cryptography.fernet import Fernet

from tybot.console import workspace_store


def test_workspace_cipher_round_trip(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    cipher = workspace_store._fernet()
    token = b"xoxb-test_token_value"

    encrypted = cipher.encrypt(token)

    assert encrypted != token
    assert cipher.decrypt(encrypted) == token


def test_workspace_mask_does_not_contain_middle_of_token():
    token = "xoxb-visi_ble_secret_middle_last"

    masked = workspace_store._mask(token)

    assert masked.startswith("xoxb-visi")
    assert masked.endswith("last")
    assert "secret_middle" not in masked


def test_workspace_cipher_requires_dedicated_key(monkeypatch):
    monkeypatch.delenv("WORKSPACE_SECRET_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    with pytest.raises(workspace_store.WorkspaceStoreError, match="WORKSPACE_SECRET_KEY"):
        workspace_store._fernet()
