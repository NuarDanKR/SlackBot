"""Keep ordinary pytest runs away from repository and managed production DSNs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_GUARDED = ("TYBOT_ENV_FILE", "ENV_SETTINGS_PATH")
_guard_dir: tempfile.TemporaryDirectory[str] | None = None
_previous: dict[str, str | None] = {}


def pytest_configure(config: pytest.Config) -> None:
    """Run before test modules import code that may call load_env_file()."""
    global _guard_dir

    if os.environ.get("DATABASE_URL"):
        raise pytest.UsageError(
            "pytest refuses an ambient DATABASE_URL. Use the dedicated "
            "TYBOT_SCHEMA_TEST_DSN for isolated PostgreSQL tests."
        )
    _guard_dir = tempfile.TemporaryDirectory(prefix="tybot-pytest-env-")
    empty = Path(_guard_dir.name) / "empty.env"
    empty.write_text("", encoding="utf-8")
    for key in _GUARDED:
        _previous[key] = os.environ.get(key)
        os.environ[key] = str(empty)


def pytest_unconfigure(config: pytest.Config) -> None:
    global _guard_dir

    for key, value in _previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _previous.clear()
    if _guard_dir is not None:
        _guard_dir.cleanup()
        _guard_dir = None
