"""워크스페이스별 상한을 DB 에서 읽어 봇에 공급한다.

## 왜 이 파일이 따로 있나
`cost.py` 는 **DB 를 모른다.** 한도를 어디서 읽는지는 정책이고, 상한을 어떻게 세는지는
계산이다. 둘을 한 파일에 두면 상한 계산을 DB 없이 시험할 수 없다.

## 왜 캐시하나
상한은 **모든 LLM 호출 앞에서** 읽힌다. 매번 DB 를 치면 답변 경로에 왕복이 하나
늘고, DB 가 느린 순간이 곧 답변이 느린 순간이 된다.

그렇다고 기동 시 한 번만 읽으면 **콘솔에서 바꾼 값이 재시작 전까지 안 먹는다.**
2026-09-15 에 실제로 그랬다 — 경영본부 상한을 $2→$5→$10 로 올렸는데 봇은 계속
막았고, 화면에는 바꾼 값이 그대로 보였다. 사람은 자기가 한 조작이 먹혔다고 믿는다.

그래서 **짧게 캐시한다.** 기본 60초. 「바꾸고 1분 안에 먹는다」 는 사람이 기다릴 수
있는 시간이고, 초당 수십 호출이 와도 DB 는 분당 한 번만 읽는다.

## 못 읽으면
**막지 않는다.** 직전에 읽은 값을 그대로 쓰고, 그것도 없으면 빈 표를 돌려준다(전체
한도만 적용). DB 가 잠깐 죽었다고 전 워크스페이스가 답을 못 하는 쪽이 예산 초과보다
나쁘다. 대신 경고를 남긴다 — 조용히 무한대가 되면 안 된다.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("tybot.gateway.budget")

DEFAULT_TTL_SECONDS = 60.0


class WorkspaceLimits:
    """DB 의 `workspace.limit_usd` 를 TTL 캐시로 공급한다. 호출 가능 객체."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        loader=None,
        clock=time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._loader = loader  # 시험에서 갈아 끼운다. 기본은 아래 지연 임포트
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: dict[str, float] = {}
        self._loaded_at: float | None = None
        self._warned = False

    def _load(self) -> dict[str, float]:
        if self._loader is not None:
            return self._loader()
        # 지연 임포트 — 게이트웨이가 콘솔·DB 없이도 임포트되어야 한다.
        from ..console.workspace_store import limits_by_workspace

        return limits_by_workspace()

    def __call__(self) -> dict[str, float]:
        now = self._clock()
        with self._lock:
            fresh = self._loaded_at is not None and now - self._loaded_at < self._ttl
            if fresh:
                return dict(self._cached)
        # **락 밖에서 읽는다.** DB 가 느릴 때 모든 답변 스레드가 한 줄로 서면
        # 상한 조회가 답변 지연으로 바뀐다. 동시에 두 번 읽힐 수는 있지만
        # 같은 값이라 해롭지 않다.
        try:
            loaded = {
                str(key).lower(): float(value)
                for key, value in (self._load() or {}).items()
            }
        except Exception as exc:  # noqa: BLE001 - 한도를 못 읽는다고 답변을 막지 않는다
            with self._lock:
                stale = dict(self._cached)
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "워크스페이스 상한을 읽지 못했습니다(%s) — %s",
                        exc,
                        "직전 값으로 계속합니다" if stale else "전체 한도만 적용합니다",
                    )
            return stale
        with self._lock:
            self._cached = loaded
            self._loaded_at = now
            self._warned = False
            return dict(loaded)

    def invalidate(self) -> None:
        """다음 조회에서 다시 읽게 한다."""
        with self._lock:
            self._loaded_at = None
