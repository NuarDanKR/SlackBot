"""Read-only coverage inventory for an old archive and a new Archiver root."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .store import ArchiveStore, is_synthetic_channel_id


@dataclass(frozen=True)
class RootCounts:
    documents: int
    broken_documents: int
    raw_lines: int
    coordinates: int
    uncoordinated_lines: int
    unidentified_channel_documents: int
    misplaced_dm_documents: int


@dataclass(frozen=True)
class ReadInventory:
    legacy: RootCounts
    archiver: RootCounts
    shared_coordinates: int
    legacy_only_coordinates: int
    archiver_only_coordinates: int

    def to_json(self) -> dict:
        return asdict(self)


def _collect(root: Path) -> tuple[RootCounts, set[tuple[str, str, str]]]:
    store = ArchiveStore(root)
    docs = store.source_docs()
    coordinates: set[tuple[str, str, str]] = set()
    raw_lines = uncoordinated = unidentified = misplaced_dm = 0
    for doc in docs:
        if doc.dm_user:
            misplaced_dm += 1
            continue
        channel_id = doc.channel_id or ""
        if not channel_id or is_synthetic_channel_id(channel_id):
            unidentified += 1
        for line in doc.raw_lines:
            raw_lines += 1
            if channel_id and not is_synthetic_channel_id(channel_id) and line.message_ts:
                coordinates.add((doc.workspace, channel_id, line.message_ts))
            else:
                uncoordinated += 1
    counts = RootCounts(
        documents=len(docs), broken_documents=len(store.broken()),
        raw_lines=raw_lines, coordinates=len(coordinates),
        uncoordinated_lines=uncoordinated,
        unidentified_channel_documents=unidentified,
        misplaced_dm_documents=misplaced_dm,
    )
    return counts, coordinates


def inventory(legacy_root: Path | str, archiver_root: Path | str) -> ReadInventory:
    """Count channel evidence without moving data or exposing message content."""
    legacy = Path(legacy_root).resolve()
    archiver = Path(archiver_root).resolve()
    if (not legacy.is_dir() or not archiver.is_dir()
            or legacy == archiver or legacy in archiver.parents or archiver in legacy.parents):
        raise ValueError("archive roots must be separate existing directories")
    old_counts, old_keys = _collect(legacy)
    new_counts, new_keys = _collect(archiver)
    return ReadInventory(
        legacy=old_counts, archiver=new_counts,
        shared_coordinates=len(old_keys & new_keys),
        legacy_only_coordinates=len(old_keys - new_keys),
        archiver_only_coordinates=len(new_keys - old_keys),
    )
