from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive.store import ArchiveDoc, RawLine, SearchHit
from tybot.hybrid_search_rank import fuse


def hit(name: str, *, score: int = 1, path: str | None = None, line: int = 1) -> SearchHit:
    source = Path(path or f"workspaces/sample/channels/C1/raw/{name}.md")
    raw = RawLine("2026-01-01 09:00", "speaker", f"sensitive-{name}", line, source)
    doc = ArchiveDoc(
        path=source,
        workspace="sample",
        channel="channel",
        channel_id="C1",
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        raw_lines=[raw],
    )
    return SearchHit(doc, raw, score)


def test_unavailable_vectors_preserve_keyword_objects_and_order_exactly():
    keywords = [hit("a", score=9), hit("b", score=3), hit("c")]
    ranking = fuse(keywords, None, limit=3)

    assert ranking.vector_state == "unavailable"
    assert ranking.hits == tuple(keywords)
    assert all(got is expected for got, expected in zip(ranking.hits, keywords, strict=True))
    assert [item.origin for item in ranking.results] == ["keyword"] * 3


def test_empty_vectors_are_distinct_but_keep_the_same_keyword_fallback():
    keywords = [hit("a"), hit("b")]
    ranking = fuse(keywords, [], limit=2)
    assert ranking.vector_state == "empty"
    assert ranking.hits == tuple(keywords)


def test_overlap_is_boosted_and_vector_only_evidence_can_enter_the_union():
    a, b, c = hit("a"), hit("b"), hit("c")
    ranking = fuse([a, b], [c, b], limit=3)

    assert ranking.hits == (b, a, c)
    assert [item.origin for item in ranking.results] == ["both", "keyword", "vector"]
    assert ranking.overlap_count == 1


def test_rank_not_raw_keyword_score_is_the_declared_input_contract():
    first = hit("first", score=1)
    second = hit("second", score=999)
    ranking = fuse([first, second], [], limit=2)
    assert ranking.hits == (first, second)


def test_duplicate_lines_from_overlapping_chunks_are_counted_once():
    a = hit("a")
    duplicate = hit("a", score=99)
    ranking = fuse([a, duplicate], [duplicate, a], limit=10)
    assert ranking.hits == (a,)
    assert ranking.keyword_count == ranking.vector_count == ranking.overlap_count == 1


def test_same_line_number_in_different_source_files_is_not_deduplicated():
    a = hit("a", path="workspaces/sample/channels/C1/raw/a.md", line=7)
    b = hit("b", path="workspaces/sample/channels/C1/raw/b.md", line=7)
    ranking = fuse([a], [b], limit=2)
    assert ranking.hits == (a, b)


def test_limit_is_applied_after_fusion_not_to_each_input():
    keywords = [hit(f"k{i}") for i in range(5)]
    vectors = [hit(f"v{i}") for i in range(5)]
    ranking = fuse(keywords, vectors, limit=3)
    assert len(ranking.hits) == 3
    assert ranking.keyword_count == ranking.vector_count == 5


def test_result_repr_does_not_contain_source_body():
    ranking = fuse([hit("private-body")], None, limit=1)
    assert "sensitive-private-body" not in repr(ranking)
    assert "sensitive-private-body" not in repr(ranking.results[0])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 1, "keyword_weight": 0}, "keyword_weight"),
        ({"limit": 1, "vector_weight": float("nan")}, "vector_weight"),
        ({"limit": 1, "rank_constant": 0}, "rank_constant"),
    ],
)
def test_invalid_rank_options_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        fuse([], [], **kwargs)


def test_non_search_hits_are_rejected():
    with pytest.raises(TypeError, match="SearchHit"):
        fuse([object()], [], limit=1)
