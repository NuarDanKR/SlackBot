from __future__ import annotations

import hashlib

import pytest

from tybot.archive.chunking import Chunk
from tybot.embedding_index_store import (
    DocumentKey,
    EmbeddingIndexWriter,
    IndexPublishError,
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
        EmbeddingIndexWriter(conn).publish(bad, "alpha", [chunk("alpha")], [[0.1]])
    assert conn.cur.calls == []


def test_publish_is_atomic_bound_and_never_inserts_source_text():
    conn = Connection([None, (2, "active"), None, (41,), None, None])
    result = EmbeddingIndexWriter(conn).publish(
        key(), "alpha", [chunk("alpha")], [[0.1, 0.2]]
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
    inactive = Connection([None, (2, "draft")])
    with pytest.raises(IndexPublishError, match="not_active"):
        EmbeddingIndexWriter(inactive).publish(key(), "alpha", [chunk("alpha")], [[0.1, 0.2]])
    wrong = Connection([None, (3, "active")])
    with pytest.raises(IndexPublishError, match="dimension"):
        EmbeddingIndexWriter(wrong).publish(key(), "alpha", [chunk("alpha")], [[0.1, 0.2]])


def test_identical_current_revision_is_idempotent():
    digest = hashlib.sha256(b"alpha").hexdigest()
    conn = Connection([None, (2, "active"), (9, "ready", True, 1), [(0, 0, 5, digest)]])
    result = EmbeddingIndexWriter(conn).publish(
        key(), "alpha", [chunk("alpha")], [[0.3, 0.4]]
    )
    assert result.already_current and result.document_revision_id == 9
    assert conn.cur.many == []


def test_incomplete_or_different_existing_revision_is_never_overwritten():
    conn = Connection([None, (2, "active"), (9, "building", False, 0)])
    with pytest.raises(IndexPublishError, match="conflict"):
        EmbeddingIndexWriter(conn).publish(key(), "alpha", [chunk("alpha")], [[0.3, 0.4]])


def test_document_coordinates_and_revisions_are_fail_closed():
    bad = key()
    with pytest.raises(ValueError):
        DocumentKey(**{**bad.__dict__, "workspace": ""}).checked()
    with pytest.raises(ValueError):
        DocumentKey(**{**bad.__dict__, "model_revision": "bad revision"}).checked()
