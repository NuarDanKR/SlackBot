from __future__ import annotations

import hashlib

import pytest

from tybot.archive.chunking import Chunk
from tybot.embedding_index_store import (
    DocumentKey,
    EmbeddingIndexWriter,
    IndexPublishError,
    current_document_ids,
    prepare_chunks,
)


def chunk(text: str, start: int = 0) -> Chunk:
    return Chunk(text=text, start=start, end=start + len(text), kind="text", line_start=1, line_end=1)


def key(text: str = "alpha") -> DocumentKey:
    return DocumentKey(
        workspace="tyit", channel_id="C1", doc_path="workspaces/tyit/C1/raw/a.md",
        source_revision=hashlib.sha256(text.encode()).hexdigest(),
        chunker_revision="chunk-v1:max=1200:ov=120", model_revision="local-v1",
    )


class Tx:
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Cursor:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []
        self.many = []
        self.result = None

    def __enter__(self): return self
    def __exit__(self, *_): return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(str(sql).split()), tuple(params)))
        self.result = self.scripted.pop(0) if self.scripted else None

    def executemany(self, sql, rows):
        self.many.append((" ".join(str(sql).split()), list(rows)))

    def fetchone(self): return self.result
    def fetchall(self): return self.result or []


class Connection:
    def __init__(self, scripted): self.cur = Cursor(scripted)
    def transaction(self): return Tx()
    def cursor(self): return self.cur


def test_prepare_requires_exact_source_slices_and_one_vector_each():
    with pytest.raises(ValueError, match="exactly one"):
        prepare_chunks("alpha", [chunk("alpha")], [])
    with pytest.raises(ValueError, match="coordinates"):
        prepare_chunks("alpha", [chunk("wrong")], [[0.1]])
    with pytest.raises(ValueError, match="non-finite"):
        prepare_chunks("alpha", [chunk("alpha")], [[float("nan")]])


def test_source_revision_is_checked_before_opening_a_transaction():
    conn = Connection([])
    bad = key()
    bad = DocumentKey(**{**bad.__dict__, "source_revision": "0" * 64})
    with pytest.raises(ValueError, match="source_revision"):
        EmbeddingIndexWriter(conn).publish(
            bad, "alpha", [chunk("alpha")], [[0.1]], expected_current=None
        )
    assert conn.cur.calls == []


def test_publish_is_atomic_bound_and_never_inserts_source_text():
    conn = Connection([None, None, (2, "active"), None, (41,), None, None])
    result = EmbeddingIndexWriter(conn).publish(
        key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
    )
    assert result.document_revision_id == 41 and not result.already_current
    assert len(conn.cur.many) == 1
    sql, rows = conn.cur.many[0]
    assert "%s::vector" in sql
    assert rows == [(41, 0, 0, 5, hashlib.sha256(b"alpha").hexdigest(), "[0.10000000000000001,0.20000000000000001]")]
    combined_sql = " ".join(sql for sql, _ in conn.cur.calls) + " " + sql
    assert "alpha" not in combined_sql
    assert any("state='ready', is_current=true" in sql for sql, _ in conn.cur.calls)


def test_inactive_model_and_wrong_dimensions_fail_before_insert():
    inactive = Connection([None, None, (2, "draft")])
    with pytest.raises(IndexPublishError, match="not_active"):
        EmbeddingIndexWriter(inactive).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
        )
    wrong = Connection([None, None, (3, "active")])
    with pytest.raises(IndexPublishError, match="dimension"):
        EmbeddingIndexWriter(wrong).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
        )


def test_identical_current_revision_is_idempotent():
    digest = hashlib.sha256(b"alpha").hexdigest()
    conn = Connection(
        [
            None,
            (9, digest, "chunk-v1:max=1200:ov=120"),
            (2, "active"),
            (9, "ready", True, 1),
            [(0, 0, 5, digest)],
        ]
    )
    result = EmbeddingIndexWriter(conn).publish(
        key(), "alpha", [chunk("alpha")], [[0.3, 0.4]], expected_current=9
    )
    assert result.already_current and result.document_revision_id == 9
    assert conn.cur.many == []


def test_incomplete_or_different_existing_revision_is_never_overwritten():
    conn = Connection([None, None, (2, "active"), (9, "building", False, 0)])
    with pytest.raises(IndexPublishError, match="conflict"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.3, 0.4]], expected_current=None
        )


def test_document_coordinates_and_revisions_are_fail_closed():
    bad = key()
    with pytest.raises(ValueError):
        DocumentKey(**{**bad.__dict__, "workspace": ""}).checked()
    with pytest.raises(ValueError):
        DocumentKey(**{**bad.__dict__, "model_revision": "bad revision"}).checked()


