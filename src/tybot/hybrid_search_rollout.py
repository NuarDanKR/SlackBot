"""Fail-closed review gate for B-12 hybrid-search shadow measurements.

Passing this gate means only that a human has enough aggregate evidence to
review a rollout.  It never enables vector search, selects a model, or retains
questions and source coordinates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tybot.hybrid_search_eval import ShadowMetrics
from tybot.hybrid_search_tuning import WeightEvaluation


@dataclass(frozen=True)
class RolloutPolicy:
    min_comparisons: int
    min_labeled_comparisons: int
    min_mean_recall_delta_at_k: float
    max_vector_unavailable_rate: float
    max_mean_vector_latency_ms: float
    max_mean_embedding_cost_usd: float
    max_displaced_keyword_per_comparison: float

    def __post_init__(self) -> None:
        if isinstance(self.min_comparisons, bool) or self.min_comparisons < 1:
            raise ValueError("min_comparisons must be a positive integer")
        if (
            isinstance(self.min_labeled_comparisons, bool)
            or self.min_labeled_comparisons < 1
            or self.min_labeled_comparisons > self.min_comparisons
        ):
            raise ValueError("min_labeled_comparisons is invalid")
        for name, value in (
            ("min_mean_recall_delta_at_k", self.min_mean_recall_delta_at_k),
            ("max_vector_unavailable_rate", self.max_vector_unavailable_rate),
            ("max_mean_vector_latency_ms", self.max_mean_vector_latency_ms),
            ("max_mean_embedding_cost_usd", self.max_mean_embedding_cost_usd),
            (
                "max_displaced_keyword_per_comparison",
                self.max_displaced_keyword_per_comparison,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{name} must be a non-negative finite number")
        if self.max_vector_unavailable_rate > 1:
            raise ValueError("max_vector_unavailable_rate must be at most 1")


@dataclass(frozen=True)
class RolloutReview:
    state: str  # review_ready | blocked
    blockers: tuple[str, ...]
    comparisons: int
    labeled_comparisons: int
    mean_recall_delta_at_k: float | None
    vector_unavailable_rate: float
    mean_vector_latency_ms: float | None
    mean_embedding_cost_usd: float | None
    displaced_keyword_per_comparison: float


def assess_rollout(
    evaluation: WeightEvaluation,
    measurements: tuple[ShadowMetrics, ...] | list[ShadowMetrics],
    policy: RolloutPolicy,
) -> RolloutReview:
    """Apply explicit thresholds and return fixed blocker codes only."""
    if not isinstance(evaluation, WeightEvaluation):
        raise TypeError("evaluation must be WeightEvaluation")
    if not isinstance(policy, RolloutPolicy):
        raise TypeError("policy must be RolloutPolicy")
    samples = tuple(measurements)
    if any(not isinstance(item, ShadowMetrics) for item in samples):
        raise TypeError("measurements must contain ShadowMetrics")
    if evaluation.comparisons != len(samples):
        raise ValueError("weight evaluation and measurements do not match")

    blockers: list[str] = []
    if evaluation.comparisons < policy.min_comparisons:
        blockers.append("insufficient_comparisons")
    if evaluation.labeled_comparisons < policy.min_labeled_comparisons:
        blockers.append("insufficient_labeled_comparisons")

    recall_delta = evaluation.mean_recall_delta_at_k
    if recall_delta is None:
        blockers.append("recall_unverified")
    elif recall_delta < policy.min_mean_recall_delta_at_k:
        blockers.append("recall_gain_below_policy")

    unavailable = sum(item.vector_state == "unavailable" for item in samples)
    unavailable_rate = unavailable / len(samples) if samples else 1.0
    if unavailable_rate > policy.max_vector_unavailable_rate:
        blockers.append("vector_unavailable_rate_above_policy")

    latencies = [item.vector_latency_ms for item in samples]
    latency_unknown = any(value is None for value in latencies)
    mean_latency = None if latency_unknown else _mean([float(v) for v in latencies])
    if mean_latency is None:
        blockers.append("vector_latency_unknown")
    elif mean_latency > policy.max_mean_vector_latency_ms:
        blockers.append("vector_latency_above_policy")

    attempted = [item for item in samples if item.vector_state != "unavailable"]
    costs = [item.embedding_cost_usd for item in attempted]
    cost_unknown = not attempted or any(value is None for value in costs)
    mean_cost = None if cost_unknown else _mean([float(v) for v in costs])
    if mean_cost is None:
        blockers.append("embedding_cost_unknown")
    elif mean_cost > policy.max_mean_embedding_cost_usd:
        blockers.append("embedding_cost_above_policy")

    displaced_rate = (
        evaluation.displaced_keyword_at_k / evaluation.comparisons
        if evaluation.comparisons
        else math.inf
    )
    if displaced_rate > policy.max_displaced_keyword_per_comparison:
        blockers.append("keyword_displacement_above_policy")

    return RolloutReview(
        state="blocked" if blockers else "review_ready",
        blockers=tuple(blockers),
        comparisons=evaluation.comparisons,
        labeled_comparisons=evaluation.labeled_comparisons,
        mean_recall_delta_at_k=recall_delta,
        vector_unavailable_rate=unavailable_rate,
        mean_vector_latency_ms=mean_latency,
        mean_embedding_cost_usd=mean_cost,
        displaced_keyword_per_comparison=displaced_rate,
    )


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)
