"""Replaying a current Slack snapshot must not rewrite revision history."""

from __future__ import annotations

import pytest

from tybot.archive import revision_store


class Cursor:
    def __init__(self, latest):
        self.latest = latest
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))

    def fetchone(self):
        return self.latest


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self._cursor


def _record(monkeypatch, latest, *, body="current"):
    cursor = Cursor(latest)
    monkeypatch.setattr(revision_store, "_connect", lambda: Connection(cursor))
    result = revision_store.record_revision(
        workspace="tyit", channel_id="C12345678", message_ts="1790000000.000001",
        kind="create", body=body,
    )
    return result, cursor


@pytest.mark.parametrize("kind", ["create", "change"])
def test_current_slack_snapshot_reuses_existing_revision(monkeypatch, kind):
    latest = {
        "revision_no": 3, "kind": kind, "edited_ts": "1790000010.000001",
        "body_sha256": revision_store.body_digest("current"),
    }

    revision_no, cursor = _record(monkeypatch, latest)

    assert revision_no == 3
    assert len(cursor.executed) == 2
    assert all("INSERT" not in query for query, _ in cursor.executed)


@pytest.mark.parametrize("kind,body", [
    ("change", "stale"),
    ("delete", "current"),
    ("redact", "current"),
])
def test_conflicting_slack_snapshot_never_adds_a_create_revision(monkeypatch, kind, body):
    latest = {
        "revision_no": 2, "kind": kind, "edited_ts": "1790000010.000001",
        "body_sha256": revision_store.body_digest("current"),
    }
    cursor = Cursor(latest)
    monkeypatch.setattr(revision_store, "_connect", lambda: Connection(cursor))

    with pytest.raises(ValueError, match="conflicts"):
        revision_store.record_revision(
            workspace="tyit", channel_id="C12345678", message_ts="1790000000.000001",
            kind="create", body=body,
        )

    assert all("INSERT" not in query for query, _ in cursor.executed)
