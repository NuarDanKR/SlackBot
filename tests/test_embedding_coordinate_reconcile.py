from __future__ import annotations

import hashlib

import pytest

from tybot.archive.embedding_source import CHANNEL, VectorSource
from tybot.embedding_coordinate_reconcile import (
    CoordinateMigration,
    CurrentDocument,
    ReconciliationError,
    TargetDocument,
    current_documents,
    plan_reconciliation,
    retire_migration,
)

MODEL = "local-v1"
CHUNKER = "source-v1:chunk-v1:max=1200:ov=120"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source(channel: str, path: str, text: str = "same") -> VectorSource:
    return VectorSource("sample", channel, path, CHANNEL, text, digest(text))


def current(id_: int, channel: str, path: str, text: str = "same") -> CurrentDocument:
    return CurrentDocument(id_, "sample", channel, path, digest(text), CHUNKER, MODEL)


def plan(rows, sources, *, complete=True):
    return plan_reconciliation(
        rows,
        sources,
        model_revision=MODEL,
        chunker_revision=CHUNKER,
        scan_complete=complete,
    )


def test_exact_current_rows_are_kept_and_changed_bodies_are_reindexed():
    path = "workspaces/sample/channels/C1/raw/2026-01-01.md"
    result = plan(
        [current(1, "C1", path), current(2, "C2", path, "old")],
        [source("C1", path), source("C2", path, "new")],
    )
    assert result.unchanged_document_ids == (1,)
    assert [(item.channel_id, item.source_revision) for item in result.reindex] == [
        ("C2", digest("new"))
    ]
    assert result.migrations == ()
    assert result.unresolved_document_ids == ()


def test_channel_id_and_directory_rename_make_one_migration_after_reindex():
    old_path = "sample/old-name/raw/2026-01-01.md"
    new_path = "workspaces/sample/channels/C123__new-name/raw/2026-01-01.md"
    result = plan([current(7, "old-name", old_path)], [source("C123", new_path)])

    assert [item.doc_path for item in result.reindex] == [new_path]
    assert result.migrations == (
        CoordinateMigration(7, result.reindex[0]),
    )
    assert result.unresolved_document_ids == ()


def test_attachment_file_id_and_revision_survive_directory_rename():
    old = "sample/old/archive/attachments/F123/rev-a.md"
    new = "workspaces/sample/channels/C1/attachments/F123/rev-a.md"
    result = plan([current(8, "old", old)], [source("C1", new)])
    assert result.migrations[0].old_document_revision_id == 8


def test_same_body_in_multiple_channels_is_never_guessed_as_a_migration():
    rows = [
        current(1, "old-a", "sample/a/raw/2026-01-01.md"),
        current(2, "old-b", "sample/b/raw/2026-01-01.md"),
    ]
    sources = [
        source("C1", "workspaces/sample/channels/C1/raw/2026-01-01.md"),
        source("C2", "workspaces/sample/channels/C2/raw/2026-01-01.md"),
    ]
    result = plan(rows, sources)
    assert result.migrations == ()
    assert result.unresolved_document_ids == (1, 2)
    assert len(result.reindex) == 2


def test_missing_and_open_day_sources_are_not_automatically_retired():
    result = plan([current(9, "C1", "sample/C1/raw/2026-01-02.md")], [])
    assert result.unresolved_document_ids == (9,)
    assert result.migrations == ()


def test_incomplete_scan_and_duplicate_coordinates_fail_closed():
    with pytest.raises(ReconciliationError, match="scan_incomplete"):
        plan([], [], complete=False)
    path = "sample/C1/raw/2026-01-01.md"
    with pytest.raises(ReconciliationError, match="duplicate_current"):
        plan([current(1, "C1", path), current(2, "C1", path)], [])
    with pytest.raises(ReconciliationError, match="duplicate_source"):
        plan([], [source("C1", path), source("C1", path)])


class Tx:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Cursor:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        self.result = self.scripted.pop(0) if self.scripted else None

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result or []


class Connection:
    def __init__(self, scripted):
        self.cur = Cursor(scripted)

    def cursor(self):
        return self.cur

    def transaction(self):
        return Tx()


def target() -> TargetDocument:
    return TargetDocument(
        "sample",
        "C123",
        "workspaces/sample/channels/C123/raw/2026-01-01.md",
        digest("same"),
        CHUNKER,
        MODEL,
    )


def old_row(*, state="ready", is_current=True):
    return ("sample", "old", "sample/old/raw/2026-01-01.md", digest("same"), CHUNKER, MODEL, state, is_current)


def test_retirement_requires_the_exact_replacement_to_be_current():
    migration = CoordinateMigration(7, target())
    missing = Connection([old_row(), None])
    with pytest.raises(ReconciliationError, match="replacement_not_current"):
        retire_migration(missing, migration)
    assert not any("SET state='retired'" in sql for sql, _ in missing.cur.calls)

    wrong = Connection([("sample", "old", "x", digest("other"), CHUNKER, MODEL, "ready", True)])
    with pytest.raises(ReconciliationError, match="identity_mismatch"):
        retire_migration(wrong, migration)


def test_retirement_happens_only_after_replacement_and_is_idempotent():
    migration = CoordinateMigration(7, target())
    conn = Connection([old_row(), (12,), None])
    assert retire_migration(conn, migration)
    assert any("SET state='retired', is_current=false" in sql for sql, _ in conn.cur.calls)

    retired = Connection([old_row(state="retired", is_current=False), (12,)])
    assert not retire_migration(retired, migration)


def test_current_manifest_query_never_selects_body_or_vectors():
    row = (7, "sample", "C1", "sample/C1/raw/a.md", digest("same"), CHUNKER, MODEL)
    conn = Connection([[row]])
    result = current_documents(conn, model_revision=MODEL, chunker_revision=CHUNKER)
    assert result == (current(7, "C1", "sample/C1/raw/a.md"),)
    sql = conn.cur.calls[0][0].lower()
    assert "source_revision" in sql
    assert "embedding" not in sql
    assert " text" not in sql
