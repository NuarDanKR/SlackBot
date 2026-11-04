"""Resumable B-12 index runner using current DB revisions as checkpoints.

There is deliberately no second progress ledger.  A rerun compares a complete
canonical-source inventory with ``ready/current`` manifests and sends only
missing or changed sources to the bounded coordinator.  Coordinate migrations
retire their old row only after the exact replacement is current.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tybot.archive import chunking
from tybot.archive.embedding_source import (
    VectorSource,
    source_chunker_revision,
)
from tybot.embedding_coordinate_reconcile import (
    ReconciliationError,
    current_documents,
    plan_reconciliation,
    retire_migration,
)
from tybot.embedding_index_job import (
    Embedder,
    IndexAuthorization,
    IndexBatchResult,
    IndexLimits,
    Publisher,
    run_index_batch,
)


@dataclass(frozen=True)
class ReconciledIndexResult:
    inventory_count: int
    unchanged_count: int
    requested_count: int
    unresolved_count: int
    migration_count: int
    migrations_retired: int
    migrations_deferred: int
    migrations_failed: int
    batch: IndexBatchResult


def run_reconciled_index(
    connection,
    sources: Iterable[VectorSource],
    *,
    scan_complete: bool,
    model_revision: str,
    authorize: Callable,
    embedder: Embedder,
    writer: Publisher,
    limits: IndexLimits | None = None,
    spec: chunking.ChunkSpec = chunking.DEFAULT_SPEC,
) -> ReconciledIndexResult:
    """Reconcile, publish missing revisions, then retire safe migrations."""
    inventory = tuple(sources)
    chunker_revision = source_chunker_revision(spec.plan)
    current = current_documents(
        connection,
        model_revision=model_revision,
        chunker_revision=chunker_revision,
    )
    plan = plan_reconciliation(
        current,
        inventory,
        model_revision=model_revision,
        chunker_revision=chunker_revision,
        scan_complete=scan_complete,
    )
    by_identity = {
        (
            source.workspace,
            source.channel_id,
            source.doc_path,
            source.source_revision,
        ): source
        for source in inventory
    }
    requested: list[VectorSource] = []
    for target in plan.reindex:
        source = by_identity.get(
            (
                target.workspace,
                target.channel_id,
                target.doc_path,
                target.source_revision,
            )
        )
        if source is None:
            raise ReconciliationError("reindex_source_missing")
        requested.append(source)

    batch = run_index_batch(
        requested,
        model_revision=model_revision,
        authorize=authorize,
        embedder=embedder,
        writer=writer,
        limits=limits,
        spec=spec,
    )
    retired = deferred = failed = 0
    for migration in plan.migrations:
        try:
            changed = retire_migration(connection, migration)
        except ReconciliationError as exc:
            if str(exc) == "replacement_not_current":
                deferred += 1
            else:
                failed += 1
        else:
            if changed:
                retired += 1

    return ReconciledIndexResult(
        inventory_count=len(inventory),
        unchanged_count=len(plan.unchanged_document_ids),
        requested_count=len(requested),
        unresolved_count=len(plan.unresolved_document_ids),
        migration_count=len(plan.migrations),
        migrations_retired=retired,
        migrations_deferred=deferred,
        migrations_failed=failed,
        batch=batch,
    )


__all__ = [
    "IndexAuthorization",
    "ReconciledIndexResult",
    "run_reconciled_index",
]
