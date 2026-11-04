from __future__ import annotations

from tybot.hybrid_search_eval import ShadowMetrics
from tybot.hybrid_search_rollout import RolloutPolicy, assess_rollout
from tybot.hybrid_search_tuning import WeightEvaluation, WeightOption


def policy(**overrides) -> RolloutPolicy:
    values = {
        "min_comparisons": 2,
        "min_labeled_comparisons": 1,
        "min_mean_recall_delta_at_k": 0.1,
        "max_vector_unavailable_rate": 0.1,
        "max_mean_vector_latency_ms": 100,
        "max_mean_embedding_cost_usd": 0.01,
        "max_displaced_keyword_per_comparison": 1,
    }
    values.update(overrides)
    return RolloutPolicy(**values)


def evaluation(**overrides) -> WeightEvaluation:
    values = {
        "option": WeightOption(1, 1),
        "comparisons": 2,
        "labeled_comparisons": 2,
        "mean_keyword_recall_at_k": 0.5,
        "mean_hybrid_recall_at_k": 0.75,
        "mean_recall_delta_at_k": 0.25,
        "vector_only_at_k": 1,
        "displaced_keyword_at_k": 1,
    }
    values.update(overrides)
    return WeightEvaluation(**values)


def metric(*, state="available", latency=50, cost=0.005) -> ShadowMetrics:
    return ShadowMetrics(
        vector_state=state,
        top_k=5,
        keyword_count=5,
        vector_count=5,
        overlap_count=4,
        vector_only_count=1,
        hybrid_vector_only_at_k=1,
        keyword_displaced_at_k=1,
        expected_count=1,
        keyword_recall_at_k=0.5,
        hybrid_recall_at_k=1,
        keyword_latency_ms=10,
        vector_latency_ms=latency,
        fusion_latency_ms=1,
        embedding_cost_usd=cost,
    )


def test_passing_thresholds_only_make_the_run_ready_for_human_review():
    got = assess_rollout(evaluation(), [metric(), metric()], policy())

    assert got.state == "review_ready"
    assert got.blockers == ()
    assert got.mean_vector_latency_ms == 50
    assert got.mean_embedding_cost_usd == 0.005


def test_every_failed_dimension_has_a_fixed_blocker_code():
    got = assess_rollout(
        evaluation(
            comparisons=1,
            labeled_comparisons=0,
            mean_recall_delta_at_k=None,
            displaced_keyword_at_k=3,
        ),
        [metric(state="unavailable", latency=None, cost=None)],
        policy(),
    )

    assert got.state == "blocked"
    assert got.blockers == (
        "insufficient_comparisons",
        "insufficient_labeled_comparisons",
        "recall_unverified",
        "vector_unavailable_rate_above_policy",
        "vector_latency_unknown",
        "embedding_cost_unknown",
        "keyword_displacement_above_policy",
    )


def test_known_values_over_policy_are_not_folded_into_unknown():
    got = assess_rollout(
        evaluation(mean_recall_delta_at_k=0.05, displaced_keyword_at_k=3),
        [metric(latency=150, cost=0.02), metric(latency=150, cost=0.02)],
        policy(),
    )

    assert got.blockers == (
        "recall_gain_below_policy",
        "vector_latency_above_policy",
        "embedding_cost_above_policy",
        "keyword_displacement_above_policy",
    )


def test_unknown_cost_is_not_zero_and_unavailable_runs_do_not_define_cost():
    unknown = assess_rollout(
        evaluation(), [metric(cost=None), metric()], policy()
    )
    unavailable = assess_rollout(
        evaluation(),
        [metric(state="unavailable", cost=None), metric(state="unavailable", cost=None)],
        policy(max_vector_unavailable_rate=1),
    )

    assert unknown.mean_embedding_cost_usd is None
    assert "embedding_cost_unknown" in unknown.blockers
    assert unavailable.mean_embedding_cost_usd is None
    assert "embedding_cost_unknown" in unavailable.blockers


def test_mismatched_measurement_set_is_refused_not_partially_assessed():
    try:
        assess_rollout(evaluation(comparisons=2), [metric()], policy())
    except ValueError as exc:
        assert str(exc) == "weight evaluation and measurements do not match"
    else:
        raise AssertionError("mismatched measurement set was accepted")


def test_result_contains_aggregates_not_questions_or_source_coordinates():
    got = assess_rollout(evaluation(), [metric(), metric()], policy())
    rendered = repr(got)

    assert "question" not in rendered
    assert "workspace" not in rendered
    assert "doc_path" not in rendered


def test_policy_has_no_implicit_thresholds_and_rejects_invalid_values():
    invalid = [
        {"min_comparisons": 0},
        {"min_labeled_comparisons": 3},
        {"max_vector_unavailable_rate": 1.1},
        {"max_mean_vector_latency_ms": float("inf")},
        {"max_mean_embedding_cost_usd": -1},
    ]
    for values in invalid:
        try:
            policy(**values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid policy accepted: {values}")