def test_a_revision_that_lost_the_document_while_embedding_is_refused():
    """A newer revision became current while this one was being embedded.

    The vectors in hand describe text that is no longer the canonical source,
    so they must not take the document back.  Nothing is written: no manifest
    row, no chunk, and the revision that is current stays current.
    """
    conn = Connection([None, (7, "0" * 64, "other-chunker")])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
        )
    assert conn.cur.many == []
    statements = [sql for sql, _ in conn.cur.calls]
    # `FOR UPDATE` locks a row; it does not change one. Match the verb, not the word.
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql in statements)


def test_the_expectation_is_compared_while_the_lock_is_held():
    """Order matters: lock, then compare, then write.

    Comparing before the advisory lock would read a value that another
    publication is free to change before we act on it -- the check would be
    decoration.
    """
    conn = Connection([None, (7, "0" * 64, "other-chunker")])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
        )
    first, second = conn.cur.calls[0][0], conn.cur.calls[1][0]
    assert "pg_advisory_xact_lock" in first
    assert "is_current" in second and "FOR UPDATE" in second
    # The comparison is scoped exactly like the statement that retires the old
    # row.  Narrowing it by chunker_revision would make a re-chunk unpublishable,
    # so the column may be read but never used to filter.
    assert "chunker_revision=" not in second
    assert "AND model_revision=%s AND is_current" in second


def test_a_first_indexing_is_refused_when_the_document_already_has_a_current():
    conn = Connection([None, (7, "0" * 64, "other-chunker")])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=None
        )


def test_an_expectation_of_a_document_that_has_none_is_refused():
    """Symmetric: the caller believed it was replacing something that is gone."""
    conn = Connection([None, None])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=7
        )


def test_a_different_current_row_is_refused():
    conn = Connection([None, (8, "0" * 64, "other-chunker")])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=7
        )


@pytest.mark.parametrize("value", [0, -1, True, "7", 7.0])
def test_an_unusable_expectation_never_opens_a_transaction(value):
    conn = Connection([])
    with pytest.raises(ValueError, match="expected_current"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.1, 0.2]], expected_current=value
        )
    assert conn.cur.calls == []


def test_publishing_without_saying_what_it_replaces_is_a_type_error():
    """No default. A caller that cannot name the current row cannot publish."""
    conn = Connection([])
    with pytest.raises(TypeError):
        EmbeddingIndexWriter(conn).publish(key(), "alpha", [chunk("alpha")], [[0.1, 0.2]])
    assert conn.cur.calls == []


def test_the_revision_already_current_is_idempotent_despite_a_stale_expectation():
    """Two workers picked up the same source. The second is not an error.

    Its view of what was current is older, but the document already holds
    exactly the revision it set out to install, and the idempotent path writes
    nothing. Refusing here would make ordinary duplicated work fail.
    """
    digest = hashlib.sha256(b"alpha").hexdigest()
    conn = Connection(
        [
            None,
            (9, digest, "chunk-v1:max=1200:ov=120"),
            (2, "active"),
            (9, "ready", True, 1),
            [(0, 0, 5, digest)],
        ]
    )
    result = EmbeddingIndexWriter(conn).publish(
        key(), "alpha", [chunk("alpha")], [[0.3, 0.4]], expected_current=None
    )
    assert result.already_current and result.document_revision_id == 9
    assert conn.cur.many == []


def test_a_stale_expectation_is_refused_when_the_current_row_is_another_revision():
    """The exemption is exact: same source revision *and* same chunker revision."""
    digest = hashlib.sha256(b"alpha").hexdigest()
    conn = Connection([None, (9, digest, "chunk-v2:max=600:ov=60")])
    with pytest.raises(IndexPublishError, match="superseded"):
        EmbeddingIndexWriter(conn).publish(
            key(), "alpha", [chunk("alpha")], [[0.3, 0.4]], expected_current=None
        )
    assert conn.cur.many == []


def test_current_ids_are_read_without_narrowing_to_a_chunker():
    conn = Connection([[("tyit", "C1", "a.md", 4), ("tyit", "C2", "b.md", 9)]])
    observed = current_document_ids(conn, model_revision="local-v1")

    assert observed == {("tyit", "C1", "a.md"): 4, ("tyit", "C2", "b.md"): 9}
    sql, params = conn.cur.calls[0]
    assert params == ("local-v1",)
    assert "is_current" in sql and "chunker_revision" not in sql
    # Manifest coordinates only. No source text column, no vector column.
    assert "embedding" not in sql and "text" not in sql


@pytest.mark.parametrize("revision", ["", " local-v1", None])
def test_current_ids_refuse_an_unusable_model_revision(revision):
    conn = Connection([])
    with pytest.raises(ValueError, match="model_revision"):
        current_document_ids(conn, model_revision=revision)
    assert conn.cur.calls == []
