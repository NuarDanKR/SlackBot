"""Fail-closed pgvector candidate lookup for B-12.

The database result is never answer evidence. It is only an immutable source
coordinate. The caller must reopen ``doc_path`` under the request's current ACL
and verify ``source_revision`` before using any text.

This module does not choose a model, call a provider, or widen permissions. A
missing database returns ``None`` so the existing keyword path remains usable;
an empty allowed-channel set returns an empty list and never queries broadly.
"""
from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

log = logging.getLogger("tybot.embedding_search")

MAX_DIMENSIONS = 16_000
MAX_CANDIDATES = 500
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class SearchScope:
    workspace: str
    channel_ids: frozenset[str]

    @classmethod
    def checked(cls, workspace: str, channel_ids: Iterable[str]) -> SearchScope:
        key = str(workspace or "").strip()
        channels = frozenset(str(value or "").strip() for value in channel_ids)
        if not key:
            raise ValueError("workspace is required")
        if "" in channels or "*" in channels:
            raise ValueError("channel_ids must be explicit")
        return cls(workspace=key, channel_ids=channels)


@dataclass(frozen=True)
class VectorCandidate:
    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str
    chunker_revision: str
    model_revision: str
    chunk_no: int
    start_offset: int
    end_offset: int
    text_sha256: str
    distance: float


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:  # noqa: BLE001 - keyword search remains available
        log.warning("벡터 검색 DB에 붙지 못했습니다: %s", type(exc).__name__)
        return None


def _query_vector(values: Iterable[float]) -> str:
    vector = tuple(values)
    if not vector or len(vector) > MAX_DIMENSIONS:
        raise ValueError("query embedding dimension is invalid")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in vector
    ):
        raise ValueError("query embedding contains a non-finite number")
    normalized = tuple(float(value) for value in vector)
    # pgvector's Python adapter is optional. A bound text value cast by SQL
    # keeps the query parameterized without registering a process-global codec.
    literal = "[" + ",".join(format(value, ".17g") for value in normalized) + "]"
    return literal


def candidates(
    query_embedding: Iterable[float],
    *,
    scope: SearchScope,
    model_revision: str,
    limit: int = 100,
    connect: Callable[[], object | None] | None = None,
) -> list[VectorCandidate] | None:
    """Return only current, ready candidates inside an already-approved scope.

    ``None`` means the vector index could not be read. ``[]`` means the caller
    had no allowed channels or the active index had no candidate. Invalid scope
    and revision inputs raise instead of silently broadening the query.
    """
    if (
        not isinstance(scope, SearchScope)
        or not scope.workspace
        or scope.workspace != scope.workspace.strip()
    ):
        raise ValueError("a checked search scope is required")
    if not scope.channel_ids:
        return []
    if any(
        not channel or channel == "*" or channel != channel.strip()
        for channel in scope.channel_ids
    ):
        raise ValueError("channel_ids must be explicit")
    revision = str(model_revision or "").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("model_revision is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")
    literal = _query_vector(query_embedding)
    connector = connect or _connect
    conn = connector()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH query_vector AS (
                    SELECT %(embedding)s::vector AS embedding
                ), ranked AS (
                    SELECT d.workspace, d.channel_id, d.doc_path,
                           d.source_revision, d.chunker_revision,
                           d.model_revision, c.chunk_no, c.start_offset,
                           c.end_offset, c.text_sha256,
                           CASE
                             WHEN vector_dims(c.embedding) = m.dimensions
                              AND vector_dims(q.embedding) = m.dimensions
                             THEN c.embedding <=> q.embedding
                           END AS distance
                      FROM archive_search_document_revision d
                      JOIN archive_search_chunk c
                        ON c.document_revision_id = d.id
                      JOIN search_embedding_model_revision m
                        ON m.revision = d.model_revision
                      CROSS JOIN query_vector q
                     WHERE d.workspace = %(workspace)s
                       AND d.channel_id = ANY(%(channel_ids)s)
                       AND d.model_revision = %(model_revision)s
                       AND d.state = 'ready'
                       AND d.is_current
                       AND m.state = 'active'
                )
                SELECT workspace, channel_id, doc_path, source_revision,
                       chunker_revision, model_revision, chunk_no,
                       start_offset, end_offset, text_sha256, distance
                  FROM ranked
                 WHERE distance IS NOT NULL
                 ORDER BY distance, doc_path, chunk_no
                 LIMIT %(limit)s
                """,
                {
                    "embedding": literal,
                    "workspace": scope.workspace,
                    "channel_ids": sorted(scope.channel_ids),
                    "model_revision": revision,
                    "limit": limit,
                },
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - keyword search remains available
        log.warning("벡터 검색 조회 실패: %s", type(exc).__name__)
        return None
    return [
        VectorCandidate(
            workspace=str(row["workspace"]),
            channel_id=str(row["channel_id"]),
            doc_path=str(row["doc_path"]),
            source_revision=str(row["source_revision"]),
            chunker_revision=str(row["chunker_revision"]),
            model_revision=str(row["model_revision"]),
            chunk_no=int(row["chunk_no"]),
            start_offset=int(row["start_offset"]),
            end_offset=int(row["end_offset"]),
            text_sha256=str(row["text_sha256"]),
            distance=float(row["distance"]),
        )
        for row in rows
    ]
