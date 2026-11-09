from __future__ import annotations

import pytest

from tybot.embedding_index_job import IndexRequest
from tybot.embedding_index_policy import authorize_index_request


class Cursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.cur = Cursor(row)

    def cursor(self):
        return self.cur


def request(kind="channel", model="embed-v1"):
    return IndexRequest("tyit", "C1", "hidden.md", "a" * 64, kind, 100, model)


def row(**changes):
    values = {
        "enabled": True,
        "model": "embed-v1",
        "transport": "local",
        "revision": 3,
        "state": "active",
        "channel": True,
        "attachment": True,
        "dm": False,
        "workspace_external": False,
        "model_external": False,
        "provider": "openai",
    }
    values.update(changes)
    return tuple(values.values())


def test_missing_policy_is_denied_without_reading_source_coordinates():
    conn = Connection(None)
    got = authorize_index_request(conn, request())
    assert not got.allowed and got.policy_revision == "unconfigured"
    sql, params = conn.cur.calls[0]
    assert params == ("tyit",)
    assert "doc_path" not in sql and "source_revision" not in sql


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"model": "other-v1"},
        {"state": "draft"},
        {"channel": False},
    ],
)
def test_closed_policy_states_never_authorize(changes):
    assert not authorize_index_request(Connection(row(**changes)), request()).allowed


def test_external_active_policy_authorizes_only_explicit_kinds():
    policy = row(
        transport="external", workspace_external=True, model_external=True
    )
    assert authorize_index_request(Connection(policy), request("channel")).allowed
    assert authorize_index_request(Connection(policy), request("attachment")).allowed
    assert not authorize_index_request(Connection(policy), request("dm")).allowed


def test_dm_requires_its_own_switch():
    got = authorize_index_request(Connection(row(
        dm=True, transport="external", workspace_external=True, model_external=True
    )), request("dm"))
    assert got.allowed


@pytest.mark.parametrize(
    ("workspace_approved", "model_approved", "allowed"),
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
)
def test_external_transfer_requires_both_approvals(
    workspace_approved, model_approved, allowed
):
    got = authorize_index_request(
        Connection(
            row(
                transport="external",
                workspace_external=workspace_approved,
                model_external=model_approved,
            )
        ),
        request(),
    )
    assert got.allowed is allowed
    assert got.external_transfer_approved is allowed


def test_a_hosted_provider_cannot_be_disguised_as_local():
    got = authorize_index_request(Connection(row(transport="local")), request())
    assert not got.allowed
    assert got.transport == "external"
    assert not got.external_transfer_approved


def test_an_unclassified_provider_fails_closed():
    with pytest.raises(ValueError, match="embedding_model_provider_invalid"):
        authorize_index_request(Connection(row(provider="new-provider")), request())


@pytest.mark.parametrize(
    "bad",
    [
        row(transport="other"),
        row(revision=0),
        row(revision=True),
        (True,),
    ],
)
def test_corrupt_policy_rows_fail_closed_with_fixed_codes(bad):
    with pytest.raises(ValueError, match="embedding_policy_"):
        authorize_index_request(Connection(bad), request())


def test_policy_revision_is_bound_to_workspace_revision():
    got = authorize_index_request(Connection(row(revision=19)), request())
    assert got.policy_revision == "workspace:tyit:r19"
