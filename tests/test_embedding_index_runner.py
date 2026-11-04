from __future__ import annotations

import hashlib

import pytest

from tybot.archive.embedding_source import CHANNEL, VectorSource
from tybot.embedding_coordinate_reconcile import (
    CurrentDocument,
    ReconciliationError,
)
from tybot.embedding_index_job import EmbeddingBatch, IndexAuthorization
from tybot.embedding_index_runner import run_reconciled_index
from tybot.embedding_index_store import PublishResult

MODEL = "local-v1"
CHUNKER = "source-v1:chunk-v1:max=1200:ov=120"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source(channel: str, path: str, text: str = "body\n") -> VectorSource:
    return VectorSource("sample", channel, path, CHANNEL, text, digest(text))


def current(id_: int, channel: str, path: str, text: str = "body\n") -> CurrentDocument:
    return CurrentDocument(id_, "sample", channel, path, digest(text), CHUNKER, MODEL)


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, inputs, *, model_revision, policy_revision):
        self.calls.append((inputs, model_revision, policy_revision))
        return EmbeddingBatch.checked(
            model_revision,
            [[float(item.chunk_no), 1.0] for item in inputs],
            cost_usd=0,
        )


class Writer:
    def __init__(self, *, current=False):
        self.calls = []
        self.current = current

    def publish(self, key, text, chunks, vectors):
        self.calls.append((key, text, chunks, vectors))
        return PublishResult(10 + len(self.calls), len(chunks), self.current)


def allow(_request):
    return IndexAuthorization(True, MODEL, "policy-1", "local")


def install_current(monkeypatch, rows):
    monkeypatch.setattr(
        "tybot.embedding_index_runner.current_documents",
        lambda *_args, **_kwargs: tuple(rows),
    )


def test_rerun_uses_current_manifest_as_checkpoint(monkeypatch):
    path = "workspaces/sample/channels/C1/raw/2026-01-01.md"
    item = source("C1", path)
    install_current(monkeypatch, [current(1, "C1", path)])
    embedder, writer = Embedder(), Writer()
    result = run_reconciled_index(
        object(),
        [item],
        scan_complete=True,
        model_revision=MODEL,
        authorize=allow,
        embedder=embedder,
        writer=writer,
    )
    assert result.inventory_count == result.unchanged_count == 1
    assert result.requested_count == 0
    assert result.batch.embed_calls == 0
    assert embedder.calls == writer.calls == []


def test_new_and_changed_sources_are_the_only_embedding_requests(monkeypatch):
    same_path = "workspaces/sample/channels/C1/raw/2026-01-01.md"
    changed_path = "workspaces/sample/channels/C2/raw/2026-01-01.md"
    new_path = "workspaces/sample/channels/C3/raw/2026-01-01.md"
    install_current(
        monkeypatch,
        [
            current(1, "C1", same_path),
            current(2, "C2", changed_path, "old\n"),
        ],
    )
    embedder, writer = Embedder(), Writer()
    result = run_reconciled_index(
        object(),
        [
            source("C1", same_path),
            source("C2", changed_path, "new\n"),
            source("C3", new_path),
        ],
        scan_complete=True,
        model_revision=MODEL,
        authorize=allow,
        embedder=embedder,
        writer=writer,
    )
    assert result.unchanged_count == 1
    assert result.requested_count == result.batch.published == 2
    assert len(embedder.calls) == len(writer.calls) == 2


def test_coordinate_migration_retires_only_after_publish(monkeypatch):
    old = "sample/old/raw/2026-01-01.md"
    new = "workspaces/sample/channels/C123__new/raw/2026-01-01.md"
    install_current(monkeypatch, [current(7, "old", old)])
    events = []
    writer = Writer()
    original_publish = writer.publish

    def publish(*args):
        events.append("publish")
        return original_publish(*args)

    writer.publish = publish

    def retire(_connection, migration):
        events.append(("retire", migration.old_document_revision_id))
        return True

    monkeypatch.setattr("tybot.embedding_index_runner.retire_migration", retire)
    result = run_reconciled_index(
        object(),
        [source("C123", new)],
        scan_complete=True,
        model_revision=MODEL,
        authorize=allow,
        embedder=Embedder(),
        writer=writer,
    )
    assert events == ["publish", ("retire", 7)]
    assert result.migration_count == result.migrations_retired == 1


def test_unpublished_migration_is_deferred_not_retired(monkeypatch):
    old = "sample/old/raw/2026-01-01.md"
    new = "workspaces/sample/channels/C123__new/raw/2026-01-01.md"
    install_current(monkeypatch, [current(7, "old", old)])
    monkeypatch.setattr(
        "tybot.embedding_index_runner.retire_migration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ReconciliationError("replacement_not_current")
        ),
    )
    result = run_reconciled_index(
        object(),
        [source("C123", new)],
        scan_complete=True,
        model_revision=MODEL,
        authorize=lambda _request: IndexAuthorization(
            False, MODEL, "policy-1", "local"
        ),
        embedder=Embedder(),
        writer=Writer(),
    )
    assert result.batch.embed_calls == 0
    assert result.migrations_retired == 0
    assert result.migrations_deferred == 1


def test_missing_old_source_stays_unresolved_and_is_never_retired(monkeypatch):
    install_current(
        monkeypatch,
        [current(9, "C1", "sample/C1/raw/2026-01-01.md")],
    )
    called = False

    def retire(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr("tybot.embedding_index_runner.retire_migration", retire)
    result = run_reconciled_index(
        object(),
        [],
        scan_complete=True,
        model_revision=MODEL,
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(),
    )
    assert result.unresolved_count == 1
    assert result.migration_count == 0
    assert called is False


def test_incomplete_inventory_fails_before_embedding_or_retirement(monkeypatch):
    install_current(monkeypatch, [])
    embedder, writer = Embedder(), Writer()
    with pytest.raises(ReconciliationError, match="scan_incomplete"):
        run_reconciled_index(
            object(),
            [source("C1", "sample/C1/raw/a.md")],
            scan_complete=False,
            model_revision=MODEL,
            authorize=allow,
            embedder=embedder,
            writer=writer,
        )
    assert embedder.calls == writer.calls == []


def test_result_repr_contains_counts_not_paths_or_text(monkeypatch):
    path = "workspaces/sample/channels/C1/raw/private.md"
    item = source("C1", path, "private source body\n")
    install_current(monkeypatch, [])
    result = run_reconciled_index(
        object(),
        [item],
        scan_complete=True,
        model_revision=MODEL,
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(),
    )
    rendered = repr(result)
    assert path not in rendered
    assert item.text not in rendered
