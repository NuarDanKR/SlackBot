"""Revalidate vector candidates against currently visible source text.

The opener is the security boundary: it must return text only after current ACL
and revision filtering.  This module never opens ``doc_path`` itself, because
doing so could bypass ``ArchiveStore.visible_docs()`` and resurrect edited or
deleted lines.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from .embedding_search import MAX_CANDIDATES, VectorCandidate


@dataclass(frozen=True)
class VisibleVectorSource:
    workspace: str
    channel_id: str
    doc_path: str
    text: str


@dataclass(frozen=True)
class ResolvedVectorChunk:
    candidate: VectorCandidate
    text: str


@dataclass(frozen=True)
class Resolution:
    resolved: tuple[ResolvedVectorChunk, ...]
    dropped: tuple[str, ...]


VisibleSourceOpener = Callable[[str, str, str], VisibleVectorSource | None]


def _safe_relative_path(raw: str) -> bool:
    if not raw or "\\" in raw or "\x00" in raw:
        return False
    raw_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts) or ":" in raw_parts[0]:
        return False
    path = PurePosixPath(raw)
    return not path.is_absolute()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_candidates(
    candidates: Iterable[VectorCandidate],
    *,
    open_visible: VisibleSourceOpener,
) -> Resolution:
    """Return only chunks whose current visible source still matches exactly.

    Drop codes contain no source content and intentionally do not distinguish a
    missing document from a permission denial.  That distinction would make the
    vector cache an oracle for documents the requester cannot currently see.
    """
    items = tuple(candidates)
    if len(items) > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates may be resolved")
    opened: dict[tuple[str, str, str], VisibleVectorSource | None] = {}
    resolved: list[ResolvedVectorChunk] = []
    dropped: list[str] = []
    for candidate in items:
        if not _safe_relative_path(candidate.doc_path):
            dropped.append("source_not_visible")
            continue
        key = (candidate.workspace, candidate.channel_id, candidate.doc_path)
        if key not in opened:
            opened[key] = open_visible(*key)
        source = opened[key]
        if source is None or (
            source.workspace,
            source.channel_id,
            source.doc_path,
        ) != key:
            dropped.append("source_not_visible")
            continue
        if _sha256(source.text) != candidate.source_revision:
            dropped.append("source_revision_changed")
            continue
        if not 0 <= candidate.start_offset < candidate.end_offset <= len(source.text):
            dropped.append("chunk_bounds_changed")
            continue
        text = source.text[candidate.start_offset : candidate.end_offset]
        if _sha256(text) != candidate.text_sha256:
            dropped.append("chunk_hash_changed")
            continue
        resolved.append(ResolvedVectorChunk(candidate=candidate, text=text))
    return Resolution(tuple(resolved), tuple(dropped))
