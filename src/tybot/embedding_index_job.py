"""Bounded, policy-gated coordinator for building the B-12 vector index.

This module does not choose a provider, discover archive files, open a DB, or
schedule itself.  Callers inject canonical sources, an authorization decision,
an embedding service, and the atomic writer.  External text transfer requires
an explicit per-source approval before the embedding service is called.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from tybot.archive import chunking
from tybot.archive.embedding_source import VectorSource
from tybot.embedding_index_plan import EmbeddingInput, IndexPlanError, plan_source
from tybot.embedding_index_store import MAX_DIMENSIONS, PublishResult

LOCAL = "local"
EXTERNAL = "external"
MAX_DOCUMENTS = 1_000
MAX_CHARS = 100_000_000
MAX_CHUNKS = 100_000


@dataclass(frozen=True)
class IndexRequest:
    workspace: str
    channel_id: str
    doc_path: str = field(repr=False)
    source_revision: str
    kind: str
    char_count: int
    model_revision: str


@dataclass(frozen=True)
class IndexAuthorization:
    allowed: bool
    model_revision: str
    policy_revision: str
    transport: str
    external_transfer_approved: bool = False

    def checked(self, expected_model: str) -> IndexAuthorization:
        if self.model_revision != expected_model:
            raise ValueError("authorization_model_mismatch")
        if not self.policy_revision or self.policy_revision != self.policy_revision.strip():
            raise ValueError("authorization_policy_invalid")
        if self.transport not in {LOCAL, EXTERNAL}:
            raise ValueError("authorization_transport_invalid")
        if self.transport == EXTERNAL and not self.external_transfer_approved:
            raise ValueError("external_transfer_not_approved")
        return self


@dataclass(frozen=True)
class EmbeddingBatch:
    model_revision: str
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        revision = str(self.model_revision or "")
        if not revision or revision != revision.strip():
            raise ValueError("embedding_model_revision_invalid")
        dimensions = {len(vector) for vector in self.vectors}
        if (
            not self.vectors
            or dimensions == {0}
            or len(dimensions) != 1
            or next(iter(dimensions)) > MAX_DIMENSIONS
        ):
            raise ValueError("embedding_vectors_invalid")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for vector in self.vectors
            for value in vector
        ):
            raise ValueError("embedding_vectors_invalid")
        if self.cost_usd is not None:
            _nonnegative_finite("cost_usd", self.cost_usd)

    @classmethod
    def checked(
        cls,
        model_revision: str,
        vectors: Sequence[Sequence[float]],
        *,
        cost_usd: float | None,
    ) -> EmbeddingBatch:
        revision = str(model_revision or "")
        if not revision or revision != revision.strip():
            raise ValueError("embedding_model_revision_invalid")
        prepared = tuple(tuple(vector) for vector in vectors)
        if cost_usd is not None:
            _nonnegative_finite("cost_usd", cost_usd)
        return cls(revision, prepared, None if cost_usd is None else float(cost_usd))


class Embedder(Protocol):
    def embed(
        self,
        inputs: tuple[EmbeddingInput, ...],
        *,
        model_revision: str,
        policy_revision: str,
    ) -> EmbeddingBatch: ...


class Publisher(Protocol):
    def publish(self, key, source_text, chunks, embeddings) -> PublishResult: ...


@dataclass(frozen=True)
class IndexLimits:
    max_documents: int = 100
    max_total_chars: int = 5_000_000
    max_total_chunks: int = 10_000
    max_calls: int = 100
    max_chars_per_document: int = 1_000_000

    def __post_init__(self) -> None:
        _positive_int("max_documents", self.max_documents, MAX_DOCUMENTS)
        _positive_int("max_total_chars", self.max_total_chars, MAX_CHARS)
        _positive_int("max_total_chunks", self.max_total_chunks, MAX_CHUNKS)
        _positive_int("max_calls", self.max_calls, MAX_DOCUMENTS)
        _positive_int("max_chars_per_document", self.max_chars_per_document, MAX_CHARS)


@dataclass(frozen=True)
class IndexBatchResult:
    scanned: int
    planned: int
    published: int
    already_current: int
    embed_calls: int
    embedded_chars: int
    embedded_chunks: int
    cost_usd: float | None
    stopped_reason: str
    outcomes: tuple[tuple[str, int], ...]


def run_index_batch(
    sources: Iterable[VectorSource],
    *,
    model_revision: str,
    authorize: Callable[[IndexRequest], IndexAuthorization],
    embedder: Embedder,
    writer: Publisher,
    limits: IndexLimits | None = None,
    spec: chunking.ChunkSpec = chunking.DEFAULT_SPEC,
) -> IndexBatchResult:
    """Index a bounded source stream once, with no retries or model fallback."""
    revision = str(model_revision or "")
    if not revision or revision != revision.strip():
        raise ValueError("model_revision must be non-empty and trimmed")
    limits = limits or IndexLimits()
    counts: Counter[str] = Counter()
    scanned = planned = published = current = calls = chars = chunks = 0
    costs: list[float | None] = []
    stopped = ""

    for source in sources:
        if scanned >= limits.max_documents:
            stopped = "document_limit_reached"
            break
        scanned += 1
        if not isinstance(source, VectorSource):
            counts["source_invalid"] += 1
            continue
        if len(source.text) > limits.max_chars_per_document:
            counts["source_too_large"] += 1
            continue
        try:
            plan = plan_source(source, model_revision=revision, spec=spec)
        except (IndexPlanError, ValueError):
            counts["plan_rejected"] += 1
            continue
        if chars + plan.char_count > limits.max_total_chars:
            stopped = "character_limit_reached"
            break
        if chunks + plan.chunk_count > limits.max_total_chunks:
            stopped = "chunk_limit_reached"
            break
        if calls >= limits.max_calls:
            stopped = "call_limit_reached"
            break
        planned += 1
        request = IndexRequest(
            workspace=source.workspace,
            channel_id=source.channel_id,
            doc_path=source.doc_path,
            source_revision=source.source_revision,
            kind=source.kind,
            char_count=plan.char_count,
            model_revision=revision,
        )
        try:
            approval = authorize(request)
            if not isinstance(approval, IndexAuthorization):
                raise ValueError("authorization_invalid_result")
            approval.checked(revision)
        except Exception:  # noqa: BLE001 - fixed code; policy details stay in owner store
            counts["authorization_rejected"] += 1
            continue
        if not approval.allowed:
            counts["authorization_denied"] += 1
            continue

        calls += 1
        chars += plan.char_count
        chunks += plan.chunk_count
        try:
            batch = embedder.embed(
                plan.inputs(),
                model_revision=revision,
                policy_revision=approval.policy_revision,
            )
        except Exception:  # noqa: BLE001 - provider response/body must not escape
            costs.append(None)
            counts["embedding_failed"] += 1
            continue
        if (
            not isinstance(batch, EmbeddingBatch)
            or batch.model_revision != revision
            or len(batch.vectors) != plan.chunk_count
        ):
            costs.append(None if not isinstance(batch, EmbeddingBatch) else batch.cost_usd)
            counts["embedding_invalid_result"] += 1
            continue
        costs.append(batch.cost_usd)
        try:
            result = plan.publish(writer, [list(vector) for vector in batch.vectors])
        except Exception:  # noqa: BLE001 - DB details and source coordinates stay private
            counts["publish_failed"] += 1
            continue
        if not isinstance(result, PublishResult):
            counts["publish_invalid_result"] += 1
            continue
        if result.already_current:
            current += 1
            counts["already_current"] += 1
        else:
            published += 1
            counts["published"] += 1

    known_costs = [cost for cost in costs if cost is not None]
    total_cost = None if len(known_costs) != len(costs) else sum(known_costs)
    return IndexBatchResult(
        scanned=scanned,
        planned=planned,
        published=published,
        already_current=current,
        embed_calls=calls,
        embedded_chars=chars,
        embedded_chunks=chunks,
        cost_usd=total_cost,
        stopped_reason=stopped,
        outcomes=tuple(sorted(counts.items())),
    )


def _positive_int(name: str, value: object, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _nonnegative_finite(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be a non-negative finite number")
