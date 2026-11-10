"""요청별 캐시 감사 값은 누적 계수의 양수 차이만 기록한다."""

from dataclasses import dataclass

from tybot.slack.pilot import (
    _bot_cache_metric_snapshot,
    _cache_metric_delta,
    _cache_metric_snapshot,
)


@dataclass(frozen=True)
class _Stats:
    lookups: int = 0
    hits: int = 0
    saved_input_tokens: int = 0
    saved_output_tokens: int = 0
    saved_usd: float = 0.0


class _Store:
    def cache_stats(self):
        return {"raw_lookups": 3, "raw_hits": 2, "raw_invalidations": 1}


class _Router:
    response_cache_stats = _Stats(2, 1, 100, 20, 0.01)
    prompt_cache_stats = _Stats(3, 2)


class _Engine:
    router = _Router()


def test_snapshot_contains_counts_but_no_source_coordinates():
    got = _cache_metric_snapshot(_Store(), _Engine())

    assert got["raw_hits"] == 2
    assert got["response_saved_input_tokens"] == 100
    assert got["prompt_hits"] == 2
    assert all("path" not in key and "question" not in key for key in got)


def test_delta_drops_zeros_and_counter_resets():
    before = {"raw_hits": 4, "raw_misses": 2, "response_saved_usd": 0.01}
    after = {"raw_hits": 7, "raw_misses": 1, "response_saved_usd": 0.01234567}

    assert _cache_metric_delta(before, after) == {
        "raw_hits": 3,
        "response_saved_usd": 0.002346,
    }


def test_a_minimal_workspace_bot_can_skip_optional_cache_metrics():
    """일부 단위시험과 오류 복구 경로는 `__init__` 없는 최소 봇을 사용한다."""
    assert _bot_cache_metric_snapshot(object()) == {}
