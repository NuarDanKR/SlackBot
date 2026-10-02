"""The status probe reads by workspace and channel ID without taking a write lock."""

from __future__ import annotations

from contextlib import contextmanager

from tybot.archive import collection_probe


class Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return next(self.rows)


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_probe_reads_mode_flag_and_latest_ack_by_id(monkeypatch):
    cursor = Cursor([
        {"mode": "active", "membership": "joined", "writer_owner": "archiver", "operator_hold": False},
        {"enabled": True},
        {"state": "partial", "written_to": "live", "updated_at": "2026-10-02T09:00:00+09:00"},
        {"total": 2},
    ])

    @contextmanager
    def connect():
        yield Connection(cursor)

    monkeypatch.setattr(collection_probe, "connect", connect)
    result = collection_probe.load("mgmt", "C123")

    assert result.checked and result.channel is not None
    assert result.channel.mode == "active"
    assert result.channel.last_ack_target == "live"
    assert result.channel.attachment_issues == 2
    assert len(cursor.queries) == 4
    assert cursor.queries[0][1] == ("mgmt", "C123")
    assert cursor.queries[2][1] == ("mgmt", "C123")
    assert all("FOR UPDATE" not in sql for sql, _ in cursor.queries)


def test_missing_row_and_db_failure_are_distinct(monkeypatch):
    @contextmanager
    def missing():
        yield Connection(Cursor([None]))

    monkeypatch.setattr(collection_probe, "connect", missing)
    assert collection_probe.load("mgmt", "C123") == collection_probe.Probe(checked=True)

    @contextmanager
    def unavailable():
        yield None

    monkeypatch.setattr(collection_probe, "connect", unavailable)
    assert collection_probe.load("mgmt", "C123") == collection_probe.Probe(checked=False)
