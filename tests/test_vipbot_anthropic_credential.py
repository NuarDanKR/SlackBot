from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tybot import vipbot_anthropic_credential as credential

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "tybot" / "vipbot_anthropic_credential.py"
KEY_PREFIX = "sk-" + "ant-"
KEY = KEY_PREFIX + "synthetic-vipbot-key-000000000000"


def private(path, value=KEY):
    path.write_text(value, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def code(call) -> str:
    with pytest.raises(credential.VipbotAnthropicCredentialError) as caught:
        call()
    return caught.value.code


def resolve(directory, **extra):
    return credential.resolve_anthropic_api_key({"CREDENTIALS_DIRECTORY": str(directory), **extra})


def test_systemd_credential_is_the_only_success_path(tmp_path):
    private(tmp_path / credential.CREDENTIAL_NAME)
    assert resolve(tmp_path) == KEY


def test_plaintext_environment_presence_is_rejected_without_reading_its_value(tmp_path):
    private(tmp_path / credential.CREDENTIAL_NAME)
    assert (
        code(lambda: resolve(tmp_path, ANTHROPIC_API_KEY="must-not-be-read"))
        == credential.PLAINTEXT_ENV
    )


def test_missing_directory_or_credential_is_unconfigured(tmp_path):
    assert code(lambda: credential.resolve_anthropic_api_key({})) == credential.UNCONFIGURED
    assert code(lambda: resolve(tmp_path)) == credential.UNCONFIGURED


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "sk-other-synthetic-key-000000000000",
        "sk-ant-contains space 000000000000",
        "sk-ant-line-one\nline-two-000000000000",
        "sk-ant-비ASCII키-000000000000",
        KEY_PREFIX + "valid-looking-000000000000\0tail",
        KEY_PREFIX + ("x" * 600),
    ],
)
def test_malformed_values_are_rejected_without_echoing_them(tmp_path, value):
    private(tmp_path / credential.CREDENTIAL_NAME, value)
    with pytest.raises(credential.VipbotAnthropicCredentialError) as caught:
        resolve(tmp_path)
    assert caught.value.code in {credential.INVALID, credential.UNREADABLE}
    assert not value or value not in str(caught.value)


def test_a_directory_or_symlink_is_not_a_credential(tmp_path):
    target = private(tmp_path / "target")
    link = tmp_path / credential.CREDENTIAL_NAME
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    assert code(lambda: resolve(tmp_path)) == credential.UNREADABLE


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_group_or_world_readable_credentials_are_rejected(tmp_path):
    path = private(tmp_path / credential.CREDENTIAL_NAME)
    path.chmod(0o640)
    assert code(lambda: resolve(tmp_path)) == credential.BAD_MODE


def test_error_chains_and_messages_hold_no_path_or_secret(tmp_path):
    secret = KEY_PREFIX + "secret-material-000000000000"
    private(tmp_path / credential.CREDENTIAL_NAME, secret)
    with pytest.raises(credential.VipbotAnthropicCredentialError) as caught:
        credential.resolve_anthropic_api_key({"ANTHROPIC_API_KEY": secret})
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_the_resolver_has_no_db_provider_sdk_or_network_import():
    modules = set()
    for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules == {"__future__", "os", "stat", "collections.abc", "pathlib"}
