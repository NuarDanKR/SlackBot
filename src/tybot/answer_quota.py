"""Per-user successful-answer quota for the shared TYBot/VIPBot runtime.

Reservations happen before model work so concurrent requests cannot all pass the
limit. Only a successfully delivered grounded answer is finalized; every other
outcome releases the reservation.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger("tybot.answer_quota")

KST = ZoneInfo("Asia/Seoul")
DEFAULT_LIMIT = 10
RESERVATION_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    used: int
    limit: int
    reset_at: datetime
    reservation_id: str = ""
    duplicate: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class QuotaStore(Protocol):
    def reserve(
        self,
        *,
        day: str,
        workspace: str,
        bot_name: str,
        slack_user: str,
        event_key: str,
        limit: int,
        now: datetime,
    ) -> tuple[bool, int, str, bool]: ...

    def finalize(self, reservation_id: str, *, now: datetime) -> None: ...

    def release(self, reservation_id: str) -> None: ...


@dataclass
class _Reservation:
    reservation_id: str
    day: str
    workspace: str
    bot_name: str
    slack_user: str
    event_key: str
    state: str
    reserved_at: datetime


class MemoryQuotaStore:
    """Thread-safe fallback and deterministic unit-test store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, _Reservation] = {}

    def reserve(
        self,
        *,
        day: str,
        workspace: str,
        bot_name: str,
        slack_user: str,
        event_key: str,
        limit: int,
        now: datetime,
    ) -> tuple[bool, int, str, bool]:
        with self._lock:
            self._rows = {
                key: row
                for key, row in self._rows.items()
                if row.day == day
                and (
                    row.state == "counted"
                    or now - row.reserved_at <= RESERVATION_TTL
                )
            }
            duplicate = next(
                (
                    row
                    for row in self._rows.values()
                    if row.day == day
                    and row.workspace == workspace
                    and row.bot_name == bot_name
                    and row.event_key == event_key
                ),
                None,
            )
            used = self._used(day, workspace, bot_name, slack_user)
            if duplicate is not None:
                return False, used, "", True
            if used >= limit:
                return False, used, "", False
            reservation_id = uuid.uuid4().hex
            self._rows[reservation_id] = _Reservation(
                reservation_id,
                day,
                workspace,
                bot_name,
                slack_user,
                event_key,
                "reserved",
                now,
            )
            return True, used + 1, reservation_id, False

    def _used(self, day: str, workspace: str, bot_name: str, slack_user: str) -> int:
        return sum(
            row.day == day
            and row.workspace == workspace
            and row.bot_name == bot_name
            and row.slack_user == slack_user
            and row.state in ("reserved", "counted")
            for row in self._rows.values()
        )

    def finalize(self, reservation_id: str, *, now: datetime) -> None:
        del now
        with self._lock:
            row = self._rows.get(reservation_id)
            if row is not None:
                row.state = "counted"

    def release(self, reservation_id: str) -> None:
        with self._lock:
            row = self._rows.get(reservation_id)
            if row is not None and row.state == "reserved":
                self._rows.pop(reservation_id, None)


