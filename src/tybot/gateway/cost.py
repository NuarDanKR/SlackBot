"""비용 가드 — 일별 지출 상한을 강제한다.

## 왜 파일에 남기나
상한은 프로세스 메모리 카운터였다. 봇을 재시작하면 당일 누적이 0으로 돌아가서,
배포·크래시 루프가 있는 날에는 상한이 사실상 사라졌다. 그래서 **당일 누적을 파일에 남긴다**.

- 기록은 `{"day": "YYYY-MM-DD", "spent_usd": 1.234, "by_workspace": {...}}` 한 줄.
  사람이 열어 확인할 수 있다.
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger("tybot.gateway.cost")


class CostLimitExceeded(RuntimeError):
    """일별 비용 상한 초과."""


# --- 누가 쓰는가 -----------------------------------------------------------
# LLM 호출은 답변 한 건 안에서 여러 겹으로 일어난다 — 의도 분류, 전문 봇 라우팅,
# 본 답변, 문장 다듬기. 이 중 어느 것도 워크스페이스를 인자로 들고 다니지 않는다.
# 열 군데 넘는 호출 자리에 인자를 끼우면 **한 군데만 빠져도 그 비용은 조용히
# 아무에게도 안 달린다** — 그리고 그건 화면에 정상으로 보인다.
#
# 그래서 요청 단위로 한 번 세우고 안쪽에서는 읽기만 한다. 요청 ID 와 같은 성격이다.
_current_workspace: ContextVar[str] = ContextVar("tybot_cost_workspace", default="")


def current_workspace() -> str:
    """지금 비용이 달릴 워크스페이스. 안 세워졌으면 빈 문자열."""
    return _current_workspace.get()


@contextmanager
def attribute_to(workspace: str) -> Iterator[None]:
    """이 블록 안의 LLM 비용을 이 워크스페이스에 단다.

    **반드시 되돌린다.** Slack 이벤트는 스레드 풀에서 돌고, 스레드는 재사용된다 —
    되돌리지 않으면 다음 이벤트가 앞 이벤트의 워크스페이스를 물려받아 엉뚱한 곳의
    한도를 깎는다.
    """
    token = _current_workspace.set((workspace or "").strip().lower())
    try:
        yield
    finally:
        _current_workspace.reset(token)


class CostGuard:
    """일별 누적 비용 추적기. **지갑은 하나, 칸은 여럿이다.**

    ## 왜 두 겹인가
    결제는 한 계정에서 나간다 — 그래서 전체 한도가 바깥 테두리다. 그런데 한 지갑을
    그냥 공유하면 **한 워크스페이스가 다 쓰고 나머지가 굶는다.** 2026-09-15 에 실제로
    그랬다: 경영본부가 답을 못 받는데 원인은 다른 워크스페이스의 사용액이었고, 화면
    어디에도 그 사실이 없었다.

    그래서 안쪽에 워크스페이스별 칸을 둔다. 둘 중 **먼저 닿는 쪽**이 막고, 오류
    메시지는 **어느 쪽에 걸렸는지** 말한다 — 그걸 모르면 사람은 엉뚱한 한도를 올린다
    (그날 콘솔에서 워크스페이스 상한을 올렸는데 아무 일도 일어나지 않았다).

    ## 한도는 밖에서 온다
    이 클래스는 DB 를 모른다. `workspace_limits` 로 `{키: 한도}` 를 돌려주는 함수를
    받는다 — 그래야 콘솔에서 바꾼 값이 **재시작 없이** 반영되고, 이 파일은 DB 없이
    시험된다.

    한도를 못 읽으면 **막지 않는다.** DB 가 잠깐 죽었다고 전 워크스페이스가 답을 못
    하면 그게 예산 초과보다 나쁘다. 그때는 전체 한도만 적용되고 그 사실이 로그에 남는다.
    """

    def __init__(
        self,
        daily_limit_usd: float,
        *,
        state_path: Path | str | None = None,
        workspace_limits: Callable[[], Mapping[str, float]] | None = None,
    ) -> None:
        self._limit = daily_limit_usd
        self._workspace_limits = workspace_limits
        self._lock = threading.Lock()
        self._state_path = Path(state_path) if state_path else None
        self._day = _dt.date.today()
        self._spent = 0.0
        # 워크스페이스별 당일 누적. 전체 누적과 **따로** 센다 — 합이 전체와 어긋날 수
        # 있다(워크스페이스를 모르는 호출: 배치·점검). 그건 오류가 아니라 사실이다.
        self._by_workspace: dict[str, float] = {}
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
        # 워크스페이스별 누적. 옛 파일에는 없다 — 없으면 빈 채로 둔다(재시작 뒤
        # 워크스페이스 한도가 느슨해지지만, 전체 한도는 그대로 지켜진다).
        raw = data.get("by_workspace")
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    self._by_workspace[str(key)] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
        logger.info("당일 누적 비용 복원: $%.4f (%s)", self._spent, self._state_path)

    def _persist(self) -> None:
        """호출측이 락을 잡은 상태에서 부른다."""
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({
                    "day": self._day.isoformat(),
                    "spent_usd": round(self._spent, 6),
                    "by_workspace": {
                        k: round(v, 6) for k, v in sorted(self._by_workspace.items())
                    },
                }),
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
            self._by_workspace.clear()
            self._persist()

    def limit_for(self, workspace: str) -> float | None:
        """이 워크스페이스의 한도. 모르면 `None`(전체 한도만 적용).

        **0 이하는 한도가 아니라 「설정 안 함」 으로 본다.** 0 을 한도로 받으면 그
        워크스페이스는 영원히 답을 못 하는데, 화면에서는 그냥 빈 칸으로 보인다.

        화면도 이 함수를 쓴다 — 표시하는 한도와 막는 한도가 갈라지면, 사람은 화면에
        보이는 숫자를 올리고 아무 일도 일어나지 않는 것을 본다(2026-09-15).
        """
        if not workspace or self._workspace_limits is None:
            return None
        try:
            limits = self._workspace_limits() or {}
        except Exception as e:  # noqa: BLE001 - 한도를 못 읽는다고 막지 않는다
            logger.warning("워크스페이스 한도를 읽지 못했습니다(%s) — 전체 한도만 적용", e)
            return None
        try:
            value = float(limits.get(workspace.strip().lower()))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @property
    def spent_today(self) -> float:
        with self._lock:
            self._rollover()
            return self._spent

    def spent_by_workspace(self) -> dict[str, float]:
        """워크스페이스별 당일 누적. 화면이 「누가 썼나」 를 보여 줄 근거다."""
        with self._lock:
            self._rollover()
            return dict(self._by_workspace)

    def check(self, estimated_usd: float = 0.0, *, workspace: str | None = None) -> None:
        """호출 전 예상 비용을 더했을 때 상한을 넘으면 차단한다.

        **어느 한도에 걸렸는지 말한다.** 안 말하면 사람은 엉뚱한 쪽을 올린다 —
        2026-09-15 에 「일별 비용 상한 초과 ... 한도 $5.00」 만 보고 콘솔에서
        워크스페이스 상한을 올렸는데, 실제로 걸린 것은 전체 한도였다.

        `workspace` 를 안 주면 지금 문맥(`attribute_to`)에서 읽는다.
        """
        workspace = current_workspace() if workspace is None else workspace.strip().lower()
        limit = self.limit_for(workspace)
        with self._lock:
            self._rollover()
            used = self._by_workspace.get(workspace, 0.0)
            if limit is not None and used + estimated_usd > limit:
                raise CostLimitExceeded(
                    f"워크스페이스 '{workspace}' 일별 상한 초과: 사용 ${used:.2f}"
                    f" + 예상 ${estimated_usd:.2f} > 한도 ${limit:.2f}"
                    " (콘솔 > 워크스페이스 관리에서 조정)"
                )
            if self._spent + estimated_usd > self._limit:
                raise CostLimitExceeded(
                    f"전체 일별 비용 상한 초과: 사용 ${self._spent:.2f}"
                    f" + 예상 ${estimated_usd:.2f} > 한도 ${self._limit:.2f}"
                    " (전체 한도는 DAILY_COST_LIMIT_USD 환경변수)"
                )

    def record(self, usd: float, *, workspace: str | None = None) -> None:
        workspace = current_workspace() if workspace is None else workspace.strip().lower()
        with self._lock:
            self._rollover()
            self._spent += usd
            if workspace:
                self._by_workspace[workspace] = (
                    self._by_workspace.get(workspace, 0.0) + usd
                )
            self._persist()
