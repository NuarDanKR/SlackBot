from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tybot import archiving_bot
from tybot.archive import (
    attachment_reader,
    attachment_writer,
    backfill,
    backfill_adapter,
    files,
    shadow_paths,
    writer,
)
from tybot.archive.files import attachment_storage
from tybot.archive.store import ArchiveStore
from tybot.archive.supervisor_state import JobState
from tybot.attachment_review import scan

CHANNEL_ID = "C12345678"
CHANNEL_NAME = "#team-notices"


def test_channel_root_reuses_first_name_and_refuses_duplicate_identity(tmp_path):
    original = shadow_paths.channel_root(tmp_path, "tyit", CHANNEL_ID, CHANNEL_NAME)
    original.mkdir(parents=True)

    assert shadow_paths.channel_root(tmp_path, "tyit", CHANNEL_ID, "#renamed") == original
    (original.parent / f"{CHANNEL_ID}__other").mkdir()
    with pytest.raises(shadow_paths.ShadowPathError, match="multiple"):
        shadow_paths.channel_root(tmp_path, "tyit", CHANNEL_ID, CHANNEL_NAME)


def test_channel_root_rejects_invalid_coordinates(tmp_path):
    with pytest.raises(shadow_paths.ShadowPathError, match="channel ID"):
        shadow_paths.channel_root(tmp_path, "tyit", "../bad", CHANNEL_NAME)
    with pytest.raises(shadow_paths.ShadowPathError, match="workspace"):
        shadow_paths.channel_root(tmp_path, "../bad", CHANNEL_ID, CHANNEL_NAME)


def test_shadow_raw_writer_is_read_without_changing_live_layout(tmp_path):
    root = tmp_path / "archiver-shadow"
    archive = shadow_paths.archive_dir(root, "tyit", CHANNEL_ID, CHANNEL_NAME)
    result = writer.ingest(
        root, workspace="tyit", channel=CHANNEL_NAME, channel_id=CHANNEL_ID,
        messages=[writer.IncomingMessage(
            datetime(2026, 9, 30, tzinfo=UTC), "U1", "source text",
            source_ts="1790726400.000001",
        )],
        acl=[CHANNEL_NAME], channel_directory=archive,
    )

    assert result.written == 1
    assert result.path.parent == archive / "raw"
    assert ArchiveStore(root).source_files() == [result.path]
    assert not (root / "workspaces").exists()


def test_shadow_collector_writes_events_to_channel_scoped_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(archiving_bot.ingest_ack, "advance", lambda **_: None)
    monkeypatch.setattr(archiving_bot, "record_revision", lambda **_: 1)
    root = tmp_path / "archiver-shadow"
    config = archiving_bot.ArchiverWorkspace(
        "tyit", "archiver-bot", "archiver-app", "T12345678", "U_MASTER",
        frozenset({CHANNEL_ID}), separate_attachments=True,
    )

    class Client:
        def conversations_info(self, *, channel):
            return {"channel": {"id": channel, "name": CHANNEL_NAME.lstrip("#"),
                                "is_member": True}}

        def users_info(self, *, user):
            return {"user": {"name": user}}

    collector = archiving_bot.ShadowCollector(config, root, layout="per-channel-v1")
    event = {"channel_type": "channel", "channel": CHANNEL_ID, "user": "U1",
             "ts": "1790726400.000001", "text": "A new shadow message"}
    assert collector.ingest_event(Client(), event) == "written"
    assert collector.ingest_event(Client(), event) == "duplicate"
    assert len(list((root / "tyit").glob(f"{CHANNEL_ID}__*/archive/raw/*.md"))) == 1


