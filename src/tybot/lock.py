"""프로세스 락 — 두 가지 사고를 막는다.

## 1. 봇 이중 기동 (`instance_lock`)
봇이 두 곳에서 뜨면 Slack 이벤트를 양쪽이 받는다. 같은 질문에 두 번 답하고 LLM 비용이 두 배가 된다.
사람이 실수로 두 번 띄우는 경우뿐 아니라, **관리 콘솔이 재기동 권한을 쥐면** 배포 중 이전 프로세스가
아직 살아 있는 채로 새 프로세스가 올라오는 경로가 생긴다. 그래서 콘솔 배포 기능보다 이 락이 먼저다.

## 2. 아카이브 동시 append (`archive_write_lock`)
`tybot.service`(실시간 수집)와 `tybot-collect.timer`(정기 백필)는 **서로 다른 프로세스**이면서
같은 MD 파일에 append 한다. 겹치면 라인이 섞이거나 프론트매터의 `doc_count` 갱신이 유실된다.
그래서 `writer.ingest()` 안에서 이 락을 잡는다 — 호출하는 쪽이 잊어버릴 수 없게.

## 백엔드 두 가지
| 백엔드 | 쓰는 때 | 막을 수 있는 범위 |
|---|---|---|
| 파일 락 (기본) | `DATABASE_URL` 이 없을 때 | **같은 서버** 안의 다른 프로세스 |
| PostgreSQL advisory 락 | `DATABASE_URL` 이 있을 때 | **여러 서버**에 걸친 다른 프로세스 |

지금은 봇과 콘솔이 한 서버에 있으므로 파일 락으로 충분하다. 서버를 늘리거나 콘솔을 분리하면
`DATABASE_URL` 만 넣으면 advisory 락으로 바뀐다.

## 크래시 안전
파일 락은 OS 가 프로세스 종료 시 해제한다. 프로세스가 kill -9 로 죽어도 다음 기동은 막히지 않는다
(직접 만든 pid 파일 방식은 이 성질이 없어서 쓰지 않는다).
"""
from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import tempfile
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tybot.lock")

# 윈도우에서 잠글 바이트 위치. 파일 앞부분(진단 정보)을 읽을 수 있게 멀리 둔다.
_WIN_LOCK_OFFSET = 1 << 30

# 확보한 락을 여기에 담아 둔다.
#
# 왜 필요한가: 파일 락은 **열어 둔 파일 핸들**이 락 그 자체다. 호출측이
# `instance_lock("bot").acquire()` 처럼 참조를 남기지 않으면 락 객체가 가비지 컬렉션되면서
# 핸들이 닫히고, 락이 조용히 풀린다. 그 상태로 봇을 또 띄우면 이중 기동이 그냥 통과한다.
# 프로세스 수명만큼 쥐는 것이 정상 동작이므로 여기에 붙잡아 둔다.
_HELD: set[object] = set()


class AlreadyRunning(RuntimeError):
    """같은 락을 이미 다른 프로세스가 쥐고 있다."""


class LockUnavailable(RuntimeError):
    """락 자체를 만들 수 없다(경로 권한, DB 접속 실패 등)."""


def _holder_line() -> str:
    return (
        f"pid={os.getpid()} host={socket.gethostname()} "
        f"started={datetime.now(UTC).astimezone().isoformat(timespec='seconds')} "
        f"argv={' '.join(sys.argv[:2])}"
    )


