"""Keep ordinary pytest runs away from repository and managed production DSNs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_ENV_FILE_GUARDS = ("TYBOT_ENV_FILE", "ENV_SETTINGS_PATH")
_RUNTIME_PATH_GUARDS = ("STATE_DIR", "LOCK_DIR")
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
    guard_root = Path(_guard_dir.name)
    empty = guard_root / "empty.env"
    empty.write_text("", encoding="utf-8")
    for key in (*_ENV_FILE_GUARDS, *_RUNTIME_PATH_GUARDS):
        _previous[key] = os.environ.get(key)
    for key in _ENV_FILE_GUARDS:
        os.environ[key] = str(empty)
    os.environ["STATE_DIR"] = str(guard_root / "runtime")
    os.environ["LOCK_DIR"] = str(guard_root / "locks")


@pytest.fixture(autouse=True)
def _conversion_policy_known_off():
    """변환 정책을 **DB 에서 읽은 「위임 꺼짐」** 으로 둔다 (B-89 후속).

    시험에는 DB 가 없다. 그대로 두면 정책이 「모름」 이 되어 HWP 직접 변환이 모든
    시험에서 막힌다. 정책 자체를 시험하는 곳은 `set_override(None)` 으로 이것을 걷는다.
    """
    from tybot import conversion_policy

    conversion_policy.set_override(conversion_policy.from_db())
    yield
    conversion_policy.set_override(None)


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
