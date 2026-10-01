from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from tybot import archiving_bot
from tybot.archive import shadow_paths, writer
from tybot.archive.dm_consumer import DmConsumer
from tybot.archive.dm_file_handoff import DmFile, DmFileVault, digest_of
from tybot.archive.dm_inbox import DmHandoff, DmInbox
from tybot.archive.store import ArchiveStore


def _handoff(**changes):
    event = {
        "channel_type": "im", "channel": "D12345678", "user": "U12345678",
        "ts": "1790800000.000001", "text": "human message",
    }
    event.update(changes)
    return DmHandoff(
        "tyit", "D12345678", "U12345678", "1790800000.000001",
        "create", "U87654321", "Alice", event,
    )


def _system(tmp_path):
    cipher = Fernet(Fernet.generate_key())
    inbox = DmInbox(tmp_path / "inbox", cipher)
    root = tmp_path / "archive"
    consumer = DmConsumer(
        workspace="tyit", master_bot_user_id="U87654321",
        archive_root=root, inbox=inbox,
        file_vault=DmFileVault(tmp_path / "inbox", cipher),
    )
    return inbox, root, consumer


def _file_handoff(consumer, *, file_id="F12345678", text="human message"):
    data = b"private original attachment bytes"
    meta = DmFile(
        workspace="tyit", channel_id="D12345678", user_id="U12345678",
        message_ts="1790800000.000001", file_id="F12345678",
        name="note.txt", mimetype="text/plain", size=len(data), sha256=digest_of(data),
    )
    key = consumer.file_vault.put(meta, data)
    return _handoff(
        subtype="file_share", text=text, files=[{"id": file_id}],
        _dm_file_keys=[key],
    ), key, data


