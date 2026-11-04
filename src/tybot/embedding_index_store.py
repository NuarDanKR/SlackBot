"""Atomic writer for the rebuildable B-12 vector candidate index.

The caller owns source loading, ACL decisions, chunking, and model calls.  This
module verifies their immutable outputs and publishes one complete revision in
one transaction.  It never stores source text or provider responses.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from tybot.archive.chunking import Chunk

MAX_DIMENSIONS = 16_000
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHUNKER_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IndexPublishError(RuntimeError):
    """A revision cannot be safely published."""


class _Cursor(Protocol):
    def execute(self, query: str, params=...): ...
    def executemany(self, query: str, params_seq): ...
    def fetchone(self): ...
    def fetchall(self): ...


@dataclass(frozen=True)
class DocumentKey:
    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str
    chunker_revision: str
    model_revision: str

    def checked(self) -> DocumentKey:
        values = (self.workspace, self.channel_id, self.doc_path)
        if any(not value or value != value.strip() for value in values):
            raise ValueError("document coordinates must be non-empty and trimmed")
        if not _SHA256_RE.fullmatch(self.source_revision):
            raise ValueError("source_revision must be a lowercase SHA-256")
        if not _CHUNKER_REVISION_RE.fullmatch(self.chunker_revision):
            raise ValueError("chunker_revision is invalid")
        if not _REVISION_RE.fullmatch(self.model_revision):
            raise ValueError("model_revision is invalid")
        return self


@dataclass(frozen=True)
class PreparedChunk:
    chunk_no: int
    start_offset: int
    end_offset: int
    text_sha256: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class PublishResult:
    document_revision_id: int
    chunk_count: int
    already_current: bool


def prepare_chunks(
    source_text: str,
    chunks: list[Chunk],
    embeddings: list[list[float] | tuple[float, ...]],
) -> tuple[PreparedChunk, ...]:
    """Validate exact source slices and pair each one with one finite vector."""
    if len(chunks) != len(embeddings):
        raise ValueError("each chunk must have exactly one embedding")
    prepared: list[PreparedChunk] = []
    previous_start = -1
    for number, (chunk, raw_embedding) in enumerate(zip(chunks, embeddings, strict=True)):
        if chunk.start < previous_start or not 0 <= chunk.start < chunk.end <= len(source_text):
            raise ValueError("chunk coordinates are invalid or unordered")
        if chunk.text != source_text[chunk.start : chunk.end]:
            raise ValueError("chunk text does not match its source coordinates")
        vector = tuple(raw_embedding)
        if not vector or len(vector) > MAX_DIMENSIONS:
            raise ValueError("embedding dimension is invalid")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("embedding contains a non-finite number")
        prepared.append(
            PreparedChunk(
                chunk_no=number,
                start_offset=chunk.start,
                end_offset=chunk.end,
                text_sha256=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                embedding=tuple(float(value) for value in vector),
            )
        )
        previous_start = chunk.start
    return tuple(prepared)


def source_revision(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"


def current_document_ids(
    connection, *, model_revision: str
) -> dict[tuple[str, str, str], int]:
    """Read what is current per document, in exactly the scope publication replaces.

    The scope is ``(workspace, channel_id, doc_path, model_revision)`` -- the
    same as the partial unique index and the retire statement, and deliberately
    **not** narrowed by ``chunker_revision``.  A reader that filtered by chunker
    would report ``None`` for a document that is current under the previous
    chunker, and the writer would then refuse every attempt to re-chunk it:
    the expectation would never be satisfiable.

    Only manifest coordinates are selected.  No source text, no vector.
    """
    revision = str(model_revision or "")
    if not revision or revision != revision.strip():
        raise ValueError("model_revision must be non-empty and trimmed")
    with connection.cursor() as cur:
        cur.execute(
            """SELECT workspace, channel_id, doc_path, id
                 FROM archive_search_document_revision
                WHERE model_revision=%s AND is_current""",
            (revision,),
        )
        rows = cur.fetchall()
    return {(str(row[0]), str(row[1]), str(row[2])): int(row[3]) for row in rows}


class EmbeddingIndexWriter:
    def __init__(self, connection) -> None:
        self.connection = connection

    def publish(
        self,
        key: DocumentKey,
        source_text: str,
        chunks: list[Chunk],
        embeddings: list[list[float] | tuple[float, ...]],
        *,
        expected_current: int | None,
    ) -> PublishResult:
        """Publish one revision, but only on top of the current row the caller saw.

        ``expected_current`` is the ``document_revision_id`` that was current for
        this document when the caller opened the canonical source -- ``None``
        when there was none.  It has no default: a caller that cannot say what
        it replaced cannot publish.

        Without it, the index has no notion of which of two revisions carries
        the fresher source.  ``source_revision`` is a hash and carries no order,
        and ``indexed_at`` records when a row was written, not when its source
        was read.  So an indexing job that read an older file and finished later
        would simply retire the newer revision and take over, with no error.
        """
        key.checked()
        if expected_current is not None and (
            isinstance(expected_current, bool)
            or not isinstance(expected_current, int)
            or expected_current <= 0
        ):
            raise ValueError("expected_current must be a positive id or None")
        if source_revision(source_text) != key.source_revision:
            raise ValueError("source text does not match source_revision")
        prepared = prepare_chunks(source_text, chunks, embeddings)
        with self.connection.transaction(), self.connection.cursor() as cur:
            return self._publish(cur, key, prepared, expected_current)

    def _publish(
        self,
        cur: _Cursor,
        key: DocumentKey,
        prepared: tuple[PreparedChunk, ...],
        expected_current: int | None,
    ) -> PublishResult:
        lock_key = "\x1f".join(
            (key.workspace, key.channel_id, key.doc_path, key.model_revision)
        )
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
        # The lock is held, so what is current now cannot move under us.  Compare
        # it before anything is written: a refusal then leaves no manifest row and
        # no chunk behind, and the revision that is current stays current.
        cur.execute(
            """SELECT id, source_revision, chunker_revision
                 FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND model_revision=%s AND is_current
                FOR UPDATE""",
            (key.workspace, key.channel_id, key.doc_path, key.model_revision),
        )
        row = cur.fetchone()
        actual_current = None if row is None else int(row[0])
        # One mismatch is harmless: what is current is already exactly the
        # revision being published.  The caller's view was old, but the document
        # already holds what the caller wanted to install, and the idempotent
        # path below writes nothing.  Refusing here would turn two workers that
        # picked up the same source into an error.
        already_published = row is not None and (
            str(row[1]),
            str(row[2]),
        ) == (key.source_revision, key.chunker_revision)
        if actual_current != expected_current and not already_published:
            raise IndexPublishError("embedding_source_superseded")
        cur.execute(
            """SELECT dimensions, state FROM search_embedding_model_revision
               WHERE revision=%s FOR SHARE""",
            (key.model_revision,),
        )
        model = cur.fetchone()
        if not model or str(model[1]) != "active":
            raise IndexPublishError("embedding_model_not_active")
        dimensions = int(model[0])
        if any(len(chunk.embedding) != dimensions for chunk in prepared):
            raise IndexPublishError("embedding_dimension_mismatch")

        identity = (
            key.workspace,
            key.channel_id,
            key.doc_path,
            key.source_revision,
            key.chunker_revision,
            key.model_revision,
        )
        cur.execute(
            """SELECT id, state, is_current, chunk_count
                 FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND source_revision=%s AND chunker_revision=%s AND model_revision=%s
                FOR UPDATE""",
            identity,
        )
        existing = cur.fetchone()
        if existing:
            return self._existing(cur, existing, prepared)

        cur.execute(
            """INSERT INTO archive_search_document_revision
                   (workspace, channel_id, doc_path, source_revision,
                    chunker_revision, model_revision, state, is_current, chunk_count)
               VALUES (%s,%s,%s,%s,%s,%s,'building',false,0)
               RETURNING id""",
            identity,
        )
        document_id = int(cur.fetchone()[0])
        cur.executemany(
            """INSERT INTO archive_search_chunk
                   (document_revision_id, chunk_no, start_offset, end_offset,
                    text_sha256, embedding)
               VALUES (%s,%s,%s,%s,%s,%s::vector)""",
            [
                (
                    document_id,
                    chunk.chunk_no,
                    chunk.start_offset,
                    chunk.end_offset,
                    chunk.text_sha256,
                    _vector_literal(chunk.embedding),
                )
                for chunk in prepared
            ],
        )
        cur.execute(
            """UPDATE archive_search_document_revision
                  SET is_current=false, state='retired'
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND model_revision=%s AND is_current AND id<>%s""",
            (key.workspace, key.channel_id, key.doc_path, key.model_revision, document_id),
        )
        cur.execute(
            """UPDATE archive_search_document_revision
                  SET state='ready', is_current=true, chunk_count=%s,
                      indexed_at=now(), error_code=''
                WHERE id=%s""",
            (len(prepared), document_id),
        )
        return PublishResult(document_id, len(prepared), False)

    @staticmethod
    def _existing(
        cur: _Cursor, existing, prepared: tuple[PreparedChunk, ...]
    ) -> PublishResult:
        document_id, state, is_current, count = existing
        if str(state) != "ready" or not bool(is_current) or int(count) != len(prepared):
            raise IndexPublishError("embedding_revision_conflict")
        cur.execute(
            """SELECT chunk_no, start_offset, end_offset, text_sha256
                 FROM archive_search_chunk
                WHERE document_revision_id=%s ORDER BY chunk_no""",
            (document_id,),
        )
        actual = [tuple(row) for row in cur.fetchall()]
        expected = [
            (chunk.chunk_no, chunk.start_offset, chunk.end_offset, chunk.text_sha256)
            for chunk in prepared
        ]
        if actual != expected:
            raise IndexPublishError("embedding_revision_conflict")
        return PublishResult(int(document_id), len(prepared), True)
