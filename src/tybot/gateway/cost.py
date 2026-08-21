"""비용 가드 — 일별 지출 상한을 강제한다.

## 왜 파일에 남기나
상한은 프로세스 메모리 카운터였다. 봇을 재시작하면 당일 누적이 0으로 돌아가서,
배포·크래시 루프가 있는 날에는 상한이 사실상 사라졌다. 그래서 **당일 누적을 파일에 남긴다**.

- 기록은 `{"day": "YYYY-MM-DD", "spent_usd": 1.234}` 한 줄. 사람이 열어 확인할 수 있다.
- 날짜가 바뀌면 자동으로 0에서 다시 시작한다.
- 파일을 못 읽거나 못 쓰면 **차단하지 않고 메모리 카운터로 동작한다** — 회계 파일 하나 때문에
  봇이 답을 못 하는 쪽이 더 나쁘다. 대신 경고를 남긴다.
- 프로세스가 여럿이면 이 파일도 정확하지 않다. 봇은 단일 인스턴스 전제이고,
  다중 인스턴스가 필요해지면 같은 인터페이스로 Redis 등을 붙인다.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("tybot.gateway.cost")


class CostLimitExceeded(RuntimeError):
    """일별 비용 상한 초과."""


class CostGuard:
    """일별 누적 비용 추적기. `state_path` 를 주면 재시작에도 누적이 유지된다."""

    def __init__(self, daily_limit_usd: float, *, state_path: Path | str | None = None) -> None:
        self._limit = daily_limit_usd
        self._lock = threading.Lock()
        self._state_path = Path(state_path) if state_path else None
        self._day = _dt.date.today()
        self._spent = 0.0
        if self._state_path:
            self._restore()

    # --- 영속화 -----------------------------------------------------------
    def _restore(self) -> None:
        """저장된 당일 누적을 읽는다. 날짜가 다르면 버린다."""
        assert self._state_path is not None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - 손상된 파일이 기동을 막지 않는다
            logger.warning("비용 상태 파일을 읽지 못했습니다(%s) — 0에서 시작합니다", e)
            return
        if str(data.get("day")) != self._day.isoformat():
            return  # 다른 날 기록 — 오늘 누적은 0
        try:
            self._spent = max(0.0, float(data.get("spent_usd") or 0.0))
        except (TypeError, ValueError):
            logger.warning("비용 상태 파일의 spent_usd 값이 이상합니다 — 0에서 시작합니다")
            return
        logger.info("당일 누적 비용 복원: $%.4f (%s)", self._spent, self._state_path)

    def _persist(self) -> None:
        """호출측이 락을 잡은 상태에서 부른다."""
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"day": self._day.isoformat(), "spent_usd": round(self._spent, 6)}),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)  # 원자적 교체 — 중간 상태를 남기지 않는다
        except Exception as e:  # noqa: BLE001 - 기록 실패로 답변을 막지 않는다
            logger.warning("비용 상태 기록 실패(%s) — 메모리 카운터로 계속합니다", e)

    # --- 상한 판정 --------------------------------------------------------
    def _rollover(self) -> None:
        today = _dt.date.today()
        if today != self._day:
            self._day = today
            self._spent = 0.0
            self._persist()

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
            self._persist()
