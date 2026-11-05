from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tybot import vipbot_event_claims as claims


def code(call) -> str:
    with pytest.raises(claims.EventClaimError) as caught:
        call()
    return caught.value.code


def test_state_path_requires_the_systemd_state_directory(tmp_path):
    assert code(lambda: claims.state_path({})) == claims.STATE_UNCONFIGURED
    assert claims.state_path({"STATE_DIRECTORY": str(tmp_path)}) == (
        tmp_path / "slack-events.sqlite3"
    )


def test_relative_missing_or_symlinked_state_directories_are_rejected(tmp_path):
    assert code(lambda: claims.state_path({"STATE_DIRECTORY": "relative"})) == claims.STATE_INVALID
    assert (
        code(lambda: claims.state_path({"STATE_DIRECTORY": str(tmp_path / "missing")}))
        == claims.STATE_INVALID
    )
    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    assert code(lambda: claims.state_path({"STATE_DIRECTORY": str(link)})) == claims.STATE_INVALID


def test_one_event_is_claimed_exactly_once(tmp_path):
    store = claims.EventClaims(tmp_path / "events.sqlite3")
    assert store.claim("Ev00000001", now=100) is True
    assert store.claim("Ev00000001", now=101) is False
    assert store.claim("Ev00000002", now=101) is True


def test_concurrent_delivery_attempts_have_one_winner(tmp_path):
    store = claims.EventClaims(tmp_path / "events.sqlite3")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim("EvConcurrent01", now=100), range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_only_event_id_and_timestamp_are_stored(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = claims.EventClaims(path)
    store.claim("EvNoBody0001", now=123)
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(claimed_event)")]
        row = connection.execute("SELECT * FROM claimed_event").fetchone()
    assert columns == ["event_id", "claimed_at"]
    assert row == ("EvNoBody0001", 123)


def test_old_claims_are_pruned_but_recent_duplicates_remain(tmp_path):
    store = claims.EventClaims(tmp_path / "events.sqlite3")
    store.claim("EvOld000001", now=1)
    store.claim("EvRecent001", now=claims._RETENTION_SECONDS + 2)
    assert store.claim("EvOld000001", now=claims._RETENTION_SECONDS + 2) is True
    assert store.claim("EvRecent001", now=claims._RETENTION_SECONDS + 3) is False


@pytest.mark.parametrize(
    "event_id",
    ["", "short", "has space 000", "한글이벤트000", "x" * 129],
)
def test_invalid_event_ids_fail_before_sqlite(event_id, tmp_path):
    store = claims.EventClaims(tmp_path / "events.sqlite3")
    assert code(lambda: store.claim(event_id)) == claims.EVENT_ID_INVALID
    assert not (tmp_path / "events.sqlite3").exists()


def test_broken_state_storage_fails_closed_without_database_details(tmp_path):
    path = tmp_path / "events.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    store = claims.EventClaims(path)
    with pytest.raises(claims.EventClaimError) as caught:
        store.claim("EvBroken001")
    assert caught.value.code == claims.STATE_UNAVAILABLE
    assert caught.value.__cause__ is None
    assert str(path) not in str(caught.value)
