from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive.store import ArchiveDoc, RawLine, SearchHit
from tybot.hybrid_search_shadow import ReplaySearchResult, run_shadow


def hit(name: str) -> SearchHit:
    path = Path(f"workspaces/sample/channels/C1/raw/{name}.md")
    line = RawLine("2026-01-01 09:00", "speaker", f"private-{name}", 1, path)
    doc = ArchiveDoc(
        path=path,
        workspace="sample",
        channel="channel",
        channel_id="C1",
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        raw_lines=[line],
    )
    return SearchHit(doc, line, 1)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.001
        return self.value


def result(*hits, acl="acl-1", source="source-1", cost=None):
    return ReplaySearchResult.checked(
        hits, acl_revision=acl, source_revision=source, cost_usd=cost
    )


def test_same_question_is_used_without_being_retained_in_the_result():
    seen = []
    a = hit("a")
    run = run_shadow(
        "private question text",
        keyword_search=lambda query: seen.append(query) or result(a),
        vector_search=lambda query: seen.append(query) or result(a, cost=0),
        top_k=1,
        clock=Clock(),
    )
    assert seen == ["private question text", "private question text"]
    assert run.state == "compared"
    assert "private question text" not in repr(run)
    assert "private-a" not in repr(run)


def test_question_whitespace_is_replayed_exactly_not_normalized():
    seen = []
    run_shadow(
        "  exact question  ",
        keyword_search=lambda query: seen.append(query) or result(),
        vector_search=lambda query: seen.append(query) or result(),
        top_k=1,
        clock=Clock(),
    )
    assert seen == ["  exact question  ", "  exact question  "]


def test_keyword_failure_does_not_call_vector_and_hides_exception_body():
    called = False

    def keyword(_query):
        raise RuntimeError("private provider response")

    def vector(_query):
        nonlocal called
        called = True

    run = run_shadow(
        "question", keyword_search=keyword, vector_search=vector, top_k=1, clock=Clock()
    )
    assert run.state == "keyword_error"
    assert run.error_code == "keyword_error"
    assert called is False
    assert "private provider response" not in repr(run)


@pytest.mark.parametrize("vector_result", [None, RuntimeError("private vector body")])
def test_vector_failure_preserves_keyword_metrics_and_records_latency(vector_result):
    a = hit("a")

    def vector(_query):
        if isinstance(vector_result, Exception):
            raise vector_result
        return vector_result

    run = run_shadow(
        "question",
        keyword_search=lambda _query: result(a),
        vector_search=vector,
        top_k=1,
        clock=Clock(),
    )
    assert run.state == "vector_unavailable"
    assert run.metrics is not None
    assert run.metrics.vector_state == "unavailable"
    assert run.metrics.keyword_count == 1
    assert run.vector_latency_ms == 1.0
    assert run.metrics.embedding_cost_usd is None
    assert "private vector body" not in repr(run)


@pytest.mark.parametrize(
    ("acl", "source"), [("acl-2", "source-1"), ("acl-1", "source-2")]
)
def test_changed_acl_or_source_snapshot_refuses_comparison(acl, source):
    a = hit("a")
    run = run_shadow(
        "question",
        keyword_search=lambda _query: result(a),
        vector_search=lambda _query: result(a, acl=acl, source=source),
        top_k=1,
        clock=Clock(),
    )
    assert run.state == "snapshot_changed"
    assert run.metrics is None


def test_valid_replay_reports_vector_gain_and_cost():
    a, b = hit("a"), hit("b")
    run = run_shadow(
        "question",
        keyword_search=lambda _query: result(a),
        vector_search=lambda _query: result(b, cost=0.02),
        top_k=2,
        expected_hits=[b],
        clock=Clock(),
    )
    assert run.state == "compared"
    assert run.metrics is not None
    assert run.metrics.vector_only_count == 1
    assert run.metrics.keyword_recall_at_k == 0
    assert run.metrics.hybrid_recall_at_k == 1
    assert run.metrics.embedding_cost_usd == 0.02


def test_invalid_callback_result_is_a_fixed_code_not_a_traceback():
    run = run_shadow(
        "question",
        keyword_search=lambda _query: object(),
        vector_search=lambda _query: None,
        top_k=1,
        clock=Clock(),
    )
    assert run.state == "keyword_error"
    assert run.error_code == "keyword_invalid_result"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"acl_revision": ""},
        {"source_revision": " changed "},
        {"cost_usd": -1},
    ],
)
def test_replay_result_metadata_fails_closed(kwargs):
    values = {"acl_revision": "acl", "source_revision": "source", "cost_usd": 0}
    values.update(kwargs)
    with pytest.raises(ValueError):
        ReplaySearchResult.checked([], **values)


def test_direct_constructor_cannot_bypass_metadata_validation():
    with pytest.raises(ValueError, match="acl_revision"):
        ReplaySearchResult((), "", "source", 0)


def test_empty_question_is_not_sent_to_searches():
    with pytest.raises(ValueError, match="question"):
        run_shadow(
            "  ",
            keyword_search=lambda _query: result(),
            vector_search=lambda _query: result(),
            top_k=1,
        )
