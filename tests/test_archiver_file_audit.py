"""Global audit intersects Archiver membership with policy, without syncing or writing."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tybot.archive.file_audit import audit

CHANNEL = "C0123456789"


class Client:
    def __init__(self):
        self.calls = []
        self.joined = [{"id": CHANNEL, "name": "board", "is_private": True}]
        self.fail_list = False
        self.member = True

    def auth_test(self):
        return {"team_id": "test"}

    def users_conversations(self, **kwargs):
        assert "im" not in kwargs["types"]
        return {"channels": self.joined}

    def conversations_info(self, **kwargs):
        return {"channel": {"id": kwargs["channel"], "name": "board",
                            "is_member": self.member, "is_private": True}}

    def files_list(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_list:
            raise RuntimeError("sensitive Slack response")
        return {"files": [{"id": "F123"}], "paging": {"pages": 1}}


@pytest.fixture
def setup(tmp_path):
    clients = {ws: Client() for ws in ["mgmt", "tec"]}
    rows = {ws: [{"channel_id": CHANNEL, "membership": "joined", "mode": "active"}]
            for ws in clients}
    cfgs = {ws: SimpleNamespace(key=ws) for ws in clients}
    args = {
        "config_loader": lambda workspace: cfgs[workspace],
        "client_factory": lambda cfg: clients[cfg.key],
        "repo": SimpleNamespace(channels=lambda workspace: rows[workspace]),
        "live_root": tmp_path / "live", "shadow_root": tmp_path / "shadow",
        "identity_validator": lambda cfg, identity: None, "pause": 0,
    }
    return args, rows, clients


def test_masterless_workspace_included_and_scopes_do_not_mix(setup):
    args, _, clients = setup
    result = audit(["tec", "mgmt", "tec"], **args)
    assert result["complete"] is True
    assert [ws["workspace"] for ws in result["workspaces"]] == ["mgmt", "tec"]
    assert all(ws["channels"][0]["missing_after_record_check"] == ["F123"]
               for ws in result["workspaces"])
    assert all(client.calls == [{"channel": CHANNEL, "count": 100, "page": 1}]
               for client in clients.values())
    assert not args["live_root"].exists()
    assert not args["shadow_root"].exists()


@pytest.mark.parametrize("change", [
    {"operator_hold": True}, {"mode": "paused"}, {"membership": "left"},
])
def test_disallowed_channels_are_not_queried(setup, change):
    args, rows, clients = setup
    rows["mgmt"][0].update(change)
    report = audit(["mgmt"], **args)["workspaces"][0]
    assert report["skipped_channels"] == 1
    assert not clients["mgmt"].calls


def test_unregistered_joined_channel_is_not_automatically_registered(setup):
    args, rows, clients = setup
    rows["mgmt"].clear()
    assert audit(["mgmt"], **args)["workspaces"][0]["skipped_channels"] == 1
    assert not clients["mgmt"].calls


def test_stale_db_membership_is_reported_not_silently_empty(setup):
    args, _, clients = setup
    clients["mgmt"].joined = []
    report = audit(["mgmt"], **args)
    assert report["complete"] is False
    assert report["workspaces"][0]["db_joined_not_in_slack"] == [CHANNEL]


def test_failed_workspace_does_not_stop_others_or_leak_errors(setup):
    args, _, _ = setup
    real_loader = args["config_loader"]

    def load(workspace):
        if workspace == "mgmt":
            raise RuntimeError("secret connection details")
        return real_loader(workspace)

    args["config_loader"] = load
    report = audit(["mgmt", "tec"], **args)
    assert report["complete"] is False
    assert [ws["status"] for ws in report["workspaces"]] == ["failed", "ok"]
    assert "secret" not in str(report)


def test_partial_file_listing_is_not_a_complete_global_result(setup):
    args, _, clients = setup
    clients["mgmt"].fail_list = True
    report = audit(["mgmt"], **args)
    assert report["complete"] is False
    assert report["workspaces"][0]["channels"][0]["list_complete"] is False
    assert "sensitive" not in str(report)


def test_legacy_canonical_is_existing_needs_review_not_missing(setup):
    args, _, _ = setup
    folder = args["live_root"] / "workspaces/mgmt/channels" / CHANNEL / "attachments/F123"
    folder.mkdir(parents=True)
    report = audit(["mgmt"], **args)["workspaces"][0]["channels"][0]
    assert report["missing_after_record_check"] == []
    assert report["existing_needs_review"] == [{"file_id": "F123", "records": ["canonical"]}]


def test_unverified_slack_identity_is_not_allowed_to_list_files(setup):
    args, _, clients = setup

    def deny(*args):
        raise PermissionError("wrong workspace token")

    args["identity_validator"] = deny
    assert audit(["mgmt"], **args)["complete"] is False
    assert not clients["mgmt"].calls


def test_empty_registry_is_not_a_successful_audit(setup):
    assert audit([], **setup[0])["complete"] is False
