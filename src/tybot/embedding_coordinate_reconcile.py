"""Detect and retire obsolete B-12 vector coordinates without guessing.

Channel identifiers and archive directory names can change while the canonical
body stays the same.  A stale row is retired only after a complete source scan
finds one unambiguous replacement and that replacement has been published as a
current, ready revision.  Missing or ambiguous sources remain untouched; the
answer-time resolver will continue to reject them fail-closed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from tybot.embedding_index_store import DocumentKey

_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHUNKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReconciliationError(ValueError):
    """The inventory is not safe enough to plan destructive retirement."""


class SourceIdentity(Protocol):
    """Body-free canonical source identity used during reconciliation."""

    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str


@dataclass(frozen=True)
class CurrentDocument:
    document_revision_id: int
    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str
    chunker_revision: str
    model_revision: str


@dataclass(frozen=True)
class TargetDocument:
    workspace: str
    channel_id: str
    doc_path: str
    source_revision: str
    chunker_revision: str
    model_revision: str

    def key(self) -> DocumentKey:
        return DocumentKey(
            workspace=self.workspace,
            channel_id=self.channel_id,
            doc_path=self.doc_path,
            source_revision=self.source_revision,
            chunker_revision=self.chunker_revision,
            model_revision=self.model_revision,
        ).checked()


@dataclass(frozen=True)
class CoordinateMigration:
    old_document_revision_id: int
    target: TargetDocument


@dataclass(frozen=True)
class ReconciliationPlan:
    unchanged_document_ids: tuple[int, ...]
    reindex: tuple[TargetDocument, ...]
    migrations: tuple[CoordinateMigration, ...]
    unresolved_document_ids: tuple[int, ...]


def current_documents(
    connection,
    *,
    model_revision: str,
    chunker_revision: str,
) -> tuple[CurrentDocument, ...]:
    """Read current manifests only; source text and vectors are never selected."""
    _check_revisions(model_revision, chunker_revision)
    with connection.cursor() as cur:
        cur.execute(
            """SELECT id, workspace, channel_id, doc_path, source_revision,
                      chunker_revision, model_revision
                 FROM archive_search_document_revision
                WHERE model_revision=%s AND chunker_revision=%s
                  AND state='ready' AND is_current
                ORDER BY workspace, channel_id, doc_path""",
            (model_revision, chunker_revision),
        )
        rows = cur.fetchall()
    return tuple(
        CurrentDocument(
            document_revision_id=int(row[0]),
            workspace=str(row[1]),
            channel_id=str(row[2]),
            doc_path=str(row[3]),
            source_revision=str(row[4]),
            chunker_revision=str(row[5]),
            model_revision=str(row[6]),
        )
        for row in rows
    )


def plan_reconciliation(
    current: Iterable[CurrentDocument],
    sources: Iterable[SourceIdentity],
    *,
    model_revision: str,
    chunker_revision: str,
    scan_complete: bool,
) -> ReconciliationPlan:
    """Compare a complete canonical inventory with current index manifests."""
    if not scan_complete:
        raise ReconciliationError("source_scan_incomplete")
    _check_revisions(model_revision, chunker_revision)
    current_rows = tuple(current)
    source_rows = tuple(sources)
    _validate_current(current_rows, model_revision, chunker_revision)

    by_coordinate: dict[tuple[str, str, str], CurrentDocument] = {}
    for row in current_rows:
        coordinate = (row.workspace, row.channel_id, row.doc_path)
        if coordinate in by_coordinate:
            raise ReconciliationError("duplicate_current_coordinate")
        by_coordinate[coordinate] = row

    source_by_coordinate: dict[tuple[str, str, str], SourceIdentity] = {}
    for source in source_rows:
        target = _target(source, model_revision, chunker_revision)
        coordinate = (target.workspace, target.channel_id, target.doc_path)
        if coordinate in source_by_coordinate:
            raise ReconciliationError("duplicate_source_coordinate")
        source_by_coordinate[coordinate] = source

    unchanged: list[int] = []
    reindex: dict[tuple[str, str, str], TargetDocument] = {}
    stale: list[CurrentDocument] = []
    new_sources: list[SourceIdentity] = []

    for coordinate, row in by_coordinate.items():
        source = source_by_coordinate.get(coordinate)
        if source is None:
            stale.append(row)
        elif source.source_revision == row.source_revision:
            unchanged.append(row.document_revision_id)
        else:
            reindex[coordinate] = _target(source, model_revision, chunker_revision)

    for coordinate, source in source_by_coordinate.items():
        if coordinate not in by_coordinate:
            new_sources.append(source)
            reindex[coordinate] = _target(source, model_revision, chunker_revision)

    stale_groups: dict[tuple[str, str, str, str], list[CurrentDocument]] = defaultdict(list)
    source_groups: dict[tuple[str, str, str, str], list[SourceIdentity]] = defaultdict(list)
    for row in stale:
        fingerprint = _fingerprint(row.workspace, row.doc_path, row.source_revision)
        if fingerprint is not None:
            stale_groups[fingerprint].append(row)
    for source in new_sources:
        fingerprint = _fingerprint(
            source.workspace, source.doc_path, source.source_revision
        )
        if fingerprint is not None:
            source_groups[fingerprint].append(source)

    migrated_ids: set[int] = set()
    migrations: list[CoordinateMigration] = []
    for fingerprint in sorted(stale_groups.keys() & source_groups.keys()):
        old = stale_groups[fingerprint]
        new = source_groups[fingerprint]
        if len(old) != 1 or len(new) != 1:
            continue
        target = _target(new[0], model_revision, chunker_revision)
        migrations.append(CoordinateMigration(old[0].document_revision_id, target))
        migrated_ids.add(old[0].document_revision_id)

    return ReconciliationPlan(
        unchanged_document_ids=tuple(sorted(unchanged)),
        reindex=tuple(reindex[key] for key in sorted(reindex)),
        migrations=tuple(sorted(migrations, key=lambda item: item.old_document_revision_id)),
        unresolved_document_ids=tuple(
            sorted(row.document_revision_id for row in stale if row.document_revision_id not in migrated_ids)
        ),
    )


def retire_migration(connection, migration: CoordinateMigration) -> bool:
    """Retire the old coordinate only after its exact replacement is current."""
    target = migration.target
    target.key()
    with connection.transaction(), connection.cursor() as cur:
        cur.execute(
            """SELECT workspace, channel_id, doc_path, source_revision,
                      chunker_revision, model_revision, state, is_current
                 FROM archive_search_document_revision
                WHERE id=%s FOR UPDATE""",
            (migration.old_document_revision_id,),
        )
        old = cur.fetchone()
        if old is None:
            raise ReconciliationError("old_document_missing")
        if (
            str(old[0]) != target.workspace
            or str(old[3]) != target.source_revision
            or str(old[4]) != target.chunker_revision
            or str(old[5]) != target.model_revision
        ):
            raise ReconciliationError("migration_identity_mismatch")
        if (str(old[1]), str(old[2])) == (target.channel_id, target.doc_path):
            raise ReconciliationError("migration_coordinate_unchanged")

        cur.execute(
            """SELECT id FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND source_revision=%s AND chunker_revision=%s
                  AND model_revision=%s AND state='ready' AND is_current
                FOR SHARE""",
            (
                target.workspace,
                target.channel_id,
                target.doc_path,
                target.source_revision,
                target.chunker_revision,
                target.model_revision,
            ),
        )
        replacement = cur.fetchone()
        if replacement is None:
            raise ReconciliationError("replacement_not_current")
        if not bool(old[7]) and str(old[6]) == "retired":
            return False
        if not bool(old[7]) or str(old[6]) != "ready":
            raise ReconciliationError("old_document_not_current")
        cur.execute(
            """UPDATE archive_search_document_revision
                  SET state='retired', is_current=false
                WHERE id=%s AND state='ready' AND is_current""",
            (migration.old_document_revision_id,),
        )
        return True


def _target(
    source: SourceIdentity, model_revision: str, chunker_revision: str
) -> TargetDocument:
    target = TargetDocument(
        workspace=source.workspace,
        channel_id=source.channel_id,
        doc_path=source.doc_path,
        source_revision=source.source_revision,
        chunker_revision=chunker_revision,
        model_revision=model_revision,
    )
    try:
        target.key()
    except ValueError as exc:
        raise ReconciliationError("source_coordinate_invalid") from exc
    return target


def _check_revisions(model_revision: str, chunker_revision: str) -> None:
    if not _REVISION_RE.fullmatch(str(model_revision or "")):
        raise ReconciliationError("model_revision_invalid")
    if not _CHUNKER_RE.fullmatch(str(chunker_revision or "")):
        raise ReconciliationError("chunker_revision_invalid")


def _validate_current(
    rows: tuple[CurrentDocument, ...], model_revision: str, chunker_revision: str
) -> None:
    ids: set[int] = set()
    for row in rows:
        if row.document_revision_id <= 0 or row.document_revision_id in ids:
            raise ReconciliationError("current_document_id_invalid")
        ids.add(row.document_revision_id)
        if row.model_revision != model_revision or row.chunker_revision != chunker_revision:
            raise ReconciliationError("current_revision_scope_mismatch")
        if (
            not row.workspace
            or not row.channel_id
            or not row.doc_path
            or not _SHA256_RE.fullmatch(row.source_revision)
        ):
            raise ReconciliationError("current_document_invalid")


def _fingerprint(
    workspace: str, doc_path: str, source_revision: str
) -> tuple[str, str, str, str] | None:
    """Return a rename-stable key, never falling back to body hash alone."""
    parts = PurePosixPath(doc_path).parts
    if not parts or not _SHA256_RE.fullmatch(source_revision):
        return None
    if "attachments" in parts:
        index = parts.index("attachments")
        if index + 2 >= len(parts):
            return None
        return (workspace, "attachment", f"{parts[index + 1]}/{parts[-1]}", source_revision)
    if len(parts) >= 2 and parts[-2] == "raw":
        return (workspace, "raw", parts[-1], source_revision)
    return None