def test_text_dm_is_archived_in_private_path_then_acked(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    inbox.enqueue(_handoff())
    assert consumer.run_once() == (1, 0)
    assert inbox.pending_keys("tyit") == []
    directory = shadow_paths.dm_archive_dir(root, "tyit", "U12345678", "D12345678")
    raw = next((directory / "raw").glob("*.md")).read_text(encoding="utf-8")
    assert "human message" in raw
    assert "dm_user: U12345678" in raw
    assert "channel_id: D12345678" in raw
    assert ArchiveStore(root).docs() == []
    assert len(ArchiveStore(root).docs(dm_scope="U12345678")) == 1


def test_retry_after_raw_write_does_not_duplicate(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    handoff = _handoff()
    inbox.enqueue(handoff)
    assert consumer._ingest(handoff)
    assert consumer.run_once() == (1, 0)
    raw = next((root / "tyit" / "dm" / "U12345678" / "archive" / "raw").glob("*.md"))
    assert raw.read_text(encoding="utf-8").count("human message") == 1


def test_attachment_metadata_cannot_be_acked_as_file_bytes(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    key = inbox.enqueue(_handoff(subtype="file_share", files=[{"id": "F12345678"}]))
    assert consumer.run_once() == (0, 1)
    assert inbox.pending_keys("tyit") == [key]
    assert not (root / "tyit").exists()


def test_attachment_original_is_encrypted_and_raw_reference_durable_before_ack(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    handoff, file_key, data = _file_handoff(consumer)
    inbox.enqueue(handoff)

    assert consumer.run_once() == (1, 0)
    assert inbox.pending_keys("tyit") == []
    private_root = shadow_paths.dm_root(root, "tyit", "U12345678", "D12345678")
    target = DmFileVault(private_root / "objects", inbox.cipher)
    assert target.open("tyit", file_key)[1] == data
    sealed = private_root / "objects" / "tyit" / "files" / f"{file_key}.bin"
    assert data not in sealed.read_bytes()
    raw = next((private_root / "archive" / "raw").glob("*.md")).read_text(encoding="utf-8")
    assert "human message" in raw
    assert file_key in raw
    assert "extraction pending" in raw
    assert data.decode() not in raw
    assert ArchiveStore(root).docs() == []


def test_file_only_dm_and_retry_do_not_duplicate_reference(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    handoff, file_key, _ = _file_handoff(consumer, text="")
    inbox.enqueue(handoff)
    assert consumer._ingest(handoff)
    assert consumer.run_once() == (1, 0)
    raw = next((root / "tyit" / "dm" / "U12345678" / "archive" / "raw").glob("*.md"))
    assert raw.read_text(encoding="utf-8").count(file_key) == 1


def test_attachment_identity_mismatch_keeps_envelope_pending(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    handoff, _, _ = _file_handoff(consumer, file_id="F99999999")
    key = inbox.enqueue(handoff)
    assert consumer.run_once() == (0, 1)
    assert inbox.pending_keys("tyit") == [key]
    assert not root.exists()


def test_raw_write_failure_retains_envelope_and_encrypted_original(monkeypatch, tmp_path):
    inbox, root, consumer = _system(tmp_path)
    handoff, file_key, data = _file_handoff(consumer)
    inbox_key = inbox.enqueue(handoff)
    original_ingest = writer.ingest

    def fail_ingest(*args, **kwargs):
        raise OSError("raw write failed")

    monkeypatch.setattr(writer, "ingest", fail_ingest)
    assert consumer.run_once() == (0, 1)
    assert inbox.pending_keys("tyit") == [inbox_key]
    private_root = shadow_paths.dm_root(root, "tyit", "U12345678", "D12345678")
    assert DmFileVault(private_root / "objects", inbox.cipher).open("tyit", file_key)[1] == data
    assert not list(private_root.rglob("*.md"))

    monkeypatch.setattr(writer, "ingest", original_ingest)
    assert consumer.run_once() == (1, 0)
    assert inbox.pending_keys("tyit") == []


def test_wrong_master_bot_identity_is_retained(tmp_path):
    inbox, root, consumer = _system(tmp_path)
    key = inbox.enqueue(replace(_handoff(), master_bot_user_id="U99999999"))
    assert consumer.run_once() == (0, 1)
    assert inbox.pending_keys("tyit") == [key]
    assert not (root / "tyit").exists()


def test_writer_refusal_does_not_ack(monkeypatch, tmp_path):
    inbox, root, consumer = _system(tmp_path)
    key = inbox.enqueue(_handoff())
    monkeypatch.setattr(writer, "screen", lambda text: "refused")
    assert consumer.run_once() == (0, 1)
    assert inbox.pending_keys("tyit") == [key]
    assert not list(root.rglob("*.md"))


def test_worker_consumer_is_off_by_default(tmp_path):
    cfg = archiving_bot.ArchiverWorkspace(
        "tyit", "bot", "app", "T12345678", "U87654321", frozenset(), True,
    )
    assert archiving_bot._dm_handoff_consumer(cfg, tmp_path, {}) is None
    assert not list(tmp_path.iterdir())


def test_worker_consumer_requires_private_layout_and_existing_inbox(monkeypatch, tmp_path):
    cfg = archiving_bot.ArchiverWorkspace(
        "tyit", "bot", "app", "T12345678", "U87654321", frozenset(), True,
    )
    handoff = tmp_path / "inbox"
    env = {"ARCHIVER_DM_CONSUME_ENABLED": "1", "ARCHIVER_DM_HANDOFF_DIR": str(handoff)}
    with pytest.raises(archiving_bot.ArchiverConfigError, match="per-channel-v1"):
        archiving_bot._dm_handoff_consumer(cfg, tmp_path, env)
    env["ARCHIVER_SHADOW_LAYOUT"] = "per-channel-v1"
    with pytest.raises(archiving_bot.ArchiverConfigError, match="existing"):
        archiving_bot._dm_handoff_consumer(cfg, tmp_path, env)
    handoff.mkdir()
    monkeypatch.setattr("tybot.console.workspace_store._fernet", lambda: Fernet(Fernet.generate_key()))
    consumer = archiving_bot._dm_handoff_consumer(cfg, tmp_path / "archive", env)
    assert isinstance(consumer, DmConsumer)
