"""File-only recovery preserves evidence without inventing Slack messages."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tybot.archive import channel_files, file_recovery, files

CHANNEL = "C0123456789"


class Client:
    def __init__(self):
        self.info = {"id": CHANNEL, "name": "board", "is_private": True, "is_member": True}
        self.file = {"id": "F123", "name": "minutes.txt", "filetype": "txt", "size": 16,
                     "user": "UHUMAN", "groups": [CHANNEL],
                     "permalink": "https://example.slack.com/files/UHUMAN/F123/minutes.txt",
                     "url_private_download": "https://files.slack.com/fixture"}

    def conversations_info(self, **kwargs):
        return {"channel": self.info}

    def files_info(self, **kwargs):
        return {"file": self.file}

    def files_list(self, **kwargs):
        assert kwargs["channel"] == CHANNEL
        return {"files": [self.file], "paging": {"pages": 1}}

    def users_info(self, **kwargs):
        return {"user": {"id": kwargs["user"], "is_bot": False}}


@pytest.fixture
def case(tmp_path, monkeypatch):
    live, shadow = tmp_path / "live", tmp_path / "shadow"
    live.mkdir()
    shadow.mkdir()
    client = Client()
    row = {"channel_id": CHANNEL, "mode": "active", "membership": "joined"}
    repo = SimpleNamespace(channels=lambda workspace: [row])
    owner = SimpleNamespace(archiver_live_flag=lambda: True,
                            archiver_may_write_live=lambda channel, archiver_flag: archiver_flag)
    cfg = SimpleNamespace(key="mgmt", bot_token="test-credential", bot_user_id="UARCHIVER",
                          master_bot_user_id="UMASTER")
    downloads = []

    def download(file, token):
        downloads.append(file.id)
        return b"The board approved the annual plan."

    monkeypatch.setattr(files, "download_bytes", download)
    monkeypatch.setattr(channel_files, "_page_pause", lambda: 0)
    monkeypatch.setattr(channel_files, "_max_pages", lambda: 0)
    return SimpleNamespace(client=client, repo=repo, owner=owner, cfg=cfg, row=row,
                           live=live, shadow=shadow, downloads=downloads,
                           base=live / "mgmt" / f"{CHANNEL}__board")


def run(case, *, apply=True, indexer=None):
    return file_recovery.recover(
        case.client, case.repo, case.owner, case.cfg, ["F123"],
        live_root=case.live, shadow_root=case.shadow, channel_id=CHANNEL,
        apply=apply, actor="test-operator", indexer=indexer,
    )["results"][0]


def test_plan_never_downloads_or_creates_files(case):
    assert run(case, apply=False)["status"] == "missing"
    assert not case.downloads
    assert not list(case.live.rglob("*"))


def test_apply_uses_existing_conversion_without_fabricating_raw_or_ack(case):
    item = run(case)
    assert item["status"] == "published-index-pending"
    assert item["original_verified"] is True
    assert case.downloads == ["F123"]
    assert not list(case.live.rglob("raw"))
    meta = json.loads((case.base / "staging/F123/metadata.json").read_text(encoding="utf-8"))
    assert "origin_message_ts" not in meta
    assert meta["archive_state"] == "pending"  # No fake message ACK confirmation.
    doc, = (case.base / "archive/attachments/F123").glob("*.md")
    text = doc.read_text(encoding="utf-8")
    assert "message_ts:" not in text
    assert "https://example.slack.com/files/" in text
    assert 'visibility: private' in text


def test_index_is_separate_and_idempotent(case):
    run(case)
    indexed = []
    for _ in range(2):
        result = file_recovery.index_recovered(
            case.live, "mgmt", CHANNEL, ["F123"],
            indexer=lambda docs, root: indexed.extend(docs),
        )
        assert result["results"][0]["status"] == "recovered"
    assert len(indexed) == 2
    assert all(doc.workspace == "mgmt" and doc.channel_id == CHANNEL for doc in indexed)
    assert all(doc.visibility == "private" for doc in indexed)
    assert case.downloads == ["F123"]


def test_repeat_apply_does_not_redownload_or_duplicate_canonical(case):
    run(case)
    assert run(case)["status"] == "published-index-pending"
    assert case.downloads == ["F123"]
    assert len(list((case.base / "archive/attachments/F123").glob("*.md"))) == 1


@pytest.mark.parametrize("kind", ["staging", "objects", "canonical", "legacy", "raw"])
def test_existing_artifacts_require_review_not_download(case, kind):
    paths = {
        "staging": case.base / "staging/F123/metadata.json",
        "objects": case.base / "objects/F123/minutes.txt",
        "canonical": case.base / "archive/attachments/F123/old.md",
        "legacy": case.live.parent / "staging/workspaces/mgmt/channels" / CHANNEL
                  / "attachments/F123/metadata.json",
        "raw": case.base / "archive/raw/2026-09-21.md",
    }
    path = paths[kind]
    path.parent.mkdir(parents=True)
    path.write_text("original reference F123", encoding="utf-8")
    before = path.read_bytes()
    assert run(case)["status"] == "existing-needs-review"
    assert not case.downloads
    assert path.read_bytes() == before


def test_shadow_records_are_not_automatically_promoted(case):
    path = case.shadow / "mgmt" / f"{CHANNEL}__board" / "staging/F123/metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    assert run(case)["status"] == "existing-needs-review"
    assert not case.downloads


def test_revoked_file_share_is_not_downloaded(case):
    case.client.file["groups"] = []
    assert run(case)["status"] == "failed"
    assert not case.downloads


@pytest.mark.parametrize("user", ["UARCHIVER", "UMASTER"])
def test_bot_output_is_not_reingested(case, user):
    case.client.file["user"] = user
    assert run(case)["status"] == "failed"
    assert not case.downloads


def test_hold_prevents_any_file_download(case):
    case.row["operator_hold"] = True
    with pytest.raises(PermissionError):
        run(case)
    assert not case.downloads


def test_gate_disabled_prevents_download(case):
    case.owner.archiver_live_flag = lambda: False
    with pytest.raises(PermissionError):
        run(case)
    assert not case.downloads


def test_share_revoked_during_conversion_prevents_canonical_publication(case, monkeypatch):
    def download(file, token):
        case.client.file["groups"] = []
        return b"The board approved the annual plan."

    monkeypatch.setattr(files, "download_bytes", download)
    assert run(case)["status"] == "failed"
    assert not list(case.live.rglob("archive/attachments/*/*.md"))


def test_original_hash_mismatch_prevents_indexing(case):
    run(case)
    original, = (case.base / "objects/F123").iterdir()
    original.write_bytes(b"changed")
    result = file_recovery.index_recovered(
        case.live, "mgmt", CHANNEL, ["F123"],
        indexer=lambda *args: pytest.fail("must not index"),
    )
    assert result["results"][0]["status"] == "failed"


def test_index_failure_can_resume_without_download(case):
    def fail(*args):
        raise RuntimeError("private database response")

    assert run(case, indexer=fail)["status"] == "failed"
    assert run(case, indexer=lambda *args: None)["status"] == "recovered"
    assert case.downloads == ["F123"]


def test_pii_block_is_not_published(case, monkeypatch):
    from tybot import pii_screen

    monkeypatch.setattr(pii_screen, "screen_document", lambda *args, **kwargs:
                        pii_screen.ScreenResult(True, "fixture", ()))
    item = run(case)
    assert item["status"] == "retained-not-searchable"
    assert item["conversion_state"] == "blocked"
    assert not list(case.live.rglob("archive/attachments/*/*.md"))


@pytest.mark.parametrize("user_info", [
    {"id": "UHUMAN", "is_bot": True},
    {"id": "UHUMAN", "is_bot": False, "is_app_user": True},
    {"id": "UHUMAN"}, {},
])
def test_other_bots_and_unverified_uploaders_fail_closed(case, user_info):
    case.client.users_info = lambda **kwargs: {"user": user_info}
    assert run(case)["status"] == "failed"
    assert not case.downloads