class PostgresQuotaStore:
    """Transactionally reserve one of the user's daily answer slots."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):
        import psycopg

        return psycopg.connect(self._dsn, connect_timeout=3)

    def reserve(
        self,
        *,
        day: str,
        workspace: str,
        bot_name: str,
        slack_user: str,
        event_key: str,
        limit: int,
        now: datetime,
    ) -> tuple[bool, int, str, bool]:
        lock_key = f"answer-quota|{day}|{workspace}|{bot_name}|{slack_user}"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            cur.execute(
                """
                DELETE FROM answer_quota_reservation
                 WHERE state = 'reserved' AND reserved_at < %s
                """,
                (now - RESERVATION_TTL,),
            )
            cur.execute(
                "DELETE FROM answer_quota_reservation WHERE day < %s::date - 35",
                (day,),
            )
            cur.execute(
                """
                SELECT 1 FROM answer_quota_reservation
                 WHERE day = %s AND workspace = %s AND bot_name = %s AND event_key = %s
                """,
                (day, workspace, bot_name, event_key),
            )
            duplicate = cur.fetchone() is not None
            cur.execute(
                """
                SELECT count(*) FROM answer_quota_reservation
                 WHERE day = %s AND workspace = %s AND bot_name = %s
                   AND slack_user = %s AND state IN ('reserved', 'counted')
                """,
                (day, workspace, bot_name, slack_user),
            )
            used = int(cur.fetchone()[0])
            if duplicate:
                return False, used, "", True
            if used >= limit:
                return False, used, "", False
            reservation_id = uuid.uuid4().hex
            cur.execute(
                """
                INSERT INTO answer_quota_reservation
                    (reservation_id, day, workspace, bot_name, slack_user,
                     event_key, state, reserved_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'reserved', %s)
                """,
                (
                    reservation_id,
                    day,
                    workspace,
                    bot_name,
                    slack_user,
                    event_key,
                    now,
                ),
            )
            return True, used + 1, reservation_id, False

    def finalize(self, reservation_id: str, *, now: datetime) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE answer_quota_reservation
                   SET state = 'counted', finalized_at = %s
                 WHERE reservation_id = %s AND state = 'reserved'
                """,
                (now, reservation_id),
            )

    def release(self, reservation_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM answer_quota_reservation
                 WHERE reservation_id = %s AND state = 'reserved'
                """,
                (reservation_id,),
            )


class AnswerQuota:
    def __init__(self, store: QuotaStore, *, limit: int = DEFAULT_LIMIT) -> None:
        if limit < 1:
            raise ValueError("answer quota limit must be positive")
        self._store = store
        self._fallback = MemoryQuotaStore()
        self._fallback_ids: set[str] = set()
        self._fallback_lock = threading.Lock()
        self.limit = limit

    @classmethod
    def from_env(cls) -> AnswerQuota:
        dsn = str(os.getenv("DATABASE_URL") or "").strip()
        store: QuotaStore = PostgresQuotaStore(dsn) if dsn else MemoryQuotaStore()
        return cls(store, limit=DEFAULT_LIMIT)

    def reserve(
        self,
        *,
        workspace: str,
        bot_name: str,
        slack_user: str,
        event_key: str,
        now: datetime | None = None,
    ) -> QuotaDecision:
        current = (now or datetime.now(UTC)).astimezone(KST)
        day = current.date().isoformat()
        reset_at = datetime.combine(current.date() + timedelta(days=1), time(), KST)
        params = {
            "day": day,
            "workspace": workspace,
            "bot_name": bot_name,
            "slack_user": slack_user,
            "event_key": event_key,
            "limit": self.limit,
            "now": current,
        }
        try:
            allowed, used, reservation_id, duplicate = self._store.reserve(**params)
        except Exception as exc:  # noqa: BLE001 - local limiter remains available
            log.warning("답변 횟수 DB 원장 실패, 프로세스 원장 사용 err=%s", type(exc).__name__)
            allowed, used, reservation_id, duplicate = self._fallback.reserve(**params)
            if reservation_id:
                with self._fallback_lock:
                    self._fallback_ids.add(reservation_id)
        return QuotaDecision(
            allowed,
            used,
            self.limit,
            reset_at,
            reservation_id,
            duplicate,
        )

    def finalize(self, reservation_id: str, *, now: datetime | None = None) -> None:
        if not reservation_id:
            return
        current = (now or datetime.now(UTC)).astimezone(KST)
        with self._fallback_lock:
            fallback = reservation_id in self._fallback_ids
            self._fallback_ids.discard(reservation_id)
        if fallback:
            self._fallback.finalize(reservation_id, now=current)
            return
        try:
            self._store.finalize(reservation_id, now=current)
        except Exception as exc:  # noqa: BLE001 - do not break an already delivered answer
            log.warning("답변 횟수 확정 실패 err=%s", type(exc).__name__)
            self._fallback.finalize(reservation_id, now=current)

    def release(self, reservation_id: str) -> None:
        if not reservation_id:
            return
        with self._fallback_lock:
            fallback = reservation_id in self._fallback_ids
            self._fallback_ids.discard(reservation_id)
        if fallback:
            self._fallback.release(reservation_id)
            return
        try:
            self._store.release(reservation_id)
        except Exception as exc:  # noqa: BLE001 - quota cleanup must not break the bot
            log.warning("답변 횟수 예약 환급 실패 err=%s", type(exc).__name__)
            self._fallback.release(reservation_id)


def limit_message(decision: QuotaDecision, bot_name: str) -> str:
    display = "VIPBot" if bot_name.strip().lower() == "vipbot" else "TYBot"
    reset = decision.reset_at.strftime("%m월 %d일 00:00")
    return (
        f"오늘 {display}의 정상 답변 {decision.limit}회를 모두 사용했습니다. "
        f"{reset}(KST)에 다시 사용할 수 있습니다. "
        "오류로 끝나거나 답변이 전달되지 않은 요청은 횟수에 포함되지 않습니다."
    )


__all__ = [
    "AnswerQuota",
    "MemoryQuotaStore",
    "PostgresQuotaStore",
    "QuotaDecision",
    "limit_message",
]
