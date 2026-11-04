from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive.store import ArchiveDoc, RawLine, SearchHit
from tybot.hybrid_search_eval import (
    ReplayContract,
    classify_baseline,
    compare_rankings,
    summarize,
)
from tybot.hybrid_search_rank import fuse


def hit(name: str, *, line: int = 1) -> SearchHit:
    path = Path(f"workspaces/sample/channels/C1/raw/{name}.md")
    raw = RawLine("2026-01-01 09:00", "speaker", f"secret-{name}", line, path)
    doc = ArchiveDoc(
        path=path,
        workspace="sample",
        channel="channel",
        channel_id="C1",
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        raw_lines=[raw],
    )
    return SearchHit(doc, raw, 1)


@pytest.mark.parametrize(
    ("row", "state", "eligible"),
    [
        ({"reason": "permission_unverified", "hits": 0}, "permission_failed", False),
        ({"reason": "channel_no_archive", "hits": 0}, "source_absent", False),
        ({"reason": "no_access", "hits": 0}, "source_absent", False),
        ({"reason": "no_hits", "hits": 0}, "query_miss", True),
        ({"reason": "", "hits": 0}, "unknown_no_evidence", False),
        ({"hits": 2, "citations": ["one"]}, "grounded_sample", True),
        ({"hits": 2, "citations": []}, "not_applicable", False),
    ],
)
def test_baseline_separates_failures_absence_and_query_misses(row, state, eligible):
    got = classify_baseline(row)
    assert got.state == state
    assert got.eligible is eligible


def test_processing_failure_is_not_mislabeled_as_vector_opportunity():
    got = classify_baseline(
        {"reason": "no_hits", "hits": 0, "error": "specialist-timeout:external-v3"}
    )
    assert got.state == "search_failed"
    assert got.eligible is False


def test_partial_scope_is_separate_from_a_query_miss():
    got = classify_baseline(
        {"reason": "search_budget", "hits": 0, "answer": "partial"}
    )
    assert got.state == "partial_scope"


@pytest.mark.parametrize(
    "contract",
    [
        ReplayContract(False, True, True),
        ReplayContract(True, False, True),
        ReplayContract(True, True, False),
    ],
)
def test_comparison_requires_identical_replay_conditions(contract):
    with pytest.raises(ValueError, match="same question, ACL, and source revision"):
        compare_rankings(
            [],
            fuse([], None, limit=1),
            contract=contract,
            top_k=1,
            keyword_latency_ms=0,
            vector_latency_ms=None,
            fusion_latency_ms=0,
            embedding_cost_usd=None,
        )


def test_comparison_reports_gain_displacement_recall_latency_and_cost():
    a, b, c = hit("a"), hit("b"), hit("c")
    ranking = fuse([a, b], [c, a], limit=3)
    got = compare_rankings(
        [a, b],
        ranking,
        contract=ReplayContract(True, True, True),
        top_k=2,
        expected_hits=[a, c],
        keyword_latency_ms=12.5,
        vector_latency_ms=8.0,
        fusion_latency_ms=0.2,
        embedding_cost_usd=0.001,
    )
    assert got.vector_only_count == 1
    assert got.hybrid_vector_only_at_k == 1
    assert got.keyword_displaced_at_k == 1
    assert got.keyword_recall_at_k == 0.5
    assert got.hybrid_recall_at_k == 1.0
    assert got.embedding_cost_usd == 0.001


def test_unavailable_vectors_keep_failure_latency_but_not_a_verified_cost():
    a = hit("a")
    ranking = fuse([a], None, limit=1)
    got = compare_rankings(
        [a],
        ranking,
        contract=ReplayContract(True, True, True),
        top_k=1,
        keyword_latency_ms=1,
        vector_latency_ms=2,
        fusion_latency_ms=0,
        embedding_cost_usd=None,
    )
    assert got.vector_latency_ms == 2
    with pytest.raises(ValueError, match="verified cost"):
        compare_rankings(
            [a],
            ranking,
            contract=ReplayContract(True, True, True),
            top_k=1,
            keyword_latency_ms=1,
            vector_latency_ms=2,
            fusion_latency_ms=0,
            embedding_cost_usd=0.1,
        )


def test_known_vector_run_requires_latency_but_cost_may_remain_unknown():
    ranking = fuse([], [], limit=1)
    with pytest.raises(ValueError, match="vector latency"):
        compare_rankings(
            [],
            ranking,
            contract=ReplayContract(True, True, True),
            top_k=1,
            keyword_latency_ms=1,
            vector_latency_ms=None,
            fusion_latency_ms=0,
            embedding_cost_usd=None,
        )


def test_keyword_results_from_a_different_replay_are_rejected():
    a, b = hit("a"), hit("b")
    with pytest.raises(ValueError, match="does not match the keyword replay"):
        compare_rankings(
            [b],
            fuse([a], [], limit=1),
            contract=ReplayContract(True, True, True),
            top_k=1,
            keyword_latency_ms=1,
            vector_latency_ms=1,
            fusion_latency_ms=1,
            embedding_cost_usd=0,
        )


def test_metrics_and_summary_repr_never_include_bodies_or_coordinates():
    a = hit("private")
    metrics = compare_rankings(
        [a],
        fuse([a], [], limit=1),
        contract=ReplayContract(True, True, True),
        top_k=1,
        keyword_latency_ms=1,
        vector_latency_ms=2,
        fusion_latency_ms=3,
        embedding_cost_usd=None,
    )
    summary = summarize(
        [classify_baseline({"reason": "no_hits", "hits": 0})], [metrics]
    )
    rendered = repr((metrics, summary))
    assert "secret-private" not in rendered
    assert "workspaces/sample" not in rendered


def test_summary_does_not_turn_unknown_cost_into_zero():
    a = hit("a")
    known = compare_rankings(
        [a],
        fuse([a], [], limit=1),
        contract=ReplayContract(True, True, True),
        top_k=1,
        keyword_latency_ms=1,
        vector_latency_ms=1,
        fusion_latency_ms=1,
        embedding_cost_usd=0.25,
    )
    unknown = compare_rankings(
        [a],
        fuse([a], [], limit=1),
        contract=ReplayContract(True, True, True),
        top_k=1,
        keyword_latency_ms=1,
        vector_latency_ms=1,
        fusion_latency_ms=1,
        embedding_cost_usd=None,
    )
    assert summarize([], [known]).total_embedding_cost_usd == 0.25
    assert summarize([], [known, unknown]).total_embedding_cost_usd is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_k", 0),
        ("keyword_latency_ms", -1),
        ("vector_latency_ms", float("nan")),
        ("fusion_latency_ms", float("inf")),
        ("embedding_cost_usd", -0.1),
    ],
)
def test_invalid_measurements_are_rejected(field, value):
    kwargs = {
        "top_k": 1,
        "keyword_latency_ms": 1,
        "vector_latency_ms": 1,
        "fusion_latency_ms": 1,
        "embedding_cost_usd": 0,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        compare_rankings(
            [],
            fuse([], [], limit=1),
            contract=ReplayContract(True, True, True),
            **kwargs,
        )
