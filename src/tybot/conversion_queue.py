"""첨부 변환 재처리 큐 — claim/lease/backoff/circuit breaker (B-45 §5).

설계: [`docs/design/operational-warning-recovery-and-answer-progress.md`](../../docs/design/operational-warning-recovery-and-answer-progress.md)

## 왜 DB 인가

재처리는 **프로세스보다 오래 산다.** 변환기가 죽어 있는 30분 동안 재시도를
메모리에 들고 있으면 재시작 한 번에 통째로 사라지고, 그 사실은 아무 데도 안 남는다.
지금까지 실패한 첨부가 조용히 잊혔던 것이 정확히 그 모양이다.

봇이 여러 워크스페이스로 도는 것도 이유다. 두 프로세스가 같은 파일을 동시에
변환하면 산출물이 서로를 덮어쓴다. **claim/lease** 로 한 번에 하나만 잡는다.

## 담지 않는 것

payload 에 **원문도 토큰도 넣지 않는다.** 큐에는 좌표(`workspace`/`channel_id`/
`file_id`/`sha256`)와 코드만 있다. 워커는 그 좌표로 고정된 경로를 열어 읽는다 —
큐가 유출되어도 업무 내용이 따라 나가지 않는다.

## 자동으로 되풀이하지 않는 것

`pii_refused` 는 **정책 제외**다. 기술 실패가 아니라서 재시도가 의미 없고, 자동
우회를 만들면 사람이 막은 것을 기계가 푸는 길이 된다. `encrypted`·`corrupt`·
`unsupported` 도 같은 파일을 몇 번 돌려도 결과가 같다.

`converter_missing`·`converter_policy_denied` 는 **환경** 문제다. 파일마다
되풀이하면 같은 오류가 수천 줄 쌓이고 정작 고쳐야 할 한 줄이 묻힌다. 그래서
`held` 로 두고, 환경이 복구되면 사람이 푼다.
"""
from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("tybot.conversion_queue")

# 이 코드로 만든 산출물이 어느 파이프라인의 것인가. 변환기나 추출 규칙이 바뀌면
# 올린다 — **키의 일부라서**, 올리면 과거에 실패한 파일이 새 작업으로 다시 들어온다.
PIPELINE_VERSION = "1"

# 상태. `held` 는 「사람이 볼 때까지 멈춘다」 이고 `failed` 와 다르다 —
# 하나는 환경을 고쳐야 하고 하나는 그 파일을 포기한 것이다.
QUEUED = "queued"
LEASED = "leased"
SUCCEEDED = "succeeded"
FAILED = "failed"
HELD = "held"
STATES = (QUEUED, LEASED, SUCCEEDED, FAILED, HELD)

# 최초 시도를 포함한 상한. 넘으면 `failed` 로 닫고 사람에게 넘긴다.
MAX_ATTEMPTS = 4
# 1분 → 5분 → 30분. 지수로 늘리지 않는다 — 변환 장애는 대개 몇 분 안에 복구되거나
# 사람 손이 필요하고, 그 사이 값은 의미가 없다.
BACKOFF_SECONDS = (60, 300, 1800)
# 한 작업을 잡고 있을 수 있는 시간. 넘으면 다른 프로세스가 회수한다.
# 변환기 자체 상한(`external_convert.COMMAND_TIMEOUT` = 120초)보다 넉넉해야
# **정상 작업을 뺏지 않는다.**
LEASE_SECONDS = 600

# 자동으로 되풀이하지 않는 코드. 되풀이해도 결과가 같거나, 사람이 막은 것이다.
TERMINAL_CODES = frozenset({
    "pii_refused",
    "encrypted",
    "corrupt",
    "unsupported",
    "empty_output",
    "original_changed",
})
# 환경이 복구될 때까지 멈추는 코드. 파일 문제가 아니라 **서버 문제**다.
HOLD_CODES = frozenset({
    "converter_missing",
    "converter_policy_denied",
})

