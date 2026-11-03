from __future__ import annotations

import inspect

from tybot import search_index
from tybot.archive import file_reconversion, files
from tybot.archive.store import ArchiveDoc, RawLine
from tybot.archive.text_normalize import remove_nul


def test_remove_nul_preserves_the_surrounding_text():
    assert remove_nul("공\x00정\x00률") == ("공정률", 2)
    clean = "원문"
    got, count = remove_nul(clean)
    assert got is clean
    assert count == 0


def test_every_attachment_conversion_path_normalizes_before_storage():
    main = inspect.getsource(files.stage_attachments)
    retry = inspect.getsource(file_reconversion.reconvert)

    assert main.index("remove_nul(extracted)") < main.index("screen_document(")
    assert retry.index("remove_nul(text)") < retry.index("screen_document(")


def test_reindex_removes_nul_from_every_database_text_field(monkeypatch, tmp_path, caplog):
    path = tmp_path / "raw" / "2026-11-03.md"
    path.parent.mkdir()
    captured: list[tuple] = []

    doc = ArchiveDoc(
        path=path,
        workspace="ty\x00it",
        channel="#채\x00널",
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
        raw_lines=[
            RawLine("2026-11-03\x00 09:00", "화\x00자", "공\x00정", 1, path),
            RawLine("2026-11-03 09:01", "화자", "\x00\x00", 2, path),
        ],
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(search_index, "_connect", lambda: Connection())
    monkeypatch.setattr(
        search_index, "_flush", lambda _cursor, rows: captured.extend(rows) or len(rows)
    )

    with caplog.at_level("WARNING", logger="tybot.search_index"):
        result = search_index.reindex([doc], tmp_path)

    assert result == {"docs": 1, "lines": 1, "seen_lines": 1}
    assert captured
    assert all("\x00" not in str(value) for value in captured[0])
    assert captured[0][0:2] == ("tyit", "#채널")
    assert captured[0][4:7] == ("2026-11-03 09:00", "화자", "공정")
    assert "lines=2 chars=7" in caplog.text
