"""Aggregate-only RRF weight comparison for B-12 shadow retrieval.

The input retains authorized ``SearchHit`` objects only for the duration of the
comparison.  The returned report contains numbers and weight settings, never
questions, source coordinates, or document bodies.  This module deliberately
does not choose a winner or alter production search.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tybot.archive.store import SearchHit
from tybot.hybrid_search_eval import ReplayContract, compare_rankings
from tybot.hybrid_search_rank import DEFAULT_RANK_CONSTANT, fuse
from tybot.hybrid_search_shadow import ReplaySearchResult


@dataclass(frozen=True)
class WeightOption:
    keyword_weight: float
    vector_weight: float
    rank_constant: int = DEFAULT_RANK_CONSTANT


@dataclass(frozen=True)
class TuningCase:
    """One body-free replay snapshot used only while tuning runs."""

    keyword: ReplaySearchResult = field(repr=False)
    vector: ReplaySearchResult = field(repr=False)
    expected_hits: tuple[SearchHit, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.keyword, ReplaySearchResult) or not isinstance(
            self.vector, ReplaySearchResult
        ):
            raise TypeError("tuning cases require checked replay results")
        if (
            self.keyword.acl_revision != self.vector.acl_revision
            or self.keyword.source_revision != self.vector.source_revision
        ):
            raise ValueError("tuning requires the same ACL and source revision")
        if self.expected_hits is not None and any(
            not isinstance(hit, SearchHit) for hit in self.expected_hits
        ):
            raise TypeError("expected_hits must contain SearchHit")


@dataclass(frozen=True)
class WeightEvaluation:
    option: WeightOption
    comparisons: int
    labeled_comparisons: int
    mean_keyword_recall_at_k: float | None
    mean_hybrid_recall_at_k: float | None
    mean_recall_delta_at_k: float | None
    vector_only_at_k: int
    displaced_keyword_at_k: int


def evaluate_weights(
    cases: tuple[TuningCase, ...] | list[TuningCase],
    options: tuple[WeightOption, ...] | list[WeightOption],
    *,
    top_k: int,
) -> tuple[WeightEvaluation, ...]:
    """Evaluate every option over identical snapshots without selecting one."""
    checked_cases = tuple(cases)
    checked_options = tuple(options)
    if not checked_cases:
        raise ValueError("at least one tuning case is required")
    if not checked_options:
        raise ValueError("at least one weight option is required")
    if any(not isinstance(case, TuningCase) for case in checked_cases):
        raise TypeError("cases must contain TuningCase")
    if any(not isinstance(option, WeightOption) for option in checked_options):
        raise TypeError("options must contain WeightOption")
    if len(set(checked_options)) != len(checked_options):
        raise ValueError("weight options must be unique")

    evaluations: list[WeightEvaluation] = []
    for option in checked_options:
        metrics = []
        for case in checked_cases:
            ranking = fuse(
                case.keyword.hits,
                case.vector.hits,
                limit=max(top_k, 1),
                keyword_weight=option.keyword_weight,
                vector_weight=option.vector_weight,
                rank_constant=option.rank_constant,
            )
            metrics.append(
                compare_rankings(
                    case.keyword.hits,
                    ranking,
                    contract=ReplayContract(True, True, True),
                    top_k=top_k,
                    expected_hits=(
                        None
                        if case.expected_hits is None
                        else list(case.expected_hits)
                    ),
                    keyword_latency_ms=0,
                    vector_latency_ms=0,
                    fusion_latency_ms=0,
                    embedding_cost_usd=case.vector.cost_usd,
                )
            )

        paired = [
            (item.keyword_recall_at_k, item.hybrid_recall_at_k)
            for item in metrics
            if item.keyword_recall_at_k is not None
            and item.hybrid_recall_at_k is not None
        ]
        keyword_recall = [pair[0] for pair in paired]
        hybrid_recall = [pair[1] for pair in paired]
        deltas = [hybrid - keyword for keyword, hybrid in paired]
        evaluations.append(
            WeightEvaluation(
                option=option,
                comparisons=len(metrics),
                labeled_comparisons=len(paired),
                mean_keyword_recall_at_k=_mean(keyword_recall),
                mean_hybrid_recall_at_k=_mean(hybrid_recall),
                mean_recall_delta_at_k=_mean(deltas),
                vector_only_at_k=sum(
                    item.hybrid_vector_only_at_k for item in metrics
                ),
                displaced_keyword_at_k=sum(
                    item.keyword_displaced_at_k for item in metrics
                ),
            )
        )
    return tuple(evaluations)


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)