# 사람이 명시적으로 요청해도 큐에 넣지 않는 코드.
#
# `pii_refused` 하나다. **정책 제외를 기술 실패처럼 푸는 길을 만들지 않는다** —
# 버튼 하나로 우회되면 그 검사는 더 이상 검사가 아니다. 오탐이라면 검사 규칙을
# 고치거나 사람이 원본을 직접 확인하는 것이 맞다.
FORCE_BLOCKED = frozenset({"pii_refused"})

# 같은 변환기·버전이 연달아 이만큼 실패하면 회로를 연다.
BREAKER_THRESHOLD = 5
# 열린 회로가 스스로 닫히지는 않는다. 다만 이 시간이 지나면 **한 건만** 흘려
# 보내 확인한다(half-open). 안 그러면 사람이 고쳐도 아무도 모른다.
BREAKER_PROBE_SECONDS = 900

BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"


class QueueUnavailable(RuntimeError):
    """큐를 쓸 수 없다. **재처리를 못 하는 것이지 수집이 멈추는 것은 아니다.**"""


# --- 정책 (순수 함수) ---------------------------------------------------------
#
# DB 없이 검사할 수 있게 떼어 둔다. 재시도 정책은 운영에서 가장 자주 손대는
# 부분이고, 손댈 때마다 PostgreSQL 이 있어야 한다면 아무도 검사하지 않는다.


def is_retryable(error_code: str, retryable_flag: bool) -> bool:
    """이 실패를 자동으로 다시 시도해도 되는가.

    **코드가 먼저다.** 호출부가 넘긴 `retryable` 플래그는 참고값이고, 되풀이하면
    안 되는 코드는 플래그가 참이어도 막는다 — 한 곳에서 실수하면 그 파일이
    네 번씩 같은 실패를 반복한다.
    """
    code = (error_code or "").strip()
    if code in TERMINAL_CODES or code in HOLD_CODES:
        return False
    return bool(retryable_flag)


def next_state(error_code: str, retryable_flag: bool, attempt_count: int) -> str:
    """이 실패 뒤 작업이 놓일 자리."""
    code = (error_code or "").strip()
    if code in HOLD_CODES:
        return HELD
    if not is_retryable(code, retryable_flag):
        return FAILED
    if attempt_count >= MAX_ATTEMPTS:
        return FAILED
    return QUEUED


def backoff_seconds(attempt_count: int, *, jitter: float | None = None) -> int:
    """다음 시도까지 기다릴 초.

    `attempt_count` 는 **이미 끝난** 시도 수다. 지터를 섞는 이유는, 한 번에 실패한
    파일 수백 개가 같은 초에 다시 몰리면 그 순간이 두 번째 장애가 되기 때문이다.
    """
    index = max(0, min(attempt_count - 1, len(BACKOFF_SECONDS) - 1))
    base = BACKOFF_SECONDS[index]
    spread = jitter if jitter is not None else random.uniform(0, 0.25)
    return int(base * (1 + max(0.0, min(spread, 1.0))))


def breaker_allows(state: str, opened_at, *, now=None) -> bool:
    """회로가 열려 있어도 가끔 한 건은 통과시킨다(half-open).

    안 그러면 사람이 변환기를 고쳐도 큐가 스스로 회복하지 않고, 누군가 콘솔에서
    풀어 줄 때까지 모든 파일이 멈춘 채로 남는다.
    """
    if state != BREAKER_OPEN:
        return True
    if opened_at is None:
        return False
    now = now or datetime.now(UTC)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    return (now - opened_at) >= timedelta(seconds=BREAKER_PROBE_SECONDS)


@dataclass(frozen=True)
class Job:
    """큐에 든 작업 하나. **업무 내용은 없다** — 좌표와 코드뿐이다."""

    id: int
    workspace: str
    channel_id: str
    file_id: str
    original_sha256: str
    pipeline_version: str
    state: str
    attempt_count: int
    error_code: str = ""
    converter: str = ""
    converter_version: str = ""
    next_attempt_at: datetime | None = None

    def log_line(self) -> str:
        return (
            f"job={self.id} ws={self.workspace} ch={self.channel_id} "
            f"file={self.file_id} state={self.state} attempt={self.attempt_count} "
            f"code={self.error_code or '-'}"
        )


