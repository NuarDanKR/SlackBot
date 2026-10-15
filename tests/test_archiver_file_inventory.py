"""Read-only Archiver discovery must not inherit Master collection authority."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tybot.archive import channel_files
from tybot.archive.file_inventory import preview

CHANNEL = "C0123456789"


class Client:
    def __init__(self):
        self.info = {"id": CHANNEL, "is_member": True, "is_private": True, "name": "board"}
        self.calls = []
        self.fail_page = 0

    def conversations_info(self, **kwargs):
        assert kwargs == {"channel": CHANNEL}
        return {"channel": self.info}

    def files_list(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["page"] == self.fail_page:
            raise RuntimeError("private provider response must not be returned")
        return {"files": [{"id": "F1"}, {"id": f"F{kwargs['page'] + 1}"}],
                "paging": {"pages": 2}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_files, "_page_pause", lambda: 0)
    monkeypatch.setattr(channel_files, "_max_pages", lambda: 0)
    row = {"channel_id": CHANNEL, "membership": "joined", "mode": "active",
           "operator_hold": False}
    repo = SimpleNamespace(channels=lambda workspace: [row])
    return Client(), repo, row, {"live_root": tmp_path / "live",
                               "shadow_root": tmp_path / "shadow"}


def run(setup):
    client, repo, _, roots = setup
    return preview(client, repo, "mgmt", CHANNEL, **roots)


def test_complete_deduplicated_inventory_does_not_write(setup):
    result = run(setup)
    assert result["list_complete"] is True
    assert result["files_seen"] == 3
    assert result["missing_metadata_ids"] == ["F1", "F2", "F3"]
    assert result["scope"] == "channel-files-not-folder"
    assert all(call["channel"] == CHANNEL for call in setup[0].calls)
    assert all(not root.exists() for root in setup[3].values())


def test_metadata_is_not_reported_as_completed_collection(setup):
    for root_key, file_id in [("live_root", "F1"), ("shadow_root", "F2")]:
        path = setup[3][root_key] / "mgmt" / f"{CHANNEL}__board" / "staging" / file_id
        path.mkdir(parents=True)
        (path / "metadata.json").write_text('{"status":"failed"}', encoding="utf-8")
    result = run(setup)
    assert result["live_metadata_records"] == 1
    assert result["shadow_only_ids"] == ["F2"]
    assert result["missing_metadata_ids"] == ["F3"]
    assert "collected" not in result


@pytest.mark.parametrize("change", [
    {"membership": "left"}, {"membership": "unknown"},
    {"mode": "paused"}, {"operator_hold": True},
])
def test_db_collection_policy_blocks_listing(setup, change):
    setup[2].update(change)
    with pytest.raises(PermissionError):
        run(setup)
    assert not setup[0].calls


@pytest.mark.parametrize("change", [
    {"is_member": False}, {"is_im": True}, {"is_mpim": True},
    {"is_archived": True}, {"id": "COTHER"},
])
def test_current_slack_membership_required_even_with_stale_db(setup, change):
    setup[0].info.update(change)
    with pytest.raises(PermissionError):
        run(setup)
    assert not setup[0].calls


@pytest.mark.parametrize("page,seen", [(1, 0), (2, 2)])
def test_page_failure_is_not_a_complete_or_empty_inventory(setup, page, seen):
    setup[0].fail_page = page
    result = run(setup)
    assert result["list_complete"] is False
    assert result["files_seen"] == seen
    assert result["warning_count"] == 1
    assert "private provider response" not in str(result)


def test_page_limit_marks_partial(setup, monkeypatch):
    monkeypatch.setattr(channel_files, "_max_pages", lambda: 1)
    assert run(setup)["list_complete"] is False


def test_overlapping_roots_are_rejected(setup):
    setup[3]["shadow_root"] = setup[3]["live_root"] / "shadow"
    with pytest.raises(ValueError):
        run(setup)
    assert not setup[0].calls


def test_workspace_traversal_rejected_before_slack(setup):
    with pytest.raises(ValueError):
        preview(setup[0], setup[1], "../mgmt", CHANNEL, **setup[3])
    assert not setup[0].calls
