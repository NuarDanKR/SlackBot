"""Supervisor 운영 상태의 **저장소 경계** — SQL 은 전부 여기 있다.

표: `deploy/sql/archiver_supervisor_schema.sql`
판단: `supervisor_admin` · 순수 전이: `tybot.archive.supervisor_state`

## 왜 `archiving_repo` 에 안 붙이나

`archiving_repo` 는 **설정**을 든다(채널 모드, 보존, 기능 스위치). 여기는 **프로세스가
지금 무엇을 하고 있나**를 든다. 둘을 한 저장소에 섞으면 화면이 「사람이 정한 것」 과
「기계가 보고한 것」 을 같은 갱신으로 다루게 되고, 그때 사람이 끈 것이 worker 보고로
되살아난다.

## 여기서 하지 않는 것

- **판단하지 않는다.** `live` 거절·사유 필수·범위 검사는 `supervisor_admin` 몫이다
- **desired 와 observed 를 한 문장에 쓰지 않는다.** 콘솔은 desired 만, supervisor 는
  observed 만 쓴다. GRANT 도 열 단위로 그렇게 나뉘어 있다
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from .workspace_store import _connect

#: 화면 한 장이 드는 최근 작업 수. 더 많이 보여 줘도 사람은 위 몇 줄만 읽는다.
RECENT_JOBS = 20


class SupervisorRepo(Protocol):
    """저장소가 할 수 있는 일. 시험은 이걸 흉내 낸다."""

    def runtime(self, workspace: str) -> dict | None: ...
    def save_desired(self, workspace: str, mode: str, generation: int,
                     actor: str) -> None: ...
    def cursors(self, workspace: str) -> list[dict]: ...
    def jobs(self, workspace: str, limit: int) -> list[dict]: ...
    def job(self, job_id: int) -> dict | None: ...
    def create_job(self, row: dict) -> dict: ...
    def cancel_job(self, job_id: int) -> int: ...
    def transaction(self) -> Iterator[SupervisorRepo]: ...


class PostgresSupervisorRepo:
    """진짜 저장소. 잠금은 좌표를 아는 여기서 잡는다."""

    def __init__(self, connect=_connect, *, transaction_cursor=None) -> None:
        self._connect = connect
        self._transaction_cursor = transaction_cursor

    @contextmanager
    def transaction(self) -> Iterator[PostgresSupervisorRepo]:
        if self._transaction_cursor is not None:
            yield self
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield PostgresSupervisorRepo(self._connect, transaction_cursor=cur)

    @contextmanager
    def _cursor(self):
        if self._transaction_cursor is not None:
            yield self._transaction_cursor
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield cur

    # -- 워크스페이스 런타임 ------------------------------------------------
    def runtime(self, workspace: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT workspace, desired_mode, observed_state, generation,
                       heartbeat_at, last_event_at, last_write_at,
                       error_code, error_note, updated_at, updated_by
                  FROM archiver_workspace_runtime
                 WHERE workspace = %s
                """,
                (workspace,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def save_desired(self, workspace: str, mode: str, generation: int,
                     actor: str) -> None:
        """희망 상태만 쓴다. **관측 열은 건드리지 않는다.**

        `generation` 을 같이 올려야 supervisor 가 「새 설정이다」 를 안다. 시각으로
        비교하면 서버와 DB 시계가 다를 때 갱신을 놓치고, 그때 worker 는 옛 설정으로
        계속 돈다.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"archiver-desired:{workspace}",),
            )
            cur.execute(
                """
                INSERT INTO archiver_workspace_runtime
                       (workspace, desired_mode, generation, updated_at, updated_by)
                VALUES (%s, %s, %s, now(), %s)
                ON CONFLICT (workspace) DO UPDATE
                   SET desired_mode = EXCLUDED.desired_mode,
                       generation   = EXCLUDED.generation,
                       updated_at   = now(),
                       updated_by   = EXCLUDED.updated_by
                """,
                (workspace, mode, generation, actor),
            )

    # -- 채널 cursor ---------------------------------------------------------
    def cursors(self, workspace: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT channel_id, last_realtime_ts, last_history_ts,
                       last_success_at, retry_after, status, error_code, updated_at
                  FROM archive_channel_cursor
                 WHERE workspace = %s
                 ORDER BY channel_id
                """,
                (workspace,),
            )
            return [dict(row) for row in cur.fetchall()]

    # -- 소급 작업 -----------------------------------------------------------
    def jobs(self, workspace: str, limit: int = RECENT_JOBS) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace, channel_id, from_ts, to_ts, dry_run, state,
                       requested_by, reason, found_count, written_count,
                       duplicate_count, refused_count, failed_count, error_code,
                       started_at, finished_at, created_at
                  FROM archive_backfill_job
                 WHERE workspace = %s
                 ORDER BY created_at DESC, id DESC
                 LIMIT %s
                """,
                (workspace, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def job(self, job_id: int) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace, channel_id, from_ts, to_ts, dry_run, state,
                       requested_by, reason, found_count, written_count,
                       duplicate_count, refused_count, failed_count, error_code,
                       started_at, finished_at, created_at
                  FROM archive_backfill_job
                 WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def create_job(self, row: dict) -> dict:
        """작업을 **줄 세운다.** 콘솔은 실행하지 않는다.

        FastAPI 프로세스에서 Slack 을 읽거나 shell 을 부르지 않는다(§8). 여기서
        만드는 것은 요청이고, supervisor 가 집어간다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archive_backfill_job
                       (workspace, channel_id, from_ts, to_ts, dry_run,
                        state, requested_by, reason)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s)
             RETURNING id, workspace, channel_id, from_ts, to_ts, dry_run, state,
                       requested_by, reason, found_count, written_count,
                       duplicate_count, refused_count, failed_count, error_code,
                       started_at, finished_at, created_at
                """,
                (row["workspace"], row.get("channel_id", ""), row.get("from_ts", ""),
                 row.get("to_ts", ""), bool(row.get("dry_run", True)),
                 row["requested_by"], row["reason"]),
            )
            return dict(cur.fetchone())

    def cancel_job(self, job_id: int) -> int:
        """아직 시작하지 않은 작업만 취소한다.

        도는 작업을 콘솔이 `cancelled` 로 적으면, 실제로는 계속 쓰고 있는데 화면은
        멈춘 것으로 보인다. 그 상태에서 사람은 같은 범위를 한 번 더 건다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE archive_backfill_job
                   SET state = 'cancelled', finished_at = now()
                 WHERE id = %s AND state = 'queued'
                """,
                (job_id,),
            )
            return cur.rowcount


def default_repo() -> PostgresSupervisorRepo:
    return PostgresSupervisorRepo()
