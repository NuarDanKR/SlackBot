"""Aggregate a canonical embedding inventory without calling a model or DB writer."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from tybot.archive import chunking
from tybot.archive.embedding_source import VectorSource, source_chunker_revision
from tybot.embedding_index_job import IndexLimits
from tybot.embedding_index_plan import IndexPlanError, plan_source

SCHEMA = "tybot.embedding-index-preview.v1"


@dataclass(frozen=True)
class WorkspaceInventory:
    workspace: str
    documents: int
    source_chars: int
    planned_documents: int
    planned_chars: int
    planned_chunks: int
    outcomes: tuple[tuple[str, int], ...]

    def to_json(self) -> dict[str, object]:
        return {
            "workspace": self.workspace,
            "documents": self.documents,
            "sourceChars": self.source_chars,
            "plannedDocuments": self.planned_documents,
            "plannedChars": self.planned_chars,
            "plannedChunks": self.planned_chunks,
            "outcomes": dict(self.outcomes),
        }


@dataclass(frozen=True)
class EmbeddingInventory:
    model_revision: str
    chunker_revision: str
    documents: int
    source_chars: int
    planned_documents: int
    planned_chars: int
    planned_chunks: int
    minimum_batches: int
    outcomes: tuple[tuple[str, int], ...]
    workspaces: tuple[WorkspaceInventory, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "readOnly": True,
            "modelCalls": 0,
            "databaseWrites": 0,
            "modelRevision": self.model_revision,
            "chunkerRevision": self.chunker_revision,
            "documents": self.documents,
            "sourceChars": self.source_chars,
            "plannedDocuments": self.planned_documents,
            "plannedChars": self.planned_chars,
            "plannedChunks": self.planned_chunks,
            "minimumBatches": self.minimum_batches,
            "outcomes": dict(self.outcomes),
            "workspaces": [item.to_json() for item in self.workspaces],
        }


@dataclass
class _MutableInventory:
    documents: int = 0
    source_chars: int = 0
    planned_documents: int = 0
    planned_chars: int = 0
    planned_chunks: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


def inventory(
    sources: Iterable[VectorSource],
    *,
    model_revision: str,
    workspace: str = "",
    limits: IndexLimits | None = None,
    spec: chunking.ChunkSpec = chunking.DEFAULT_SPEC,
) -> EmbeddingInventory:
    """Plan canonical sources one at a time and return body-free aggregates.

    This intentionally does not accept a flag that weakens canonical-source or
    revision checks.  The caller owns source discovery and must use the same
    fail-closed source iterator as the real indexer.
    """
    revision = str(model_revision or "")
    if not revision or revision != revision.strip():
        raise ValueError("model_revision must be non-empty and trimmed")
    selected = str(workspace or "")
    if selected != selected.strip():
        raise ValueError("workspace must be trimmed")
    limits = limits or IndexLimits()
    total = _MutableInventory()
    by_workspace: dict[str, _MutableInventory] = {}

    for source in sources:
        if not isinstance(source, VectorSource):
            _outcome(total, "source_invalid")
            continue
        if selected and source.workspace != selected:
            continue
        current = by_workspace.setdefault(source.workspace, _MutableInventory())
        size = len(source.text)
        total.documents += 1
        total.source_chars += size
        current.documents += 1
        current.source_chars += size
        if size > limits.max_chars_per_document:
            _outcome(total, "source_too_large")
            _outcome(current, "source_too_large")
            continue
        try:
            plan = plan_source(source, model_revision=revision, spec=spec)
        except (IndexPlanError, ValueError):
            _outcome(total, "plan_rejected")
            _outcome(current, "plan_rejected")
            continue
        _add_plan(total, plan.char_count, plan.chunk_count)
        _add_plan(current, plan.char_count, plan.chunk_count)
        _outcome(total, "planned")
        _outcome(current, "planned")

    workspaces = tuple(
        WorkspaceInventory(
            workspace=name,
            documents=value.documents,
            source_chars=value.source_chars,
            planned_documents=value.planned_documents,
            planned_chars=value.planned_chars,
            planned_chunks=value.planned_chunks,
            outcomes=_frozen_outcomes(value),
        )
        for name, value in sorted(by_workspace.items())
    )
    return EmbeddingInventory(
        model_revision=revision,
        chunker_revision=source_chunker_revision(spec.plan),
        documents=total.documents,
        source_chars=total.source_chars,
        planned_documents=total.planned_documents,
        planned_chars=total.planned_chars,
        planned_chunks=total.planned_chunks,
        minimum_batches=_minimum_batches(total, limits),
        outcomes=_frozen_outcomes(total),
        workspaces=workspaces,
    )


def _add_plan(target: _MutableInventory, chars: int, chunks: int) -> None:
    target.planned_documents += 1
    target.planned_chars += chars
    target.planned_chunks += chunks


def _outcome(target: _MutableInventory, code: str) -> None:
    target.outcomes[code] += 1


def _frozen_outcomes(target: _MutableInventory) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(target.outcomes.items()))


def _minimum_batches(target: _MutableInventory, limits: IndexLimits) -> int:
    if not target.planned_documents:
        return 0
    return max(
        math.ceil(target.planned_documents / limits.max_documents),
        math.ceil(target.planned_documents / limits.max_calls),
        math.ceil(target.planned_chars / limits.max_total_chars),
        math.ceil(target.planned_chunks / limits.max_total_chunks),
    )


__all__ = ["EmbeddingInventory", "WorkspaceInventory", "inventory"]
