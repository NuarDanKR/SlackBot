"""비용 가드 — 일별 지출 상한을 강제한다."""
from __future__ import annotations

import datetime as _dt
import threading


class CostLimitExceeded(RuntimeError):
    """일별 비용 상한 초과."""


class CostGuard:
    """프로세스 내 일별 누적 비용 추적기.

    다중 인스턴스/영속성이 필요하면 동일 인터페이스로 Redis 등 백엔드를 붙인다.
    """

    def __init__(self, daily_limit_usd: float) -> None:
        self._limit = daily_limit_usd
        self._lock = threading.Lock()
        self._day = _dt.date.today()
        self._spent = 0.0

    def _rollover(self) -> None:
        today = _dt.date.today()
        if today != self._day:
            self._day = today
            self._spent = 0.0

    @property
    def spent_today(self) -> float:
        with self._lock:
            self._rollover()
            return self._spent

    def check(self, estimated_usd: float = 0.0) -> None:
        """호출 전 예상 비용을 더했을 때 상한을 넘으면 차단."""
        with self._lock:
            self._rollover()
            if self._spent + estimated_usd > self._limit:
                raise CostLimitExceeded(
                    f"일별 비용 상한 초과: 사용 ${self._spent:.2f} + 예상 ${estimated_usd:.2f}"
                    f" > 한도 ${self._limit:.2f}"
                )

    def record(self, usd: float) -> None:
        with self._lock:
            self._rollover()
            self._spent += usd
