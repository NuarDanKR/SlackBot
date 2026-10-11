from __future__ import annotations

import os

import pytest

from tybot.specialist_v3_secrets import CredentialStore, credential_id


def test_credentials_are_scoped_by_specialist_and_workspace(tmp_path):
    tyit = tmp_path / "hermes-tyit.hmac"
    pilot = tmp_path / "hermes-pilot.hmac"
    tyit.write_bytes(b"t" * 32 + b"\n")
    pilot.write_bytes(b"p" * 32)
    if os.name != "nt":
        tyit.chmod(0o440)
        pilot.chmod(0o440)

    store = CredentialStore(tmp_path)

    assert store.get("hermes", "tyit") == ("hermes:tyit", b"t" * 32)
    assert store.get("hermes", "pilot") == ("hermes:pilot", b"p" * 32)


def test_missing_short_or_path_like_credentials_fail_closed(tmp_path):
    store = CredentialStore(tmp_path)
    with pytest.raises(RuntimeError, match="unreadable"):
        store.get("hermes", "tyit")

    (tmp_path / "hermes-tyit.hmac").write_bytes(b"short")
    with pytest.raises(RuntimeError, match="too short"):
        store.get("hermes", "tyit")

    with pytest.raises(ValueError, match="invalid"):
        credential_id("../hermes", "tyit")


@pytest.mark.skipif(os.name == "nt", reason="symlink permission differs on Windows")
def test_symlink_credentials_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 32)
    source.chmod(0o440)
    (tmp_path / "hermes-tyit.hmac").symlink_to(source)

    with pytest.raises(RuntimeError, match="unreadable"):
        CredentialStore(tmp_path).get("hermes", "tyit")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_group_writable_credentials_are_rejected(tmp_path):
    target = tmp_path / "hermes-tyit.hmac"
    target.write_bytes(b"x" * 32)
    target.chmod(0o660)

    with pytest.raises(RuntimeError, match="group/world"):
        CredentialStore(tmp_path).get("hermes", "tyit")
