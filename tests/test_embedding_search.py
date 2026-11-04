from __future__ import annotations

import math

import pytest

from tybot import embedding_search


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql = ""
        self.params = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.sql = str(sql)
        self.params = dict(params)

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows=()):
        self.cur = Cursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cur


def scope(*channels: str) -> embedding_search.SearchScope:
    return embedding_search.SearchScope.checked("tyit", channels)


def test_scope_requires_an_explicit_workspace_and_channels():
    with pytest.raises(ValueError):
        embedding_search.SearchScope.checked("", ["C1"])
    with pytest.raises(ValueError):
        embedding_search.SearchScope.checked("tyit", ["*"])
    with pytest.raises(ValueError):
        embedding_search.SearchScope.checked("tyit", [""])


def test_direct_dataclass_construction_cannot_bypass_scope_validation():
    with pytest.raises(ValueError):
        embedding_search.candidates(
            [0.1],
            scope=embedding_search.SearchScope(" tyit ", frozenset({"C1"})),
            model_revision="local-v1",
            connect=Connection,
        )
    with pytest.raises(ValueError):
        embedding_search.candidates(
            [0.1],
            scope=embedding_search.SearchScope("tyit", frozenset({" C1"})),
            model_revision="local-v1",
            connect=Connection,
        )


def test_no_allowed_channels_never_opens_the_database():
    opened = []

    got = embedding_search.candidates(
        [0.1, 0.2],
        scope=scope(),
        model_revision="local-v1",
        connect=lambda: opened.append(True),
    )

    assert got == []
    assert opened == []


def test_database_failure_is_not_reported_as_no_match():
    assert embedding_search.candidates(
        [0.1], scope=scope("C1"), model_revision="local-v1", connect=lambda: None
    ) is None


@pytest.mark.parametrize("value", [[], [math.nan], [math.inf], [True], ["0.1"]])
def test_invalid_embeddings_are_rejected_before_database_access(value):
    opened = []
    with pytest.raises(ValueError):
        embedding_search.candidates(
            value,
            scope=scope("C1"),
            model_revision="local-v1",
            connect=lambda: opened.append(True),
        )
    assert opened == []


def test_query_is_bound_and_filtered_by_workspace_channel_and_active_revisions():
    conn = Connection()

    assert embedding_search.candidates(
        [0.1, -0.2],
        scope=scope("C2", "C1"),
        model_revision="local-v1",
        limit=12,
        connect=lambda: conn,
    ) == []

    compact = " ".join(conn.cur.sql.split())
    assert "d.workspace = %(workspace)s" in compact
    assert "d.channel_id = ANY(%(channel_ids)s)" in compact
    assert "d.model_revision = %(model_revision)s" in compact
    assert "d.state = 'ready'" in compact
    assert "d.is_current" in compact
    assert "m.state = 'active'" in compact
    assert "source_revision" in compact
    assert "body" not in compact and "question" not in compact and "answer" not in compact
    assert conn.cur.params == {
        "embedding": "[0.10000000000000001,-0.20000000000000001]",
        "workspace": "tyit",
        "channel_ids": ["C1", "C2"],
        "model_revision": "local-v1",
        "limit": 12,
    }


def test_candidate_contains_coordinates_and_no_source_text():
    conn = Connection([{
        "workspace": "tyit",
        "channel_id": "C1",
        "doc_path": "workspaces/tyit/channels/C1/raw/2026-11-04.md",
        "source_revision": "a" * 64,
        "chunker_revision": "boundary-v1",
        "model_revision": "local-v1",
        "chunk_no": 3,
        "start_offset": 120,
        "end_offset": 300,
        "text_sha256": "b" * 64,
        "distance": 0.125,
    }])

    got = embedding_search.candidates(
        [0.5], scope=scope("C1"), model_revision="local-v1", connect=lambda: conn
    )

    assert got == [embedding_search.VectorCandidate(
        workspace="tyit",
        channel_id="C1",
        doc_path="workspaces/tyit/channels/C1/raw/2026-11-04.md",
        source_revision="a" * 64,
        chunker_revision="boundary-v1",
        model_revision="local-v1",
        chunk_no=3,
        start_offset=120,
        end_offset=300,
        text_sha256="b" * 64,
        distance=0.125,
    )]
    assert not hasattr(got[0], "text") and not hasattr(got[0], "embedding")


@pytest.mark.parametrize("revision", ["", "bad revision", "x" * 129])
def test_model_revision_is_never_interpolated_into_sql(revision):
    with pytest.raises(ValueError):
        embedding_search.candidates(
            [0.1], scope=scope("C1"), model_revision=revision, connect=Connection
        )


@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
def test_candidate_limit_is_bounded(limit):
    with pytest.raises(ValueError):
        embedding_search.candidates(
            [0.1], scope=scope("C1"), model_revision="local-v1",
            limit=limit, connect=Connection,
        )
