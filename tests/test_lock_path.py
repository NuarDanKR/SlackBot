"""락 경로 해석 — 상대경로로 떨어지면 운영에서 기동이 막힌다.

실제 사고: `ARCHIVE_DIR` 이 비어 기본값 `./archive` 로 떨어졌고, 락이 `./.locks` 가 되면서
`WorkingDirectory=/opt/tybot`(ProtectSystem=strict 로 읽기 전용) 아래를 가리켜
"Read-only file system" 으로 봇이 재시작 루프에 빠졌다.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import tybot.config
import tybot.lock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("STATE_DIR", "LOCK_DIR", "ARCHIVE_DIR", "QA_LOG_DIR", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(tybot.config)
    importlib.reload(tybot.lock)
    yield


def test_lock_dir_is_always_absolute():
    assert tybot.lock._lock_dir().is_absolute()


def test_lock_dir_follows_archive_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "state" / "archive"))
    assert tybot.lock._lock_dir() == (tmp_path / "state" / ".locks").resolve()


def test_state_dir_wins_over_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "a" / "archive"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "chosen"))
    assert tybot.lock._lock_dir() == (tmp_path / "chosen" / ".locks").resolve()


def test_lock_dir_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCK_DIR", str(tmp_path / "locks"))
    assert tybot.lock._lock_dir() == (tmp_path / "locks").resolve()


def test_relative_archive_dir_still_resolves_absolute(monkeypatch):
    monkeypatch.setenv("ARCHIVE_DIR", "./archive")
    assert tybot.lock._lock_dir().is_absolute()


def test_unwritable_lock_dir_falls_back_instead_of_dying(monkeypatch, tmp_path):
    """락을 못 잡아 봇 전체가 안 뜨는 것보다, 경고 남기고 뜨는 쪽이 낫다."""
    blocked = tmp_path / "file"
    blocked.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LOCK_DIR", str(blocked / "sub"))

    path = tybot.lock._resolve_lock_path("instance-bot")
    assert path.name == "instance-bot.lock"
    assert path.is_absolute()
    assert "tybot-locks" in str(path)  # 임시 경로로 물러났다


def test_normal_lock_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCK_DIR", str(tmp_path / "made" / "here"))
    path = tybot.lock._resolve_lock_path("instance-bot")
    assert path.parent.is_dir()
    assert path == (tmp_path / "made" / "here" / "instance-bot.lock").resolve()


def test_instance_lock_acquires_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    lock = tybot.lock.instance_lock("bot")
    lock.acquire()
    try:
        assert Path(lock.path).parent == (tmp_path / ".locks").resolve()
    finally:
        lock.release()
