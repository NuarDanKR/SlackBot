from __future__ import annotations

import inspect
import re
from pathlib import Path

import tybot.embedding_index_policy as index_policy
import tybot.embedding_query_policy as query_policy

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "deploy/sql/search_embedding_query_schema.sql"
INDEX_SCHEMA = ROOT / "deploy/sql/search_embedding_schema.sql"
TABLE = "search_embedding_query_policy"

#: Columns that exist for the audit trail rather than for the decision. Every
#: other column must be read by the policy module -- see the drift test below.
NOT_READ_BY_THE_DECISION = {"workspace", "reason", "updated_at", "updated_by"}


def sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def table_body() -> str:
    body = sql()
    body = body[body.index(f"CREATE TABLE IF NOT EXISTS {TABLE}") :]
    return body[: body.index(");")]


def declarations() -> str:
    """The table body with the prose stripped.

    The comments in these files explain what must *not* be read, so a plain
    substring scan over them always trips. The declarations are what ships.
    """
    return "\n".join(raw.split("--", 1)[0] for raw in table_body().splitlines())


def statement(module) -> str:
    """The SQL a policy module actually executes, prose and docstrings removed."""
    found = re.search(r'"""(SELECT.*?)"""', inspect.getsource(module), re.S)
    assert found, "policy module has no SELECT statement"
    return " ".join(found.group(1).split())


def columns() -> set[str]:
    found = set()
    for raw in table_body().splitlines()[1:]:
        line = raw.split("--", 1)[0].strip()
        name = line.split(" ", 1)[0] if line else ""
        if re.fullmatch(r"[a-z][a-z0-9_]*", name) and name not in {"check", "unique"}:
            found.add(name)
    return found


# --- the table exists, applies in order, and stores no question -------------


def test_the_query_policy_applies_after_the_table_it_references():
    apply = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    listed = []
    inside = False
    for raw in apply.splitlines():
        if not inside:
            inside = raw.strip().startswith("FILES=(")
            continue
        line = raw.split("#", 1)[0].strip()
        if line == ")":
            break
        if line:
            listed.append(line)
    assert "search_embedding_query_schema.sql" in listed
    assert listed.index("search_embedding_schema.sql") < listed.index(
        "search_embedding_query_schema.sql"
    )


def test_the_table_holds_policy_only_and_never_a_question_or_a_vector():
    names = columns()
    assert names
    for forbidden in ("question", "answer", "prompt", "embedding", "vector", "key"):
        assert not any(forbidden in name for name in names)


# --- both switches are off until somebody turns them on ---------------------


def test_both_query_switches_default_to_false():
    body = table_body()
    assert "allow_query                      boolean NOT NULL DEFAULT false" in body
    assert (
        "query_external_transfer_approved boolean NOT NULL DEFAULT false" in body
    )


def test_an_enabled_query_policy_must_name_a_model_and_an_approver():
    body = table_body()
    assert "NOT allow_query OR model_revision IS NOT NULL" in body
    assert "REFERENCES search_embedding_model_revision(revision)" in body
    assert "updated_by                       text NOT NULL" in body


def test_workspace_uses_the_same_bounded_identifier_shape_as_the_request():
    body = table_body()
    assert "workspace ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'" in body


def test_external_approval_cannot_be_stored_on_a_local_policy():
    assert (
        "NOT query_external_transfer_approved OR transport = 'external'" in table_body()
    )


def test_the_revision_counter_is_the_tables_own():
    body = table_body()
    assert "revision                         bigint NOT NULL DEFAULT 1" in body
    assert "CHECK (revision > 0)" in body


# --- the index approval is a different column in a different table ----------


def test_the_query_schema_does_not_touch_the_index_policy_table():
    body = sql()
    assert "search_embedding_workspace_policy" not in body
    assert "ALTER TABLE" not in body


def test_the_approval_column_is_named_so_it_cannot_be_mistaken_for_the_index_one():
    # The index table owns ``external_transfer_approved``. If the query column
    # had the same name, reading the wrong table would still return a boolean
    # and nothing would look broken.
    assert "query_external_transfer_approved" in columns()
    assert "external_transfer_approved" in INDEX_SCHEMA.read_text(encoding="utf-8")
    assert "external_transfer_approved" not in columns()


def test_neither_policy_module_reads_the_others_approval():
    index_sql = statement(index_policy)
    query_sql = statement(query_policy)
    assert "search_embedding_query_policy" not in index_sql
    assert "allow_query" not in index_sql
    assert "search_embedding_workspace_policy" not in query_sql
    assert "query_external_transfer_approved" in query_sql
    assert "external_transfer_approved" not in query_sql.replace(
        "query_external_transfer_approved", ""
    )


# --- drift between the table and the module that reads it -------------------


def test_every_column_the_module_reads_exists_in_the_table():
    referenced = set(re.findall(r"\bq\.([a-z][a-z0-9_]*)", inspect.getsource(query_policy)))
    assert referenced
    assert referenced <= columns()


def test_every_gating_column_in_the_table_is_actually_read():
    source = inspect.getsource(query_policy)
    unread = {
        name
        for name in columns() - NOT_READ_BY_THE_DECISION
        if f"q.{name}" not in source
    }
    assert not unread, f"column added to the schema but never read: {sorted(unread)}"


def test_the_decision_words_and_the_transports_match_the_schema_constraint():
    body = table_body()
    for transport in sorted(query_policy.TRANSPORTS):
        assert f"'{transport}'" in body
    assert {"local", "external"} == query_policy.TRANSPORTS


# --- nothing is switched on, and nothing is indexed yet ---------------------


def test_the_schema_grants_nothing_to_public_and_only_to_the_existing_role():
    body = sql()
    assert f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC" in body
    assert "TO tyslackai" in body
    assert "TO tybot_archiver" not in body


def test_the_schema_inserts_no_row_so_no_workspace_is_enabled_by_applying_it():
    body = sql().upper()
    assert "INSERT INTO" not in body
    assert "UPDATE " not in body


def test_the_schema_builds_no_ann_index_for_a_query_path_that_is_not_wired_yet():
    body = sql().lower()
    assert "using hnsw" not in body
    assert "using ivfflat" not in body