def test_empty_shadow_root_backfill_uses_new_layout_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(archiving_bot.ingest_ack, "advance", lambda **_: None)
    monkeypatch.setattr(archiving_bot, "record_revision", lambda **_: 1)
    root = tmp_path / "archiver-shadow"
    live = tmp_path / "archive"
    live.mkdir()
    config = archiving_bot.ArchiverWorkspace(
        "tyit", "archiver-bot", "archiver-app", "T12345678", "U_MASTER",
        frozenset({CHANNEL_ID}), separate_attachments=True,
    )

    class Client:
        def conversations_info(self, *, channel):
            return {"channel": {"id": channel, "name": CHANNEL_NAME.lstrip("#"),
                                "is_member": True}}

        def conversations_history(self, **_):
            return {"messages": [
                {"ts": "1790726400.000001", "user": "U1", "text": "Backfilled text"},
                {"ts": "1790726401.000001", "bot_id": "B1", "text": "Bot reply"},
            ]}

        def users_info(self, *, user):
            return {"user": {"name": user}}

    client = Client()
    target = backfill.Target("tyit", CHANNEL_ID, CHANNEL_NAME)
    collector = archiving_bot.ShadowCollector(config, root, layout="per-channel-v1")
    preview = backfill.plan(client, [target], workspace="tyit")
    assert preview.found == 1
    assert not root.exists()

    cursors = []
    ingest = backfill_adapter.make_ingest(collector, client)
    counts, state = backfill.run(
        client, [target], workspace="tyit", ingest=ingest,
        save_cursor=lambda _, ts: cursors.append(ts),
    )
    assert (counts.written, counts.duplicate, counts.failed) == (1, 0, 0)
    assert state == JobState.SUCCEEDED
    assert cursors == ["1790726400.000001"]
    source = ArchiveStore(root).source_docs()
    assert len(source) == 1
    assert source[0].path.parent.parent.name == "archive"
    assert "Backfilled text" in source[0].path.read_text(encoding="utf-8")
    assert "Bot reply" not in source[0].path.read_text(encoding="utf-8")
    assert not list(live.rglob("*.md"))

    counts, state = backfill.run(
        client, [target], workspace="tyit", ingest=ingest,
        save_cursor=lambda _, ts: cursors.append(ts),
    )
    assert (counts.written, counts.duplicate, counts.failed) == (0, 1, 0)
    assert state == JobState.SUCCEEDED
    assert len(ArchiveStore(root).source_docs()) == 1


def test_shadow_backfill_keeps_attachment_body_out_of_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(archiving_bot.ingest_ack, "advance", lambda **_: None)
    monkeypatch.setattr(archiving_bot, "record_revision", lambda **_: 1)
    monkeypatch.setattr(files, "download_bytes", lambda *_: b"Attachment evidence from Slack")
    root = tmp_path / "archiver-shadow"
    config = archiving_bot.ArchiverWorkspace(
        "tyit", "archiver-bot", "archiver-app", "T12345678", "U_MASTER",
        frozenset({CHANNEL_ID}), separate_attachments=True,
    )

    class Client:
        def conversations_info(self, *, channel):
            return {"channel": {"id": channel, "name": CHANNEL_NAME.lstrip("#"),
                                "is_member": True}}

        def conversations_history(self, **_):
            return {"messages": [{
                "ts": "1790726400.000001", "user": "U1", "text": "Please review",
                "files": [{"id": "F123", "name": "report.txt", "filetype": "txt",
                           "size": 30, "url_private": "https://example.invalid/report"}],
            }]}

        def users_info(self, *, user):
            return {"user": {"name": user}}

    client = Client()
    collector = archiving_bot.ShadowCollector(config, root, layout="per-channel-v1")
    target = backfill.Target("tyit", CHANNEL_ID, CHANNEL_NAME)
    counts, state = backfill.run(
        client, [target], workspace="tyit",
        ingest=backfill_adapter.make_ingest(collector, client),
        save_cursor=lambda *_: None,
    )
    assert state == JobState.SUCCEEDED
    assert (counts.written, counts.failed) == (1, 0)
    channel = shadow_paths.channel_root(root, "tyit", CHANNEL_ID, CHANNEL_NAME)
    raw = next((channel / "archive" / "raw").glob("*.md")).read_text(encoding="utf-8")
    canonical = next((channel / "archive" / "attachments").glob("*/*.md"))
    assert "Attachment evidence from Slack" not in raw
    assert "[첨부:" in raw
    assert "Attachment evidence from Slack" in canonical.read_text(encoding="utf-8")
    assert next((channel / "objects").glob("*/*")).read_bytes() == b"Attachment evidence from Slack"
    assert (channel / "staging" / "F123" / "metadata.json").is_file()


