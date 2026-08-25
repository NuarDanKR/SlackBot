"""프로세스 락 — 봇 이중 기동과 아카이브 동시 append 를 실제로 막는지 검증."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import UTC
from pathlib import Path

import pytest

from tybot.lock import (
    AlreadyRunning,
    FileLock,
    LockUnavailable,
    PostgresAdvisoryLock,
    archive_write_lock,
    instance_lock,
    make_lock,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")


# --- 파일 락 ---------------------------------------------------------------

def test_second_acquire_is_refused(tmp_path):
    """같은 락을 두 번 잡을 수 없다 — 이중 기동을 막는 핵심 성질."""
    a = FileLock(tmp_path / "x.lock", label="테스트")
    b = FileLock(tmp_path / "x.lock", label="테스트")
    a.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            b.acquire()
    finally:
        a.release()


def test_release_allows_reacquire(tmp_path):
    a = FileLock(tmp_path / "x.lock", label="테스트")
    a.acquire()
    a.release()

    b = FileLock(tmp_path / "x.lock", label="테스트")
    b.acquire()  # 예외 없이 다시 잡힌다
    b.release()


def test_error_message_names_the_holder(tmp_path):
    """오류 메시지만 보고 누가 쥐고 있는지 찾을 수 있어야 한다."""
    a = FileLock(tmp_path / "x.lock", label="봇 단일 실행")
    a.acquire()
    try:
        with pytest.raises(AlreadyRunning) as err:
            FileLock(tmp_path / "x.lock", label="봇 단일 실행").acquire()
        msg = str(err.value)
        assert "봇 단일 실행" in msg
        assert f"pid={os.getpid()}" in msg
        assert "x.lock" in msg
    finally:
        a.release()


def test_creates_missing_directory(tmp_path):
    lock = FileLock(tmp_path / "없던폴더" / "x.lock", label="테스트")
    lock.acquire()
    try:
        assert (tmp_path / "없던폴더" / "x.lock").exists()
    finally:
        lock.release()


def test_unwritable_path_reports_clearly(tmp_path):
    """락 경로를 만들 수 없으면 조용히 넘기지 않고 사유를 알린다."""
    blocker = tmp_path / "blocker"
    blocker.write_text("파일이라 폴더가 될 수 없다", encoding="utf-8")
    with pytest.raises(LockUnavailable) as err:
        FileLock(blocker / "x.lock", label="테스트").acquire()
    assert "테스트" in str(err.value)


def test_context_manager_releases(tmp_path):
    path = tmp_path / "x.lock"
    with FileLock(path, label="테스트"):
        with pytest.raises(AlreadyRunning):
            FileLock(path, label="테스트").acquire()
    again = FileLock(path, label="테스트")
    again.acquire()  # 블록을 벗어났으니 다시 잡힌다
    again.release()


def test_timeout_waits_then_gives_up(tmp_path):
    a = FileLock(tmp_path / "x.lock", label="테스트")
    a.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            FileLock(tmp_path / "x.lock", label="테스트").acquire(timeout=0.2, poll=0.02)
    finally:
        a.release()


def test_lock_dies_with_the_process(tmp_path):
    """kill -9 로 죽어도 다음 기동이 막히면 안 된다 — OS 가 락을 회수한다.

    자식 프로세스에서 락을 잡고 죽인 뒤, 부모가 같은 락을 잡을 수 있어야 한다.
    직접 만든 pid 파일 방식에는 이 성질이 없어서 파일 락을 쓴다.
    """
    path = tmp_path / "x.lock"
    code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {SRC!r})
        from tybot.lock import FileLock
        held = FileLock({str(path)!r}, label="자식")
        held.acquire()
        print("locked", flush=True)
        time.sleep(30)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        # 자식이 살아 있는 동안에는 잡히지 않는다
        with pytest.raises(AlreadyRunning):
            FileLock(path, label="부모").acquire()
    finally:
        child.kill()
        child.wait(timeout=10)

    # 자식이 죽었으니 잡힌다. 윈도우는 프로세스 종료 후 핸들 정리에 100ms 정도 걸리므로 조금 기다린다.
    lock = FileLock(path, label="부모")
    lock.acquire(timeout=5.0, poll=0.05)
    lock.release()


# --- 락 선택 --------------------------------------------------------------

def test_file_lock_when_no_database_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LOCK_DIR", str(tmp_path))
    lock = instance_lock("bot")
    assert isinstance(lock, FileLock)
    assert lock.path == tmp_path / "instance-bot.lock"


def test_advisory_lock_when_database_url_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/tybot")
    lock = make_lock("instance-bot", label="봇 단일 실행")
    assert isinstance(lock, PostgresAdvisoryLock)


def test_bot_and_collect_locks_do_not_block_each_other(tmp_path, monkeypatch):
    """봇과 정기 백필은 서로를 막아서는 안 된다. 아카이브 쓰기만 따로 직렬화한다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LOCK_DIR", str(tmp_path))
    bot = instance_lock("bot")
    collect = instance_lock("collect")
    bot.acquire()
    try:
        collect.acquire()  # 예외가 없어야 한다
        collect.release()
    finally:
        bot.release()


