from __future__ import annotations

import gc
import tracemalloc
from pathlib import Path

import pytest
from test_archive_memory_growth import CTX, _attachment, _channel

from tybot import search_index
from tybot.archive import attachment_reader
from tybot.archive.store import ArchiveStore


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _peak(call) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_index_candidate_opens_only_its_attachment_family(tmp_path, monkeypatch):
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, index, body_lines=20) for index in range(12)]
    target = paths[7]
    target_doc = attachment_reader.load_checked(target, tmp_path)
    assert target_doc is not None
    query = target_doc.text.splitlines()[0]

    monkeypatch.setattr(
        search_index,
        "candidates",
        lambda _query, _channels: [
            search_index.Candidate(
                doc_path=_relative(target, tmp_path),
                line_no=1,
                spoken_at="2026-01-01 01:00",
            )
        ],
    )
    monkeypatch.setattr(search_index, "indexed_counts", lambda _paths: None)

    reads: list[Path] = []
    real_load = attachment_reader.load

    def counting(path: Path):
        reads.append(path)
        return real_load(path)

    monkeypatch.setattr(attachment_reader, "load", counting)
    store = ArchiveStore(tmp_path)
    monkeypatch.setattr(
        store,
        "visible_docs",
        lambda _ctx: pytest.fail("fallback must stream attachments"),
    )
    hits = store.search(query, CTX)

    assert hits
    assert {path.resolve() for path in reads} == {target.resolve()}
    assert all((hit.line.source_path or hit.doc.path).resolve() == target.resolve() for hit in hits)


def test_candidate_old_revision_does_not_revive_after_a_newer_revision(tmp_path):
    _channel(tmp_path, 0)
    old = _attachment(tmp_path, 1, body_lines=2)
    newer = old.with_name("r2.md")
    newer.write_text(
        old.read_text(encoding="utf-8")
        .replace("revision: r1", "revision: r2")
        .replace(
            "converted_at: 2026-01-01T01:00+09:00",
            "converted_at: 2026-01-02T01:00+09:00",
        ),
        encoding="utf-8",
    )
    channels = ArchiveStore(tmp_path)._visible_channel_docs(CTX)

    docs = attachment_reader.archive_docs(
        tmp_path,
        channels,
        paths={_relative(old, tmp_path)},
    )

    assert docs == []


def test_candidate_revision_is_compared_across_canonical_and_shadow_roots(tmp_path):
    _channel(tmp_path, 0)
    old = _attachment(tmp_path, 1, body_lines=2)
    shadow = (
        tmp_path
        / "pilot"
        / "C00000000__transition"
        / "archive"
        / "attachments"
        / "F00000001"
        / "r2.md"
    )
    shadow.parent.mkdir(parents=True)
    shadow.write_text(
        old.read_text(encoding="utf-8")
        .replace("revision: r1", "revision: r2")
        .replace(
            "converted_at: 2026-01-01T01:00+09:00",
            "converted_at: 2026-01-02T01:00+09:00",
        ),
        encoding="utf-8",
    )
    channels = ArchiveStore(tmp_path)._visible_channel_docs(CTX)

    docs = attachment_reader.archive_docs(
        tmp_path,
        channels,
        paths={_relative(old, tmp_path)},
    )

    assert docs == []


def test_candidate_path_cannot_escape_the_archive_root(tmp_path, monkeypatch):
    _channel(tmp_path, 0)
    channels = ArchiveStore(tmp_path)._visible_channel_docs(CTX)
    opened: list[Path] = []
    monkeypatch.setattr(
        attachment_reader,
        "load",
        lambda path: opened.append(path) or None,
    )

    docs = attachment_reader.archive_docs(
        tmp_path,
        channels,
        paths={"../outside/attachments/F1/r1.md"},
    )

    assert docs == []
    assert opened == []


def test_database_failure_keeps_the_existing_full_scan_fallback(tmp_path, monkeypatch):
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, index, body_lines=2) for index in range(4)]
    target_doc = attachment_reader.load_checked(paths[2], tmp_path)
    assert target_doc is not None
    query = target_doc.text.splitlines()[0]
    monkeypatch.setattr(search_index, "candidates", lambda _query, _channels: None)

    reads: list[Path] = []
    real_load = attachment_reader.load

    def counting(path: Path):
        reads.append(path)
        return real_load(path)

    monkeypatch.setattr(attachment_reader, "load", counting)
    store = ArchiveStore(tmp_path)
    monkeypatch.setattr(
        store,
        "visible_docs",
        lambda _ctx: pytest.fail("fallback must stream attachments"),
    )
    hits = store.search(query, CTX)

    assert hits
    assert {path.resolve() for path in reads} == {path.resolve() for path in paths}


def test_full_scan_fallback_does_not_retain_every_attachment(tmp_path, monkeypatch):
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, index, body_lines=800) for index in range(24)]
    target_doc = attachment_reader.load_checked(paths[-1], tmp_path)
    assert target_doc is not None
    original = target_doc.text.splitlines()[0]
    paths[-1].write_text(
        paths[-1].read_text(encoding="utf-8").replace(
            original, "uniqueneedlevalue", 1
        ),
        encoding="utf-8",
    )
    query = "uniqueneedlevalue"
    monkeypatch.setattr(search_index, "candidates", lambda _query, _channels: None)

    lazy = ArchiveStore(tmp_path)
    lazy._attachments = attachment_reader.DocCache(max_bytes=0)
    lazy_hits = []

    def search_once():
        lazy_hits.extend(lazy.search(query, CTX))

    lazy_peak = _peak(search_once)

    eager = ArchiveStore(tmp_path)
    eager._attachments = attachment_reader.DocCache(max_bytes=0)
    eager_peak = _peak(lambda: eager.visible_docs(CTX))

    assert lazy_hits
    assert lazy_peak * 2 < eager_peak