def test_new_layout_refuses_embedded_attachment_body(tmp_path):
    config = archiving_bot.ArchiverWorkspace(
        "tyit", "archiver-bot", "archiver-app", "T12345678", "U_MASTER",
        frozenset({CHANNEL_ID}), separate_attachments=False,
    )
    with pytest.raises(archiving_bot.ArchiverConfigError, match="requires separate attachments"):
        archiving_bot.ShadowCollector(config, tmp_path, layout="per-channel-v1")


def test_shadow_attachment_has_one_channel_folder_and_reader_uses_real_path(tmp_path):
    root = tmp_path / "archiver-shadow"
    archive = shadow_paths.archive_dir(root, "tyit", CHANNEL_ID, CHANNEL_NAME)
    storage = attachment_storage(
        root, "tyit", CHANNEL_ID, channel_root=archive.parent,
    )
    assert storage.objects_dir == archive.parent / "objects"
    staged = storage.staging_dir / "F123"
    staged.mkdir(parents=True)
    (staged / "metadata.json").write_text(json.dumps({
        "slack_file_id": "F123", "name": "report.pdf", "filetype": "pdf",
        "sha256": "abcdef1234567890", "conversion_state": "succeeded",
        "staged_at": "2026-09-30T00:00:00+00:00",
    }), encoding="utf-8")
    (staged / "extracted.md").write_text("# report.pdf\n\nAttachment evidence\n", encoding="utf-8")

    docs = attachment_writer.write_docs(
        root, [SimpleNamespace(metadata_path=staged / "metadata.json")],
        workspace="tyit", channel_id=CHANNEL_ID, channel=CHANNEL_NAME,
        visibility="private", acl=frozenset({CHANNEL_NAME}),
        channel_archive=archive,
    )
    assert len(docs) == 1
    paths = attachment_reader.source_files(root)
    assert len(paths) == 1
    assert paths[0].parent.parent.parent == archive
    loaded = attachment_reader.load_checked(paths[0], root)
    assert loaded is not None and loaded.text == "Attachment evidence"
    assert attachment_reader.as_archive_doc(loaded, root).path == paths[0]
    assert scan(root)[0].channel_id == CHANNEL_ID
    assert not (root / "tyit" / CHANNEL_ID).exists()


def test_shadow_attachment_path_rejects_other_channel_frontmatter(tmp_path):
    root = tmp_path / "archiver-shadow"
    archive = shadow_paths.archive_dir(root, "tyit", CHANNEL_ID, CHANNEL_NAME)
    path = archive / "attachments" / "F123" / "abcdef123456.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nkind: attachment\nworkspace: tyit\nchannel_id: C99999999\n"
        "channel: '#other'\nfile_id: F123\nrevision: abcdef123456\n"
        "visibility: private\nacl: ['#other']\nconversion_state: succeeded\n"
        "---\n\nUntrusted text\n", encoding="utf-8",
    )
    assert attachment_reader.load_checked(path, root) is None


def test_new_layout_must_be_explicit_to_allow_shadow_next_to_live(tmp_path):
    env = {
        "ARCHIVE_DIR": str(tmp_path / "archive"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "archiver-shadow"),
    }
    with pytest.raises(archiving_bot.ArchiverConfigError, match="share a parent"):
        archiving_bot.shadow_archive_dir(env)
    env["ARCHIVER_SHADOW_LAYOUT"] = "per-channel-v1"
    assert archiving_bot.shadow_archive_dir(env) == tmp_path / "archiver-shadow"
