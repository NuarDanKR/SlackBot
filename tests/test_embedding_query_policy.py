from __future__ import annotations

import dataclasses
import inspect

import pytest

from tybot.embedding_index_job import IndexAuthorization
from tybot.embedding_query_policy import (
    QueryAuthorization,
    QueryNotAuthorized,
    QueryRequest,
    authorize_query_request,
    require_query_authorization,
)


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


def request(model="embed-v1", transport="local"):
    return QueryRequest("tyit", model, transport)


def row(**changes):
    values = {
        "allow_query": True,
        "model": "embed-v1",
        "transport": "local",
        "revision": 3,
        "query_external": False,
        "state": "active",
        "model_external": False,
    }
    values.update(changes)
    return tuple(values.values())


# --- 1. the question never reaches the decision -----------------------------


def test_the_decision_takes_metadata_only_and_has_no_place_to_put_a_question():
    names = {f.name for f in dataclasses.fields(QueryRequest)}
    assert names == {"workspace", "model_revision", "transport"}
    parameters = set(inspect.signature(authorize_query_request).parameters)
    assert parameters == {"connection", "request"}


@pytest.mark.parametrize(
    "field",
    ["workspace", "model_revision", "transport"],
)
def test_a_question_cannot_ride_in_through_a_metadata_field(field):
    values = {"workspace": "tyit", "model_revision": "embed-v1", "transport": "local"}
    values[field] = "김 과장 연봉이 얼마야?"
    with pytest.raises(ValueError, match="embedding_query_request_invalid"):
        QueryRequest(**values)


@pytest.mark.parametrize("value", ["local\n", "embed-v1\n", "tyit\n"])
def test_a_trailing_newline_cannot_pass_as_an_identifier(value):
    values = {"workspace": "tyit", "model_revision": "embed-v1", "transport": "local"}
    field = {
        "local\n": "transport",
        "embed-v1\n": "model_revision",
        "tyit\n": "workspace",
    }[value]
    values[field] = value

    with pytest.raises(ValueError, match="embedding_query_request_invalid"):
        QueryRequest(**values)


def test_the_statement_names_no_question_column():
    conn = Connection(row())
    authorize_query_request(conn, request())
    sql, _ = conn.cur.calls[0]
    for forbidden in ("question", "answer", "prompt", "text"):
        assert forbidden not in sql.lower()


# --- 2. the index approval is not the query approval ------------------------


def test_the_index_policy_table_is_not_read_at_all():
    conn = Connection(row())
    authorize_query_request(conn, request())
    sql, params = conn.cur.calls[0]
    assert "search_embedding_workspace_policy" not in sql
    assert "search_embedding_query_policy" in sql
    assert params == ("tyit",)


def test_only_the_query_prefixed_transfer_approval_is_read():
    conn = Connection(row())
    authorize_query_request(conn, request())
    sql, _ = conn.cur.calls[0]
    assert "query_external_transfer_approved" in sql
    # The index column is a substring of the query one, so strip it first and
    # make sure nothing is left over -- that is the actual mistake being
    # guarded against here.
    assert "external_transfer_approved" not in sql.replace(
        "query_external_transfer_approved", ""
    )


def test_a_query_path_refuses_an_index_decision_outright():
    index = IndexAuthorization(
        allowed=True,
        model_revision="embed-v1",
        policy_revision="workspace:tyit:r3",
        transport="external",
        external_transfer_approved=True,
    )
    with pytest.raises(TypeError):
        require_query_authorization(
            index, model_revision="embed-v1", transport="external"
        )


def test_the_two_policy_revisions_cannot_be_confused_in_an_audit():
    got = authorize_query_request(Connection(row(revision=19)), request())
    assert got.policy_revision == "query:tyit:r19"
    assert not got.policy_revision.startswith("workspace:")


# --- 3. both switches are closed until someone opens them -------------------


def test_an_unconfigured_workspace_is_denied_before_anything_else():
    conn = Connection(None)
    got = authorize_query_request(conn, request())
    assert not got.allowed
    assert got.decision == "unconfigured"
    assert got.external_transfer_approved is False


@pytest.mark.parametrize(
    ("changes", "decision"),
    [
        ({"allow_query": False}, "query_disabled"),
        ({"allow_query": None}, "query_disabled"),
        ({"model": "other-v1"}, "model_mismatch"),
        ({"model": None}, "model_mismatch"),
        ({"state": "draft"}, "model_inactive"),
        ({"state": None}, "model_inactive"),
    ],
)
def test_closed_states_deny_with_a_fixed_word(changes, decision):
    got = authorize_query_request(Connection(row(**changes)), request())
    assert not got.allowed
    assert got.decision == decision


