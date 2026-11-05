"""Resolve VIPBot's Anthropic key from one systemd credential only."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

CREDENTIAL_NAME = "tybot-vipbot-anthropic-api-key"
PLAINTEXT_ENV = "vipbot_anthropic_plaintext_environment"
UNCONFIGURED = "vipbot_anthropic_credential_unconfigured"
UNREADABLE = "vipbot_anthropic_credential_unreadable"
BAD_MODE = "vipbot_anthropic_credential_mode"
INVALID = "vipbot_anthropic_credential_invalid"

_MIN_BYTES = 24
_MAX_BYTES = 512


class VipbotAnthropicCredentialError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> str:
    try:
        info = path.stat()
    except OSError:
        raise VipbotAnthropicCredentialError(UNCONFIGURED) from None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES + 2:
        raise VipbotAnthropicCredentialError(UNREADABLE)
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise VipbotAnthropicCredentialError(BAD_MODE)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise VipbotAnthropicCredentialError(UNREADABLE) from None

    if raw.endswith("\r\n"):
        key = raw[:-2]
    elif raw.endswith("\n"):
        key = raw[:-1]
    else:
        key = raw
    if (
        not key.startswith("sk-ant-")
        or not _MIN_BYTES <= len(key) <= _MAX_BYTES
        or not key.isascii()
        or any(character.isspace() for character in key)
        or any(ord(character) < 33 or ord(character) > 126 for character in key)
    ):
        raise VipbotAnthropicCredentialError(INVALID)
    return key


def resolve_anthropic_api_key(environ: Mapping[str, str] | None = None) -> str:
    """Return the dedicated key without consulting env values, DB, or shared config."""
    values = os.environ if environ is None else environ
    if "ANTHROPIC_API_KEY" in values:
        raise VipbotAnthropicCredentialError(PLAINTEXT_ENV)
    directory = str(values.get("CREDENTIALS_DIRECTORY", "") or "").strip()
    if not directory:
        raise VipbotAnthropicCredentialError(UNCONFIGURED)
    return _read(Path(directory) / CREDENTIAL_NAME)


__all__ = [
    "BAD_MODE",
    "CREDENTIAL_NAME",
    "INVALID",
    "PLAINTEXT_ENV",
    "UNCONFIGURED",
    "UNREADABLE",
    "VipbotAnthropicCredentialError",
    "resolve_anthropic_api_key",
]