# --- DB ----------------------------------------------------------------------


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise QueueUnavailable("DATABASE_URL 이 없습니다")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 배포 환경에는 있다
        raise QueueUnavailable("psycopg 가 설치되지 않았습니다") from exc
    try:
        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise QueueUnavailable(str(exc)) from exc


def _job(row: dict) -> Job:
    return Job(
        id=int(row["id"]),
        workspace=str(row["workspace"]),
        channel_id=str(row["channel_id"]),
        file_id=str(row["file_id"]),
        original_sha256=str(row.get("original_sha256") or ""),
        pipeline_version=str(row.get("pipeline_version") or PIPELINE_VERSION),
        state=str(row["state"]),
        attempt_count=int(row.get("attempt_count") or 0),
        error_code=str(row.get("error_code") or ""),
        converter=str(row.get("converter") or ""),
        converter_version=str(row.get("converter_version") or ""),
        next_attempt_at=row.get("next_attempt_at"),
    )


def enqueue(
    *,
    workspace: str,
    channel_id: str,
    file_id: str,
    original_sha256: str = "",
    error_code: str = "",
    retryable: bool = False,
    converter: str = "",
    converter_version: str = "",
    pipeline_version: str = PIPELINE_VERSION,
    force: bool = False,
) -> int | None:
    """실패한 변환을 큐에 올린다. 되풀이하면 안 되는 것은 올리지 않는다.

    같은 좌표가 이미 있으면 **새 행을 만들지 않는다.** 한 파일이 여러 번 실패할
    때마다 행이 늘면, 큐 길이가 장애 규모가 아니라 재시도 횟수를 뜻하게 된다.

    `force` 는 **사람이 명시적으로 요청한 재처리**다. 자동 판정이 「되풀이해도
    소용없다」 고 본 것도 넣는다 — 그 판정의 근거는 그때의 변환기였고, 변환기를
    고친 뒤에는 결과가 달라질 수 있기 때문이다. 자동 경로는 이것을 쓰지 않는다.

    `force` 로도 `pii_refused` 는 넣지 않는다(`FORCE_BLOCKED`). 정책 제외를
    버튼 하나로 푸는 길을 만들면 그 검사는 더 이상 검사가 아니다.

    돌려주는 값은 작업 ID. 큐에 올리지 않았으면 `None`.
    """
    if (error_code or "").strip() in FORCE_BLOCKED:
        log.info("정책 제외라 큐에 올리지 않는다 code=%s file=%s", error_code, file_id)
        return None
    if not force and not is_retryable(error_code, retryable):
        log.info(
            "재시도 대상이 아니라 큐에 올리지 않는다 code=%s file=%s",
            error_code or "-", file_id,
        )
        return None
    now = datetime.now(UTC)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversion_job (
                workspace, channel_id, file_id, original_sha256, pipeline_version,
                state, error_code, converter, converter_version, next_attempt_at
            )
            VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s)
            ON CONFLICT (workspace, channel_id, file_id, original_sha256, pipeline_version)
            DO UPDATE SET
                -- 이미 끝난 작업은 되살리지 않는다. 성공한 파일을 다시 큐에
                -- 넣으면 멀쩡한 산출물을 다시 덮어쓸 위험이 생긴다.
                state = CASE WHEN conversion_job.state IN ('succeeded')
                             THEN conversion_job.state ELSE 'queued' END,
                error_code = EXCLUDED.error_code,
                updated_at = now()
            RETURNING id
            """,
            (
                workspace, channel_id, file_id, original_sha256, pipeline_version,
                error_code, converter, converter_version, now,
            ),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def open_breakers(*, now=None) -> set[tuple[str, str]]:
    """지금 통과시키면 안 되는 `(변환기, 버전)`.

    **회로를 기록만 하고 읽지 않으면 아무 일도 하지 않는다.** 그 상태로 두면
    표만 늘고 같은 환경 오류가 계속 쌓인다 — 만들고 안 잇는 것이 우리가 가장
    자주 겪은 고장이다.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM conversion_breaker WHERE state = 'open'")
        rows = cur.fetchall()
    return {
        (str(row["converter"]), str(row.get("converter_version") or ""))
        for row in rows
        if not breaker_allows(str(row["state"]), row.get("opened_at"), now=now)
    }


