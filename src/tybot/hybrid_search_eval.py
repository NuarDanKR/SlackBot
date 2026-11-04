"""Sanitized shadow evaluation for keyword and hybrid archive retrieval (B-12).

The evaluator never searches or embeds text.  Callers supply already authorized
rankings produced for the same question and source snapshot.  Results contain
counts and timings only; questions, answer bodies, source coordinates, and model
output are deliberately absent.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from tybot.answer_issues import issue_codes
from tybot.archive.store import SearchHit
from tybot.hybrid_search_rank import HybridRanking

QUERY_MISS_REASONS = frozenset({"no_hits"})
SOURCE_ABSENT_REASONS = frozenset({"channel_no_archive", "no_access"})
PROCESSING_CODES = frozenset(
    {
        "error",
        "timeout",
        "resource_exhaustion",
        "empty_answer",
        "specialist_failure",
    }
)
ELIGIBLE_STATES = frozenset({"query_miss", "grounded_sample"})
MAX_TOP_K = 100


@dataclass(frozen=True)
class BaselineClassification:
    state: str
    eligible: bool
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReplayContract:
    same_question: bool
    same_acl: bool
    same_source_revision: bool

    @property
    def verified(self) -> bool:
        return self.same_question and self.same_acl and self.same_source_revision


@dataclass(frozen=True)
class ShadowMetrics:
    vector_state: str
    top_k: int
    keyword_count: int
    vector_count: int
    overlap_count: int
    vector_only_count: int
    hybrid_vector_only_at_k: int
    keyword_displaced_at_k: int
    expected_count: int | None
    keyword_recall_at_k: float | None
    hybrid_recall_at_k: float | None
    keyword_latency_ms: float
    vector_latency_ms: float | None
    fusion_latency_ms: float
    embedding_cost_usd: float | None


@dataclass(frozen=True)
class EvaluationSummary:
    baseline_total: int
    baseline_states: tuple[tuple[str, int], ...]
    comparisons: int
    vector_states: tuple[tuple[str, int], ...]
    vector_only_candidates: int
    displaced_keyword_candidates: int
    mean_keyword_recall_at_k: float | None
    mean_hybrid_recall_at_k: float | None
    total_embedding_cost_usd: float | None


def classify_baseline(row: dict, feedback: list[dict] | None = None) -> BaselineClassification:
    """Classify a QA row without claiming every no-hit answer is vector-fixable."""
    codes = tuple(issue_codes(row, feedback or []))
    code_set = set(codes)
    reason = str(row.get("reason") or "")

    if "permission_unverified" in code_set:
        state = "permission_failed"
    elif code_set & PROCESSING_CODES:
        state = "search_failed"
    elif "partial_scope" in code_set:
        state = "partial_scope"
    elif reason in SOURCE_ABSENT_REASONS:
        state = "source_absent"
    elif reason in QUERY_MISS_REASONS:
        state = "query_miss"
    elif "no_evidence" in code_set:
        state = "unknown_no_evidence"
    elif _has_grounded_evidence(row):
        state = "grounded_sample"
    else:
        state = "not_applicable"
    return BaselineClassification(state, state in ELIGIBLE_STATES, codes)


def compare_rankings(
    keyword_hits: list[SearchHit] | tuple[SearchHit, ...],
    hybrid: HybridRanking,
    *,
    contract: ReplayContract,
    top_k: int,
    expected_hits: list[SearchHit] | tuple[SearchHit, ...] | None = None,
    keyword_latency_ms: float,
    vector_latency_ms: float | None,
    fusion_latency_ms: float,
    embedding_cost_usd: float | None,
) -> ShadowMetrics:
    """Compare rankings only when replay conditions are explicitly identical."""
    if not contract.verified:
        raise ValueError("shadow replay requires the same question, ACL, and source revision")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
    _nonnegative_finite("keyword_latency_ms", keyword_latency_ms)
    _nonnegative_finite("fusion_latency_ms", fusion_latency_ms)
    if hybrid.vector_state == "unavailable":
        if embedding_cost_usd is not None:
            raise ValueError("unavailable vectors cannot report a verified cost")
        if vector_latency_ms is not None:
            _nonnegative_finite("vector_latency_ms", vector_latency_ms)
    else:
        if vector_latency_ms is None:
            raise ValueError("vector latency is required when vector lookup ran")
        _nonnegative_finite("vector_latency_ms", vector_latency_ms)
        if embedding_cost_usd is not None:
            _nonnegative_finite("embedding_cost_usd", embedding_cost_usd)

    keywords = _unique_identities(keyword_hits)
    _verify_keyword_input(keywords, hybrid)
    hybrid_ids = tuple(_identity(item.hit) for item in hybrid.results)
    keyword_top = set(keywords[:top_k])
    hybrid_top = set(hybrid_ids[:top_k])
    vector_only_at_k = sum(
        1 for item in hybrid.results[:top_k] if item.vector_rank is not None and item.keyword_rank is None
    )
    expected = None if expected_hits is None else set(_unique_identities(expected_hits))
    keyword_recall = _recall(keyword_top, expected)
    hybrid_recall = _recall(hybrid_top, expected)
    return ShadowMetrics(
        vector_state=hybrid.vector_state,
        top_k=top_k,
        keyword_count=hybrid.keyword_count,
        vector_count=hybrid.vector_count,
        overlap_count=hybrid.overlap_count,
        vector_only_count=max(0, hybrid.vector_count - hybrid.overlap_count),
        hybrid_vector_only_at_k=vector_only_at_k,
        keyword_displaced_at_k=len(keyword_top - hybrid_top),
        expected_count=None if expected is None else len(expected),
        keyword_recall_at_k=keyword_recall,
        hybrid_recall_at_k=hybrid_recall,
        keyword_latency_ms=float(keyword_latency_ms),
        vector_latency_ms=None if vector_latency_ms is None else float(vector_latency_ms),
        fusion_latency_ms=float(fusion_latency_ms),
        embedding_cost_usd=None if embedding_cost_usd is None else float(embedding_cost_usd),
    )


def summarize(
    baseline: list[BaselineClassification] | tuple[BaselineClassification, ...],
    comparisons: list[ShadowMetrics] | tuple[ShadowMetrics, ...],
) -> EvaluationSummary:
    states = Counter(item.state for item in baseline)
    vector_states = Counter(item.vector_state for item in comparisons)
    keyword_recall = [
        item.keyword_recall_at_k
        for item in comparisons
        if item.keyword_recall_at_k is not None
    ]
    hybrid_recall = [
        item.hybrid_recall_at_k
        for item in comparisons
        if item.hybrid_recall_at_k is not None
    ]
    costs = [item.embedding_cost_usd for item in comparisons]
    known_costs = [cost for cost in costs if cost is not None]
    total_cost = None if len(known_costs) != len(costs) else sum(known_costs)
    return EvaluationSummary(
        baseline_total=len(baseline),
        baseline_states=tuple(sorted(states.items())),
        comparisons=len(comparisons),
        vector_states=tuple(sorted(vector_states.items())),
        vector_only_candidates=sum(item.vector_only_count for item in comparisons),
        displaced_keyword_candidates=sum(
            item.keyword_displaced_at_k for item in comparisons
        ),
        mean_keyword_recall_at_k=_mean(keyword_recall),
        mean_hybrid_recall_at_k=_mean(hybrid_recall),
        total_embedding_cost_usd=total_cost,
    )


def _has_grounded_evidence(row: dict) -> bool:
    return int(row.get("hits") or 0) > 0 and bool(row.get("citations") or [])


def _identity(hit: SearchHit) -> tuple[str, str, str, int]:
    if not isinstance(hit, SearchHit):
        raise TypeError("rank inputs must contain SearchHit")
    source = str(hit.line.source_path or hit.doc.path)
    return (
        str(hit.doc.workspace),
        str(hit.doc.channel_id or hit.doc.channel),
        source,
        int(hit.line.lineno),
    )


def _unique_identities(hits) -> tuple[tuple[str, str, str, int], ...]:
    return tuple(dict.fromkeys(_identity(hit) for hit in hits))


def _verify_keyword_input(keywords: tuple[tuple[str, str, str, int], ...], hybrid: HybridRanking) -> None:
    if hybrid.keyword_count != len(keywords):
        raise ValueError("hybrid ranking does not match the keyword replay")
    for item in hybrid.results:
        if item.keyword_rank is None:
            continue
        index = item.keyword_rank - 1
        if index < 0 or index >= len(keywords) or keywords[index] != _identity(item.hit):
            raise ValueError("hybrid ranking does not match the keyword replay")


def _recall(found: set, expected: set | None) -> float | None:
    if expected is None or not expected:
        return None
    return len(found & expected) / len(expected)


def _nonnegative_finite(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be a non-negative finite number")


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)
