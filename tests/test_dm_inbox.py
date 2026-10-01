from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from tybot.archive.dm_inbox import DmHandoff, DmInbox


def _handoff(**event_changes):
    event = {
        "channel_type": "im",
        "channel": "D12345678",
        "user": "U12345678",
        "ts": "1790800000.000001",
        "text": "private message",
    }
    event.update(event_changes)
    return DmHandoff(
        "tyit", "D12345678", "U12345678", "1790800000.000001", "create",
        "U87654321", "Alice", event,
    )


def test_encrypted_inbox_is_durable_and_idempotent(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoff = _handoff()
    key = inbox.enqueue(handoff)

    assert inbox.enqueue(handoff) == key
    assert inbox.pending_keys("tyit") == [key]
    assert inbox.read("tyit", key) == handoff
    raw = (tmp_path / "tyit" / "pending" / f"{key}.bin").read_bytes()
    assert b"private message" not in raw
    assert b"U12345678" not in raw
    inbox.acknowledge("tyit", key)
    assert inbox.pending_keys("tyit") == []


@pytest.mark.parametrize("changes", [
    {"channel_type": "mpim"},
    {"channel_type": "channel"},
    {"bot_id": "B12345678"},
    {"subtype": "bot_message"},
    {"subtype": "message_changed"},
    {"user": "U99999999"},
    {"channel": "D99999999"},
])
def test_only_master_human_one_to_one_event_can_be_enqueued(tmp_path, changes):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    with pytest.raises(ValueError, match="one-to-one"):
        inbox.enqueue(_handoff(**changes))
    assert not list(tmp_path.rglob("*.bin"))


def test_file_share_metadata_is_preserved_but_not_exposed_in_filename(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoff = _handoff(subtype="file_share", files=[{"id": "F12345678"}])
    key = inbox.enqueue(handoff)
    assert inbox.read("tyit", key).event["files"] == [{"id": "F12345678"}]
    assert "F12345678" not in key


@pytest.mark.parametrize("keys", [[], ["bad"], ["a" * 64, "b" * 64]])
def test_file_vault_references_must_match_file_count_and_shape(tmp_path, keys):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoff = _handoff(files=[{"id": "F12345678"}], _dm_file_keys=keys)
    with pytest.raises(ValueError, match="attachment handoff references"):
        inbox.enqueue(handoff)
    assert not list(tmp_path.rglob("*.bin"))


def test_enterprise_user_id_matches_dm_path_rules(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoff = replace(_handoff(), user_id="W12345678",
                      event=_handoff().event | {"user": "W12345678"})
    assert inbox.read("tyit", inbox.enqueue(handoff)) == handoff


def test_same_revision_cannot_be_replaced_with_different_content(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoff = _handoff()
    key = inbox.enqueue(handoff)
    changed = replace(handoff, event=handoff.event | {"text": "changed"})
    with pytest.raises(ValueError, match="conflicting content"):
        inbox.enqueue(changed)
    assert inbox.read("tyit", key) == handoff


def test_workspace_boundary_and_tampering_fail_closed(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    key = inbox.enqueue(_handoff())
    with pytest.raises(ValueError, match="cannot be read"):
        inbox.read("mgmt", key)
    path = tmp_path / "tyit" / "pending" / f"{key}.bin"
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="cannot be read"):
        inbox.acknowledge("tyit", key)
    assert path.exists()


def test_invalid_identity_cannot_escape_inbox(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    with pytest.raises(ValueError, match="workspace"):
        inbox.enqueue(replace(_handoff(), workspace="../mgmt"))
    with pytest.raises(ValueError, match="key"):
        inbox.read("tyit", "../escape")
    assert not list(tmp_path.rglob("*.bin"))


def test_consumer_retains_unconfirmed_entries_for_retry(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    key = inbox.enqueue(_handoff())
    assert inbox.drain_once("tyit", lambda _: False) == (0, 1)
    assert inbox.pending_keys("tyit") == [key]

    seen = []
    assert inbox.drain_once("tyit", lambda item: seen.append(item) or True) == (1, 0)
    assert seen == [_handoff()]
    assert inbox.pending_keys("tyit") == []


def test_handler_exception_does_not_erase_entry_or_block_next_one(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    inbox.enqueue(_handoff())
    inbox.enqueue(replace(_handoff(), message_ts="1790800000.000002",
                          event=_event_with_ts("1790800000.000002")))

    def process(item):
        if item.message_ts.endswith("1"):
            raise RuntimeError("transient")
        return True

    assert inbox.drain_once("tyit", process) == (1, 1)
    assert len(inbox.pending_keys("tyit")) == 1


def _event_with_ts(ts):
    return _handoff().event | {"ts": ts}


def test_refused_entries_do_not_starve_later_dms(tmp_path):
    inbox = DmInbox(tmp_path, Fernet(Fernet.generate_key()))
    handoffs = [
        replace(_handoff(), message_ts=f"1790800000.{n:06d}",
                event=_event_with_ts(f"1790800000.{n:06d}"))
        for n in range(1, 5)
    ]
    for handoff in handoffs:
        inbox.enqueue(handoff)
    seen = []

    def handle(item):
        seen.append(item.message_ts)
        return False

    assert inbox.drain_once("tyit", handle, limit=2) == (0, 2)
    assert inbox.drain_once("tyit", handle, limit=2) == (0, 2)
    assert set(seen) == {item.message_ts for item in handoffs}
