from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive.store import ArchiveDoc, RawLine, SearchHit
from tybot.hybrid_search_shadow import ReplaySearchResult
from tybot.hybrid_search_tuning import (
    TuningCase,
    WeightOption,
    evaluate_weights,
)


def hit(name: str) -> SearchHit:
    path = Path(f"workspaces/private/channels/C1/raw/{name}.md")
    line = RawLine("2026-01-01 09:00", "speaker", f"secret-{name}", 1, path)
    doc = ArchiveDoc(
        path=path,
        workspace="private",
        channel="private-channel",
        channel_id="C1",
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        raw_lines=[line],
    )
    return SearchHit(doc, line, 1)


def result(*hits: SearchHit, acl: str = "acl-1", source: str = "source-1"):
    return ReplaySearchResult.checked(
        hits, acl_revision=acl, source_revision=source, cost_usd=0
    )


def test_weight_grid_reports_tradeoffs_without_choosing_a_winner():
    keyword, vector = hit("keyword"), hit("vector")
    cases = [
        TuningCase(result(keyword), result(vector), expected_hits=(vector,)),
    ]

    got = evaluate_weights(
        cases,
        [WeightOption(2, 1), WeightOption(1, 2)],
        top_k=1,
    )

    assert [item.option for item in got] == [
        WeightOption(2, 1),
        WeightOption(1, 2),
    ]
    assert got[0].mean_hybrid_recall_at_k == 0
    assert got[0].mean_recall_delta_at_k == 0
    assert got[0].displaced_keyword_at_k == 0
    assert got[1].mean_hybrid_recall_at_k == 1
    assert got[1].mean_recall_delta_at_k == 1
    assert got[1].displaced_keyword_at_k == 1


def test_unlabeled_cases_count_candidate_churn_without_inventing_recall():
    keyword, vector = hit("keyword"), hit("vector")
    got = evaluate_weights(
        [TuningCase(result(keyword), result(vector))],
        [WeightOption(1, 2)],
        top_k=1,
    )[0]

    assert got.comparisons == 1
    assert got.labeled_comparisons == 0
    assert got.mean_keyword_recall_at_k is None
    assert got.mean_hybrid_recall_at_k is None
    assert got.mean_recall_delta_at_k is None
    assert got.vector_only_at_k == 1
    assert got.displaced_keyword_at_k == 1


def test_recall_means_use_only_cases_with_expected_evidence():
    a, b = hit("a"), hit("b")
    got = evaluate_weights(
        [
            TuningCase(result(a), result(b), expected_hits=(b,)),
            TuningCase(result(a), result(b)),
        ],
        [WeightOption(1, 2)],
        top_k=1,
    )[0]

    assert got.comparisons == 2
    assert got.labeled_comparisons == 1
    assert got.mean_keyword_recall_at_k == 0
    assert got.mean_hybrid_recall_at_k == 1


def test_changed_acl_or_source_snapshot_is_refused_before_tuning():
    with pytest.raises(ValueError, match="same ACL and source revision"):
        TuningCase(result(acl="acl-1"), result(acl="acl-2"))
    with pytest.raises(ValueError, match="same ACL and source revision"):
        TuningCase(result(source="source-1"), result(source="source-2"))


def test_checked_replay_results_are_required():
    with pytest.raises(TypeError, match="checked replay results"):
        TuningCase(object(), result())  # type: ignore[arg-type]


def test_invalid_or_duplicate_options_fail_closed():
    case = TuningCase(result(), result())
    with pytest.raises(ValueError, match="unique"):
        evaluate_weights(
            [case], [WeightOption(1, 1), WeightOption(1, 1)], top_k=1
        )
    with pytest.raises(ValueError, match="positive finite"):
        evaluate_weights([case], [WeightOption(0, 1)], top_k=1)
    with pytest.raises(ValueError, match="top_k"):
        evaluate_weights([case], [WeightOption(1, 1)], top_k=0)


def test_empty_inputs_are_not_silently_reported_as_a_measurement():
    with pytest.raises(ValueError, match="tuning case"):
        evaluate_weights([], [WeightOption(1, 1)], top_k=1)
    with pytest.raises(ValueError, match="weight option"):
        evaluate_weights([TuningCase(result(), result())], [], top_k=1)


def test_report_repr_contains_no_source_body_or_coordinate():
    secret = hit("do-not-leak")
    report = evaluate_weights(
        [TuningCase(result(secret), result(secret), expected_hits=(secret,))],
        [WeightOption(1, 1)],
        top_k=1,
    )

    rendered = repr(report)
    assert "secret-do-not-leak" not in rendered
    assert "do-not-leak.md" not in rendered
    assert "private-channel" not in rendered
