"""Execute a body-free keyword/vector shadow replay without changing answers.

Search implementations are injected so this boundary neither chooses an
embedding provider nor opens archive files.  The same question object is sent
to both searches but is never retained in the returned run.  A comparison is
published only when both searches report identical ACL and source snapshots.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter

from tybot.archive.store import SearchHit
from tybot.hybrid_search_eval import (
    ReplayContract,
    ShadowMetrics,
    compare_rankings,
)
from tybot.hybrid_search_rank import fuse

SearchFn = Callable[[str], "ReplaySearchResult | None"]


@dataclass(frozen=True)
class ReplaySearchResult:
    hits: tuple[SearchHit, ...] = field(repr=False)
    acl_revision: str = field(repr=False)
    source_revision: str = field(repr=False)
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        _revision("acl_revision", self.acl_revision)
        _revision("source_revision", self.source_revision)
        if any(not isinstance(hit, SearchHit) for hit in self.hits):
            raise TypeError("replay results must contain SearchHit")
        if self.cost_usd is not None:
            _cost(self.cost_usd)

    @classmethod
    def checked(
        cls,
        hits: Sequence[SearchHit],
        *,
        acl_revision: str,
        source_revision: str,
        cost_usd: float | None = None,
    ) -> ReplaySearchResult:
        acl = _revision("acl_revision", acl_revision)
        source = _revision("source_revision", source_revision)
        items = tuple(hits)
        if any(not isinstance(hit, SearchHit) for hit in items):
            raise TypeError("replay results must contain SearchHit")
        if cost_usd is not None:
            _cost(cost_usd)
        return cls(items, acl, source, None if cost_usd is None else float(cost_usd))


@dataclass(frozen=True)
class ShadowRun:
    state: str
    metrics: ShadowMetrics | None = None
    keyword_latency_ms: float | None = None
    vector_latency_ms: float | None = None
    error_code: str = ""


def run_shadow(
    question: str,
    *,
    keyword_search: SearchFn,
    vector_search: SearchFn,
    top_k: int,
    expected_hits: Sequence[SearchHit] | None = None,
    clock: Callable[[], float] = perf_counter,
) -> ShadowRun:
    """Run both retrieval paths; never return the question or source identities."""
    query = str(question or "")
    if not query.strip():
        raise ValueError("question is required")

    keyword_started = clock()
    try:
        keyword = keyword_search(query)
    except Exception:  # noqa: BLE001 - fixed code, no provider/DB body escapes
        return ShadowRun(
            state="keyword_error",
            keyword_latency_ms=_elapsed(keyword_started, clock()),
            error_code="keyword_error",
        )
    keyword_ms = _elapsed(keyword_started, clock())
    if not isinstance(keyword, ReplaySearchResult):
        return ShadowRun(
            state="keyword_error",
            keyword_latency_ms=keyword_ms,
            error_code="keyword_invalid_result",
        )

    vector_started = clock()
    try:
        vector = vector_search(query)
    except Exception:  # noqa: BLE001 - keyword result remains the fail-closed path
        vector_ms = _elapsed(vector_started, clock())
        return _unavailable(
            keyword, keyword_ms, vector_ms, top_k, expected_hits, clock
        )
    vector_ms = _elapsed(vector_started, clock())
    if vector is None:
        return _unavailable(
            keyword, keyword_ms, vector_ms, top_k, expected_hits, clock
        )
    if not isinstance(vector, ReplaySearchResult):
        return ShadowRun(
            state="vector_error",
            keyword_latency_ms=keyword_ms,
            vector_latency_ms=vector_ms,
            error_code="vector_invalid_result",
        )
    if (
        keyword.acl_revision != vector.acl_revision
        or keyword.source_revision != vector.source_revision
    ):
        return ShadowRun(
            state="snapshot_changed",
            keyword_latency_ms=keyword_ms,
            vector_latency_ms=vector_ms,
            error_code="snapshot_changed",
        )

    fusion_started = clock()
    ranking = fuse(keyword.hits, vector.hits, limit=max(top_k, 1))
    fusion_ms = _elapsed(fusion_started, clock())
    metrics = compare_rankings(
        keyword.hits,
        ranking,
        contract=ReplayContract(True, True, True),
        top_k=top_k,
        expected_hits=None if expected_hits is None else list(expected_hits),
        keyword_latency_ms=keyword_ms,
        vector_latency_ms=vector_ms,
        fusion_latency_ms=fusion_ms,
        embedding_cost_usd=vector.cost_usd,
    )
    return ShadowRun("compared", metrics, keyword_ms, vector_ms)


def _unavailable(
    keyword: ReplaySearchResult,
    keyword_ms: float,
    vector_ms: float,
    top_k: int,
    expected_hits: Sequence[SearchHit] | None,
    clock: Callable[[], float],
) -> ShadowRun:
    fusion_started = clock()
    ranking = fuse(keyword.hits, None, limit=max(top_k, 1))
    fusion_ms = _elapsed(fusion_started, clock())
    metrics = compare_rankings(
        keyword.hits,
        ranking,
        contract=ReplayContract(True, True, True),
        top_k=top_k,
        expected_hits=None if expected_hits is None else list(expected_hits),
        keyword_latency_ms=keyword_ms,
        vector_latency_ms=vector_ms,
        fusion_latency_ms=fusion_ms,
        embedding_cost_usd=None,
    )
    return ShadowRun(
        "vector_unavailable",
        metrics,
        keyword_ms,
        vector_ms,
        "vector_unavailable",
    )


def _revision(name: str, value: object) -> str:
    got = str(value or "")
    if not got or got != got.strip() or len(got) > 256:
        raise ValueError(f"{name} must be non-empty and trimmed")
    return got


def _cost(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError("cost_usd must be a non-negative finite number")


def _elapsed(start: float, end: float) -> float:
    return max(0.0, (float(end) - float(start)) * 1000.0)
