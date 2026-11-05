"""Resolve the dedicated VIPBot database credential without a shared fallback."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

CREDENTIAL_NAME = "tybot-vipbot-database-url"
RUNTIME_ROLE = "tybot_vipbot_runtime"
UNCONFIGURED = "vipbot_database_unconfigured"
BAD_MODE = "vipbot_database_credential_mode"
UNREADABLE = "vipbot_database_credential_unreadable"
SHARED = "vipbot_database_shared_credential"
WRONG_ROLE = "vipbot_database_wrong_role"


class VipbotDsnError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_file(path: Path, *, explicit: bool) -> str:
    code = ""
    value = ""
    try:
        info = path.stat()
    except OSError:
        info = None
        code = UNREADABLE if explicit else UNCONFIGURED
    if not code and os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        code = BAD_MODE
    if not code:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            code = UNREADABLE
    if not code and not value:
        code = UNREADABLE
    if code:
        raise VipbotDsnError(code)
    return value


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _dsn_login(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        username = unquote(parsed.username or "")
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "user":
                username = value
        return parsed.scheme, username
    except ValueError:
        return "", ""


def resolve_vipbot_database_url() -> str:
    """Return only a VIPBot runtime DSN for the exact least-privilege role."""
    url = ""
    explicit = _env("VIPBOT_DATABASE_URL_FILE")
    directory = _env("CREDENTIALS_DIRECTORY")
    if explicit:
        url = _read_file(Path(explicit), explicit=True)
    elif directory:
        url = _read_file(Path(directory) / CREDENTIAL_NAME, explicit=False)
    if not url:
        raise VipbotDsnError(UNCONFIGURED)
    if url == _env("DATABASE_URL"):
        raise VipbotDsnError(SHARED)
    scheme, username = _dsn_login(url)
    if scheme not in {"postgres", "postgresql"} or username != RUNTIME_ROLE:
        raise VipbotDsnError(WRONG_ROLE)
    return url


__all__ = [
    "BAD_MODE",
    "CREDENTIAL_NAME",
    "RUNTIME_ROLE",
    "SHARED",
    "UNCONFIGURED",
    "UNREADABLE",
    "WRONG_ROLE",
    "VipbotDsnError",
    "resolve_vipbot_database_url",
]