def claim(owner: str, *, limit: int = 5, lease_seconds: int = LEASE_SECONDS) -> list[Job]:
    """실행할 작업을 잡는다. **한 번에 하나의 프로세스만 잡는다.**

    `FOR UPDATE SKIP LOCKED` 로 같은 행을 두 프로세스가 집지 않게 한다. 락이 아니라
    임대(lease)라서, 잡은 프로세스가 죽어도 `reclaim_expired()` 가 되찾는다 —
    죽은 프로세스의 작업이 영원히 멈춰 있는 것이 큐의 가장 흔한 고장이다.
    """
    if not owner.strip():
        raise ValueError("작업을 잡는 주체를 남기지 않으면 회수할 때 누구 것인지 모른다")
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=lease_seconds)
    # **열린 회로의 변환기는 건너뛴다.** 환경이 고장났는데 계속 집으면 시도 횟수만
    # 갉아먹고, 정작 고친 뒤에는 남은 횟수가 없어 그 파일이 영영 `failed` 로 닫힌다.
    #
    # `half-open` 시간이 지난 회로는 여기에 없다 — 한 건은 흘려 보내 확인한다.
    try:
        blocked = open_breakers(now=now)
    except QueueUnavailable:
        blocked = set()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH picked AS (
                SELECT id FROM conversion_job
                 WHERE state = 'queued' AND next_attempt_at <= %s
                 ORDER BY next_attempt_at
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE conversion_job j
               SET state = 'leased', lease_owner = %s, lease_expires_at = %s,
                   attempt_count = j.attempt_count + 1, updated_at = now()
              FROM picked
             WHERE j.id = picked.id
            RETURNING j.*
            """,
            (now, max(1, limit), owner.strip(), expires),
        )
        jobs = [_job(row) for row in cur.fetchall()]
        held_back = [j for j in jobs if (j.converter, j.converter_version) in blocked]
        if held_back:
            # 잡았지만 돌리지 않는다. **바로 되돌린다** — 임대만 잡아 두면 그
            # 시간만큼 다른 프로세스도 못 집는다.
            cur.execute(
                """
                UPDATE conversion_job
                   SET state = 'queued', lease_owner = '', lease_expires_at = NULL,
                       attempt_count = GREATEST(attempt_count - 1, 0),
                       next_attempt_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE id = ANY(%s)
                """,
                (BREAKER_PROBE_SECONDS, [j.id for j in held_back]),
            )
            log.warning(
                "회로가 열려 %d건을 되돌린다 converters=%s",
                len(held_back), sorted({j.converter for j in held_back}),
            )
        conn.commit()
    return [j for j in jobs if (j.converter, j.converter_version) not in blocked]


def reclaim_expired() -> int:
    """임대가 끝난 작업을 되돌린다. 재시작 뒤 첫 할 일이다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversion_job
               SET state = 'queued', lease_owner = '', lease_expires_at = NULL,
                   updated_at = now()
             WHERE state = 'leased' AND lease_expires_at < now()
            """
        )
        count = cur.rowcount
        conn.commit()
    if count:
        log.warning("임대 만료 작업 %d건을 회수했다", count)
    return count


def succeed(job_id: int) -> None:
    """변환에 성공했다. 회로도 함께 닫는다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversion_job
               SET state = 'succeeded', error_code = '', lease_owner = '',
                   lease_expires_at = NULL, finished_at = now(), updated_at = now()
             WHERE id = %s
            RETURNING converter, converter_version
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row and (row.get("converter") or ""):
            _close_breaker(cur, str(row["converter"]), str(row.get("converter_version") or ""))
        conn.commit()


def fail(job_id: int, *, error_code: str, retryable: bool = False) -> str:
    """변환에 실패했다. 다음 자리와 다음 시각을 정한다.

    돌려주는 값은 **놓인 상태**다. 호출부가 그것으로 사람에게 알릴지 정한다 —
    첫 일시 실패는 알림이 아니라 기록이고, `held` 는 알림이다.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM conversion_job WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise QueueUnavailable(f"작업을 찾지 못했습니다: {job_id}")
        attempts = int(row.get("attempt_count") or 0)
        state = next_state(error_code, retryable, attempts)
        delay = backoff_seconds(attempts) if state == QUEUED else 0
        cur.execute(
            """
            UPDATE conversion_job
               SET state = %s, error_code = %s, lease_owner = '',
                   lease_expires_at = NULL,
                   next_attempt_at = now() + make_interval(secs => %s),
                   finished_at = CASE WHEN %s IN ('failed', 'held') THEN now() END,
                   updated_at = now()
             WHERE id = %s
            """,
            (state, error_code, delay, state, job_id),
        )
        converter = str(row.get("converter") or "")
        if converter and error_code in HOLD_CODES | {"converter_crashed", "converter_timeout"}:
            _trip_breaker(cur, converter, str(row.get("converter_version") or ""), error_code)
        conn.commit()
    log.info("작업 실패 job=%s code=%s -> %s", job_id, error_code, state)
    return state


def _trip_breaker(cur, converter: str, version: str, error_code: str) -> None:
    """같은 변환기가 연달아 실패하면 회로를 연다.

    파일이 아니라 **환경**이 문제일 때, 파일마다 되풀이하면 같은 오류가 수천 줄
    쌓이고 정작 고쳐야 할 한 줄이 묻힌다.
    """
    cur.execute(
        """
        INSERT INTO conversion_breaker (converter, converter_version, failure_count,
                                        state, reason_code, opened_at, updated_at)
        VALUES (%s, %s, 1, 'closed', %s, NULL, now())
        ON CONFLICT (converter, converter_version) DO UPDATE SET
            failure_count = conversion_breaker.failure_count + 1,
            reason_code = EXCLUDED.reason_code,
            state = CASE WHEN conversion_breaker.failure_count + 1 >= %s
                         THEN 'open' ELSE conversion_breaker.state END,
            opened_at = CASE WHEN conversion_breaker.failure_count + 1 >= %s
                             AND conversion_breaker.opened_at IS NULL
                             THEN now() ELSE conversion_breaker.opened_at END,
            updated_at = now()
        """,
        (converter, version, error_code, BREAKER_THRESHOLD, BREAKER_THRESHOLD),
    )


def _close_breaker(cur, converter: str, version: str) -> None:
    cur.execute(
        """
        UPDATE conversion_breaker
           SET failure_count = 0, state = 'closed', opened_at = NULL,
               reason_code = '', updated_at = now()
         WHERE converter = %s AND converter_version = %s
        """,
        (converter, version),
    )


def breaker(converter: str, version: str = "") -> dict | None:
    """이 변환기의 회로 상태. 없으면 `None`(=한 번도 실패한 적 없다)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM conversion_breaker WHERE converter = %s AND converter_version = %s",
            (converter, version),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def pending_for(workspace: str, channel_id: str, file_id: str) -> dict | None:
    """이 첨부의 재처리 상태. 콘솔의 「다음 재시도」 가 읽는다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, attempt_count, error_code, next_attempt_at, finished_at
              FROM conversion_job
             WHERE workspace = %s AND channel_id = %s AND file_id = %s
             ORDER BY updated_at DESC LIMIT 1
            """,
            (workspace, channel_id, file_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def summary() -> dict[str, int]:
    """상태별 건수. 운영 리포트와 콘솔이 읽는다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT state, count(*) AS n FROM conversion_job GROUP BY state")
        rows = cur.fetchall()
    return {str(row["state"]): int(row["n"]) for row in rows}