class FileLock:
    """파일 기반 배타 락. 같은 서버의 다른 프로세스를 막는다."""

    def __init__(self, path: Path | str, *, label: str) -> None:
        self.path = Path(path)
        self.label = label
        self._fh = None

    # --- 내부 -------------------------------------------------------------
    def _try_lock(self, fh) -> bool:
        """비블로킹으로 잠근다. 이미 잡혀 있으면 False."""
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(_WIN_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
            finally:
                fh.seek(0)
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False

    def _unlock(self, fh) -> None:
        if os.name == "nt":
            import msvcrt

            with contextlib.suppress(OSError):
                fh.seek(_WIN_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.seek(0)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def holder(self) -> str:
        """지금 락을 쥔 프로세스 정보. 오류 메시지에 넣어 사람이 찾을 수 있게 한다."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "정보 없음"
        first = text.strip().splitlines()[0] if text.strip() else ""
        return first or "정보 없음"

    # --- 공개 -------------------------------------------------------------
    def acquire(self, *, timeout: float = 0.0, poll: float = 0.05) -> None:
        """락을 잡는다.

        timeout=0 이면 한 번만 시도하고 실패 시 `AlreadyRunning`.
        timeout>0 이면 그 시간까지 기다린다(아카이브 쓰기처럼 '기다리면 되는' 경우).
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - 프로세스 수명만큼 연다
        except OSError as e:
            raise LockUnavailable(f"{self.label} 락 파일을 열 수 없습니다: {self.path} ({e})") from e

        deadline = time.monotonic() + timeout
        while True:
            if self._try_lock(fh):
                break
            if time.monotonic() >= deadline:
                held_by = self.holder()
                fh.close()
                raise AlreadyRunning(
                    f"{self.label} 락을 이미 다른 프로세스가 쥐고 있습니다 — {held_by} "
                    f"(락 파일: {self.path})"
                )
            time.sleep(poll)

        # 잠근 뒤에 누가 쥐었는지 남긴다. 다음 프로세스가 오류 메시지에서 이 줄을 읽는다.
        with contextlib.suppress(OSError):
            fh.seek(0)
            fh.truncate()
            fh.write(_holder_line() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._fh = fh
        _HELD.add(self)
        logger.info("%s 락 확보 — %s", self.label, self.path)

    def release(self) -> None:
        _HELD.discard(self)
        if self._fh is None:
            return
        self._unlock(self._fh)
        with contextlib.suppress(OSError):
            self._fh.close()
        self._fh = None
        logger.info("%s 락 해제", self.label)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class PostgresAdvisoryLock:
    """PostgreSQL advisory 락. 여러 서버에 걸친 중복 기동을 막는다.

    세션 단위 락이므로 **연결을 프로세스 수명만큼 열어 둔다.** 연결이 끊기면 락도 풀리는데,
    그게 맞는 동작이다 — 프로세스가 죽었는데 락이 남아 다음 기동을 막는 쪽이 더 나쁘다.
    """

    def __init__(self, dsn: str, *, label: str, name: str, connect=None) -> None:
        self.dsn = dsn
        self.label = label
        self.name = name
        # advisory 락 키는 정수다. 이름을 crc32 로 접어 쓴다(서로 다른 락끼리 겹치지 않게 라벨 포함).
        self.key = zlib.crc32(f"tybot:{name}".encode()) & 0x7FFFFFFF
        self._connect = connect  # 테스트에서 가짜 커넥션을 주입한다
        self._conn = None

    def _open(self):
        if self._connect is not None:
            return self._connect(self.dsn)
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover - 운영 환경에서만
            raise LockUnavailable(
                "DATABASE_URL 이 설정됐지만 psycopg 가 없습니다. "
                "pip install 'psycopg[binary]' 하거나 DATABASE_URL 을 비우세요."
            ) from e
        try:
            return psycopg.connect(self.dsn, autocommit=True)
        except Exception as e:
            raise LockUnavailable(f"{self.label} 락용 DB 접속 실패: {e}") from e

    def holder(self) -> str:
        """advisory 락은 보유자 정보를 담지 못한다. 세션 정보를 조회해 대신 알려 준다."""
        if self._conn is None:
            return "정보 없음"
        try:
            cur = self._conn.execute(
                "SELECT pid, application_name, backend_start FROM pg_stat_activity "
                "WHERE pid IN (SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid = %s AND granted)",
                (self.key,),
            )
            rows = cur.fetchall()
        except Exception:  # noqa: BLE001 - 진단 실패가 기동을 막지 않는다
            return "정보 없음"
        if not rows:
            return "정보 없음"
        return " / ".join(f"pid={r[0]} app={r[1]} since={r[2]}" for r in rows)

    def acquire(self, *, timeout: float = 0.0, poll: float = 0.1) -> None:
        conn = self._open()
        self._conn = conn
        deadline = time.monotonic() + timeout
        while True:
            cur = conn.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
            got = cur.fetchone()[0]
            if got:
                break
            if time.monotonic() >= deadline:
                held_by = self.holder()
                with contextlib.suppress(Exception):
                    conn.close()
                self._conn = None
                raise AlreadyRunning(
                    f"{self.label} 락을 이미 다른 프로세스가 쥐고 있습니다 — {held_by} "
                    f"(advisory key {self.key})"
                )
            time.sleep(poll)
        _HELD.add(self)
        logger.info("%s 락 확보 — advisory key %s", self.label, self.key)

    def release(self) -> None:
        _HELD.discard(self)
        if self._conn is None:
            return
        with contextlib.suppress(Exception):
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = None
        logger.info("%s 락 해제", self.label)

    def __enter__(self) -> PostgresAdvisoryLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


Lock = FileLock | PostgresAdvisoryLock


def _lock_dir() -> Path:
    """락 파일을 둘 곳. 아카이브·감사기록과 같은 상태 디렉터리 아래에 둔다.

    항상 **절대경로**다. 예전에는 `ARCHIVE_DIR` 이 비면 `./.locks` 로 떨어졌는데,
    운영 유닛은 코드 경로가 읽기 전용이라 그 순간 기동이 막혔다.
    """
    from .config import state_dir

    explicit = os.getenv("LOCK_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return state_dir() / ".locks"


def make_lock(name: str, *, label: str, dsn: str | None = None) -> Lock:
    """이름으로 락을 만든다. DSN 이 있으면 advisory 락, 없으면 파일 락."""
    dsn = dsn if dsn is not None else os.getenv("DATABASE_URL")
    if dsn:
        return PostgresAdvisoryLock(dsn, label=label, name=name)
    return FileLock(_resolve_lock_path(name), label=label)


def _resolve_lock_path(name: str) -> Path:
    """락 파일 경로를 정하되, 그 디렉터리를 못 만들면 임시 디렉터리로 물러난다.

    락을 못 잡아 **봇 전체가 안 뜨는 것**보다, 경고를 남기고 뜨는 쪽이 낫다.
    단일 인스턴스는 systemd 가 이미 보장하고, 이 락은 그 위의 추가 안전장치다.
    """
    primary = _lock_dir()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return primary / f"{name}.lock"
    except OSError as e:
        fallback = Path(tempfile.gettempdir()) / "tybot-locks"
        logger.warning(
            "락 디렉터리를 쓸 수 없어 임시 경로로 대체합니다: %s (%s) -> %s. "
            "운영에서는 STATE_DIR 또는 ARCHIVE_DIR 을 쓰기 가능한 경로로 지정하세요.",
            primary, e, fallback,
        )
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / f"{name}.lock"


def instance_lock(name: str = "bot", *, dsn: str | None = None) -> Lock:
    """프로세스 유일성 락. 기동 시 한 번 잡고 프로세스가 사는 동안 쥔다.

    `name` 을 나누는 이유: 봇(`bot`)과 정기 백필(`collect`)은 서로를 막아서는 안 된다.
    둘이 겹쳐도 되는 이유는 아카이브 쓰기를 `archive_write_lock` 으로 따로 직렬화하기 때문이다.
    """
    return make_lock(f"instance-{name}", label=f"{name} 단일 실행", dsn=dsn)


@contextlib.contextmanager
def archive_write_lock(archive_dir: Path | str, *, timeout: float = 20.0) -> Iterator[None]:
    """아카이브 append 를 직렬화한다. `writer.ingest()` 가 호출한다.

    실패해도 수집을 멈추지 않는다 — 락을 못 잡는 상황(권한·경로 문제)에서 원문을 버리는 쪽이
    더 나쁘다. 대신 경고를 남겨서 사람이 알 수 있게 한다.
    """
    dsn = os.getenv("DATABASE_URL")
    lock: Lock
    if dsn:
        lock = PostgresAdvisoryLock(dsn, label="아카이브 쓰기", name="archive-write")
    else:
        lock = FileLock(Path(archive_dir) / ".write.lock", label="아카이브 쓰기")
    try:
        lock.acquire(timeout=timeout)
    except (AlreadyRunning, LockUnavailable) as e:
        logger.warning("아카이브 쓰기 락 없이 진행합니다 — %s", e)
        yield
        return
    try:
        yield
    finally:
        lock.release()
