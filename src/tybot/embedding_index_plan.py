"""Build a verified embedding-index plan from one canonical archive source.

This boundary does not call a model or write to PostgreSQL.  It binds the
canonical-source revision, chunking revision, and embedding-model revision
before any text leaves the process.  Callers may then embed ``inputs()`` and
publish the result through the existing atomic writer.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from tybot.archive import chunking
from tybot.archive.embedding_source import (
    ATTACHMENT,
    CHANNEL,
    VectorSource,
    source_chunker_revision,
    source_revision,
)
from tybot.embedding_index_store import DocumentKey, PublishResult


class IndexPlanError(ValueError):
    """A canonical source cannot safely become an embedding request."""


class _Publisher(Protocol):
    def publish(
        self,
        key: DocumentKey,
        source_text: str,
        chunks: list[chunking.Chunk],
        embeddings: list[list[float] | tuple[float, ...]],
    ) -> PublishResult: ...


@dataclass(frozen=True)
class EmbeddingInput:
    """One model input; its body is deliberately absent from ``repr``."""

    chunk_no: int
    text_sha256: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class IndexPlan:
    """A checked source-to-chunk plan ready for embedding and publication."""

    key: DocumentKey
    kind: str
    char_count: int
    chunk_count: int
    _source_text: str = field(repr=False)
    _chunks: tuple[chunking.Chunk, ...] = field(repr=False)

    def inputs(self) -> tuple[EmbeddingInput, ...]:
        return tuple(
            EmbeddingInput(
                chunk_no=number,
                text_sha256=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                text=chunk.text,
            )
            for number, chunk in enumerate(self._chunks)
        )

    def publish(
        self,
        writer: _Publisher,
        embeddings: list[list[float] | tuple[float, ...]],
    ) -> PublishResult:
        """Publish exactly the source and coordinates that were planned."""
        return writer.publish(
            self.key,
            self._source_text,
            list(self._chunks),
            embeddings,
        )


def plan_source(
    source: VectorSource,
    *,
    model_revision: str,
    spec: chunking.ChunkSpec = chunking.DEFAULT_SPEC,
) -> IndexPlan:
    """Validate and split one canonical, currently visible source.

    DM sources remain disabled until the index schema records their kind and
    owner scope.  Canonical-source loading already rejects them; this second
    check prevents a caller from constructing a ``VectorSource`` by hand and
    bypassing that boundary.
    """
    if source.kind not in {CHANNEL, ATTACHMENT}:
        raise IndexPlanError("embedding_source_kind_not_enabled")
    if not source.text or not source.text.strip():
        raise IndexPlanError("embedding_source_empty")
    if "\x00" in source.text or "\r" in source.text:
        raise IndexPlanError("embedding_source_not_canonical")
    if source_revision(source.text) != source.source_revision:
        raise IndexPlanError("embedding_source_revision_mismatch")

    key = DocumentKey(
        workspace=source.workspace,
        channel_id=source.channel_id,
        doc_path=source.doc_path,
        source_revision=source.source_revision,
        chunker_revision=source_chunker_revision(spec.plan),
        model_revision=model_revision,
    )
    try:
        key.checked()
    except ValueError as exc:
        raise IndexPlanError("embedding_document_identity_invalid") from exc

    chunks = chunking.split(source.text, spec)
    if not chunks:
        raise IndexPlanError("embedding_chunks_empty")
    _verify_chunks(source.text, chunks, spec)
    return IndexPlan(
        key=key,
        kind=source.kind,
        char_count=len(source.text),
        chunk_count=len(chunks),
        _source_text=source.text,
        _chunks=tuple(chunks),
    )


def _verify_chunks(
    source_text: str,
    chunks: list[chunking.Chunk],
    spec: chunking.ChunkSpec,
) -> None:
    previous_start = -1
    for chunk in chunks:
        if (
            chunk.start < previous_start
            or not 0 <= chunk.start < chunk.end <= len(source_text)
            or len(chunk.text) > spec.max_chars
            or chunk.text != source_text[chunk.start : chunk.end]
        ):
            raise IndexPlanError("embedding_chunk_contract_broken")
        previous_start = chunk.start

    gaps = chunking.uncovered(source_text, chunks)
    if any(source_text[start:end].strip() for start, end in gaps):
        raise IndexPlanError("embedding_chunk_coverage_incomplete")
    covered = source_text[chunks[0].start : chunks[-1].end]
    if chunking.restore(source_text, chunks) != covered:
        raise IndexPlanError("embedding_chunk_restore_failed")