# --- Postgres advisory 락 (가짜 커넥션) ------------------------------------

class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


class FakeConn:
    """pg_try_advisory_lock 을 흉내내는 최소 커넥션."""

    def __init__(self, granted: bool):
        self.granted = granted
        self.calls: list[str] = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append(sql.split("(")[0].strip())
        if "pg_try_advisory_lock" in sql:
            return FakeCursor([(self.granted,)])
        if "pg_advisory_unlock" in sql:
            return FakeCursor([(True,)])
        if "pg_stat_activity" in sql:
            return FakeCursor([(4242, "tybot", "2026-08-21 09:00")])
        return FakeCursor([(None,)])

    def close(self):
        self.closed = True


def test_advisory_lock_acquires_and_releases():
    conn = FakeConn(granted=True)
    lock = PostgresAdvisoryLock(
        "postgresql://x", label="봇 단일 실행", name="instance-bot", connect=lambda _: conn
    )
    lock.acquire()
    assert any("pg_try_advisory_lock" in c for c in conn.calls)
    lock.release()
    assert any("pg_advisory_unlock" in c for c in conn.calls)
    assert conn.closed


def test_advisory_lock_refused_reports_session():
    """이미 다른 세션이 쥐고 있으면 그 세션 정보를 알려 준다."""
    conn = FakeConn(granted=False)
    lock = PostgresAdvisoryLock(
        "postgresql://x", label="봇 단일 실행", name="instance-bot", connect=lambda _: conn
    )
    with pytest.raises(AlreadyRunning) as err:
        lock.acquire()
    assert "pid=4242" in str(err.value)
    assert conn.closed  # 실패했으면 연결을 남기지 않는다


def test_advisory_keys_differ_per_name():
    """서로 다른 락이 같은 키를 쓰면 봇과 백필이 서로를 막는다."""
    a = PostgresAdvisoryLock("x", label="a", name="instance-bot", connect=lambda _: FakeConn(True))
    b = PostgresAdvisoryLock("x", label="b", name="instance-collect", connect=lambda _: FakeConn(True))
    c = PostgresAdvisoryLock("x", label="c", name="archive-write", connect=lambda _: FakeConn(True))
    assert len({a.key, b.key, c.key}) == 3


# --- 아카이브 쓰기 락 ------------------------------------------------------

def test_archive_write_lock_serialises(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with archive_write_lock(tmp_path):
        # 락을 쥔 동안에는 다른 프로세스가 잡을 수 없다
        with pytest.raises(AlreadyRunning):
            FileLock(tmp_path / ".write.lock", label="아카이브 쓰기").acquire()
    # 블록을 벗어나면 풀린다
    other = FileLock(tmp_path / ".write.lock", label="아카이브 쓰기")
    other.acquire()
    other.release()


def test_archive_write_lock_does_not_drop_data_when_lock_fails(tmp_path, monkeypatch, caplog):
    """락을 못 잡아도 수집은 진행한다 — 원문을 버리는 쪽이 더 나쁘다.

    락 경로를 파일로 막아 acquire 가 실패하게 만든 뒤, 블록이 그대로 실행되는지 본다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    blocker = tmp_path / "archive"
    blocker.write_text("폴더 자리에 파일", encoding="utf-8")

    ran = False
    with caplog.at_level("WARNING"):
        with archive_write_lock(blocker):
            ran = True
    assert ran
    assert "락 없이 진행" in caplog.text


def test_ingest_holds_the_write_lock(tmp_path, monkeypatch):
    """`writer.ingest()` 가 락을 잡는다 — 호출하는 쪽이 잊어버릴 수 없게."""
    from datetime import datetime

    from tybot.archive import writer

    monkeypatch.delenv("DATABASE_URL", raising=False)
    seen: list[bool] = []
    real = writer._ingest_locked

    def spy(*a, **kw):
        # ingest 본문이 도는 동안 락이 잡혀 있어야 한다
        try:
            FileLock(tmp_path / ".write.lock", label="확인").acquire()
            seen.append(False)
        except AlreadyRunning:
            seen.append(True)
        return real(*a, **kw)

    monkeypatch.setattr(writer, "_ingest_locked", spy)
    writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#테스트",
        messages=[
            writer.IncomingMessage(
                ts=datetime(2026, 8, 21, 9, 0, tzinfo=UTC), speaker="홍길동", text="확인"
            )
        ],
    )
    assert seen == [True]


def test_lock_survives_without_a_reference(tmp_path):
    """참조를 남기지 않아도 락이 유지된다.

    `instance_lock("bot").acquire()` 처럼 쓰면 락 객체가 가비지 컬렉션되면서 파일 핸들이
    닫히고 락이 조용히 풀린다. 그러면 이중 기동 차단이 그냥 통과한다.
    """
    import gc

    FileLock(tmp_path / "x.lock", label="참조 없음").acquire()
    gc.collect()
    with pytest.raises(AlreadyRunning):
        FileLock(tmp_path / "x.lock", label="확인").acquire()
