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
from tybot.embedding_index_store import current_document_ids


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


@dataclass(frozen=True)
class _SourceIdentity:
    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str


def run_reconciled_index(
    connection,
    source_factory: Callable[[], Iterable[VectorSource]],
    *,
    scan_complete: bool,
    model_revision: str,
    authorize: Callable,
    embedder: Embedder,
    writer: Publisher,
    limits: IndexLimits | None = None,
    spec: chunking.ChunkSpec = chunking.DEFAULT_SPEC,
) -> ReconciledIndexResult:
    """Reconcile metadata, then stream requested bodies in a second scan.

    A factory is required rather than a one-shot iterable because retaining all
    canonical bodies made peak memory proportional to the whole archive.  The
    first scan keeps only identity metadata; the bounded coordinator consumes
    requested bodies directly from a fresh second scan.
    """
    if not callable(source_factory):
        raise TypeError("source_factory must be callable")
    inventory = tuple(_source_identity(source) for source in source_factory())
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
    requested_identities = {
        (
            target.workspace,
            target.channel_id,
            target.doc_path,
            target.source_revision,
        )
        for target in plan.reindex
    }

    # Read what is current **before** embedding starts, and in the full scope
    # publication replaces -- `current_documents` above is narrowed to this
    # chunker revision, so it cannot answer "what will this publication retire"
    # when the chunker itself changed.
    observed = current_document_ids(connection, model_revision=model_revision)
    batch = run_index_batch(
        _requested_sources(source_factory, requested_identities),
        model_revision=model_revision,
        authorize=authorize,
        embedder=embedder,
        writer=writer,
        expected_current=observed,
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
        requested_count=len(requested_identities),
        unresolved_count=len(plan.unresolved_document_ids),
        migration_count=len(plan.migrations),
        migrations_retired=retired,
        migrations_deferred=deferred,
        migrations_failed=failed,
        batch=batch,
    )


def _source_identity(source: VectorSource) -> _SourceIdentity:
    if not isinstance(source, VectorSource):
        raise ReconciliationError("source_invalid")
    return _SourceIdentity(
        workspace=source.workspace,
        channel_id=source.channel_id,
        doc_path=source.doc_path,
        source_revision=source.source_revision,
    )


def _requested_sources(
    source_factory: Callable[[], Iterable[VectorSource]],
    requested: set[tuple[str, str, str, str]],
) -> Iterable[VectorSource]:
    seen: set[tuple[str, str, str, str]] = set()
    for source in source_factory():
        identity = _source_identity(source)
        key = (
            identity.workspace,
            identity.channel_id,
            identity.doc_path,
            identity.source_revision,
        )
        if key not in requested:
            continue
        if key in seen:
            raise ReconciliationError("duplicate_source_identity")
        seen.add(key)
        yield source


__all__ = [
    "IndexAuthorization",
    "ReconciledIndexResult",
    "run_reconciled_index",
]