def test_an_open_local_policy_allows_without_any_external_approval():
    got = authorize_query_request(Connection(row()), request())
    assert got.allowed
    assert got.decision == "allowed"
    assert got.external_transfer_approved is False


# --- 4. external transfer of a question -------------------------------------


@pytest.mark.parametrize(
    ("query_approved", "model_approved", "allowed"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_sending_a_question_out_needs_the_query_row_and_the_model_row(
    query_approved, model_approved, allowed
):
    got = authorize_query_request(
        Connection(
            row(
                transport="external",
                query_external=query_approved,
                model_external=model_approved,
            )
        ),
        request(transport="external"),
    )
    assert got.allowed is allowed
    assert got.external_transfer_approved is allowed
    if not allowed:
        assert got.decision == "query_external_not_approved"


@pytest.mark.parametrize(
    ("policy_transport", "asked"),
    [("local", "external"), ("external", "local")],
)
def test_a_transport_the_caller_is_not_actually_using_is_refused(
    policy_transport, asked
):
    got = authorize_query_request(
        Connection(
            row(transport=policy_transport, query_external=True, model_external=True)
        ),
        request(transport=asked),
    )
    assert not got.allowed
    assert got.decision == "transport_mismatch"


def test_an_allowed_external_decision_cannot_be_built_without_the_approval():
    with pytest.raises(ValueError, match="query_external_transfer_not_approved"):
        QueryAuthorization(
            allowed=True,
            model_revision="embed-v1",
            policy_revision="query:tyit:r1",
            transport="external",
            decision="allowed",
        ).checked("embed-v1")


def test_an_allowed_flag_that_disagrees_with_its_decision_is_rejected():
    with pytest.raises(ValueError, match="query_authorization_decision_invalid"):
        QueryAuthorization(
            allowed=True,
            model_revision="embed-v1",
            policy_revision="query:tyit:r1",
            transport="local",
            decision="query_disabled",
        ).checked("embed-v1")


# --- 5. corrupt rows fail closed --------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        row(transport="other"),
        row(transport=None),
        row(revision=0),
        row(revision=True),
        (True,),
    ],
)
def test_corrupt_policy_rows_fail_closed_with_fixed_codes(bad):
    with pytest.raises(ValueError, match="embedding_query_policy_"):
        authorize_query_request(Connection(bad), request())


def test_a_request_of_the_wrong_type_is_refused_before_the_database_is_touched():
    conn = Connection(row())
    with pytest.raises(TypeError):
        authorize_query_request(conn, ("tyit", "embed-v1", "local"))
    assert conn.cur.calls == []


# --- 6. the gate a future query path will call ------------------------------


def test_the_gate_passes_an_allowed_decision_through_unchanged():
    got = authorize_query_request(Connection(row()), request())
    assert (
        require_query_authorization(got, model_revision="embed-v1", transport="local")
        is got
    )


def test_the_gate_refuses_a_denied_decision_with_its_own_word():
    got = authorize_query_request(Connection(row(allow_query=False)), request())
    with pytest.raises(QueryNotAuthorized) as caught:
        require_query_authorization(got, model_revision="embed-v1", transport="local")
    assert caught.value.decision == "query_disabled"


@pytest.mark.parametrize(
    ("model", "transport", "decision"),
    [
        ("embed-v2", "local", "model_mismatch"),
        ("embed-v1", "external", "transport_mismatch"),
    ],
)
def test_the_gate_rechecks_what_the_caller_is_about_to_use(model, transport, decision):
    got = authorize_query_request(Connection(row()), request())
    with pytest.raises(QueryNotAuthorized) as caught:
        require_query_authorization(got, model_revision=model, transport=transport)
    assert caught.value.decision == decision


def test_a_decision_word_outside_the_fixed_set_cannot_be_raised():
    with pytest.raises(ValueError, match="unknown query decision"):
        QueryNotAuthorized("looked_fine_to_me")
    with pytest.raises(ValueError, match="unknown query decision"):
        QueryNotAuthorized("allowed")


# --- 7. nothing here calls a provider or searches ---------------------------


def test_the_module_imports_no_provider_and_no_search_path():
    import tybot.embedding_query_policy as module

    source = inspect.getsource(module)
    for forbidden in (
        "embedding_gateway",
        "embedding_search",
        "gateway.providers",
        "openai",
        "requests",
    ):
        assert forbidden not in source
