"""Explicit assembly boundary for a B-12 embedding index run.

Nothing in this module discovers credentials, enables a provider, or schedules
itself.  The caller must supply the archive store, database connection, model
revision, provider map, and whether the source inventory is complete.
"""
from __future__ import annotations

from collections.abc import Mapping

from tybot.archive.embedding_source import iter_index_sources
from tybot.embedding_gateway import GatewayEmbedder, db_bindings
from tybot.embedding_index_job import IndexLimits
from tybot.embedding_index_policy import authorize_index_request
from tybot.embedding_index_runner import ReconciledIndexResult, run_reconciled_index
from tybot.embedding_index_store import EmbeddingIndexWriter
from tybot.gateway.embedding import EmbeddingProvider


def run_embedding_index(
    connection,
    store,
    *,
    model_revision: str,
    providers: Mapping[str, EmbeddingProvider],
    scan_complete: bool,
    limits: IndexLimits | None = None,
    timeout_seconds: float = 60.0,
) -> ReconciledIndexResult:
    """Run one bounded reconciliation with every unsafe choice explicit."""
    if not isinstance(providers, Mapping):
        raise TypeError("providers must be a mapping")
    if not isinstance(scan_complete, bool):
        raise TypeError("scan_complete must be bool")
    embedder = GatewayEmbedder(
        bindings=db_bindings(connection),
        providers=providers,
        timeout_seconds=timeout_seconds,
    )
    return run_reconciled_index(
        connection,
        iter_index_sources(store),
        scan_complete=scan_complete,
        model_revision=model_revision,
        authorize=lambda request: authorize_index_request(connection, request),
        embedder=embedder,
        writer=EmbeddingIndexWriter(connection),
        limits=limits,
    )


__all__ = ["run_embedding_index"]
