"""Exercise startup SELECT predicates; PostgreSQL grants have separate DB tests."""
import sqlite3
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "deploy" / "sql"


@pytest.mark.parametrize("legacy", [False, True], ids=["connections", "legacy-services"])
@pytest.mark.parametrize("master,enabled,verified,app,expected", [
    (None, True, True, True, True),
    (("U_MASTER", True), True, True, True, True),
    (("U_MASTER", False), True, True, True, False),
    (("", True), True, True, True, False),
    (("U_ARCHIVER", True), True, True, True, False),
    (None, False, True, True, False),
    (None, True, False, True, False),
    (None, True, True, False, False),
])
def test_master_is_optional_but_archiver_startup_guards_remain(
    legacy, master, enabled, verified, app, expected,
):
    file = "workspace_service_schema.sql" if legacy else "bot_connection_schema.sql"
    function = "archiver_runtime_config" if legacy else "archiver_connection_config"
    sql = (SQL_DIR / file).read_text(encoding="utf-8")
    function_sql = sql.split(f"CREATE OR REPLACE FUNCTION {function}", 1)[1]
    query = function_sql.split("AS $$", 1)[1].split("$$;", 1)[0]
    query = query.replace("requested_workspace", ":workspace")
    # These portable SELECTs are evaluated with SQLite to test absent/invalid
    # LEFT JOIN rows. This does not claim to validate PostgreSQL role privileges.
    with sqlite3.connect(":memory:") as conn:
        if legacy:
            conn.executescript("""
                CREATE TABLE workspace_service (
                    workspace TEXT, service TEXT, state TEXT, team_id TEXT,
                    bot_user_id TEXT, identity_ok INTEGER);
                CREATE TABLE workspace_service_secret (
                    workspace TEXT, service TEXT, kind TEXT, ciphertext BLOB);
            """)
            conn.execute(
                "INSERT INTO workspace_service VALUES ('tec', 'archiver', ?, 'T_TEC', 'U_ARCHIVER', ?)",
                ("enabled" if enabled else "disabled", verified),
            )
            for kind in (["bot", "app"] if app else ["bot"]):
                conn.execute(
                    "INSERT INTO workspace_service_secret VALUES ('tec', 'archiver', ?, ?)",
                    (kind, b"encrypted-fixture"),
                )
            if master:
                conn.execute(
                    "INSERT INTO workspace_service VALUES ('tec', 'master', 'enabled', 'T_TEC', ?, ?)",
                    master,
                )
        else:
            conn.executescript("""
                CREATE TABLE bot_connection (
                    id INTEGER, workspace TEXT, bot_key TEXT, connector_type TEXT,
                    state TEXT, team_id TEXT, bot_user_id TEXT, identity_ok INTEGER);
                CREATE TABLE bot_connection_secret (
                    connection_id INTEGER, kind TEXT, ciphertext BLOB);
            """)
            conn.execute(
                "INSERT INTO bot_connection VALUES (1, 'tec', 'archiver', 'slack_socket', ?, 'T_TEC', 'U_ARCHIVER', ?)",
                ("enabled" if enabled else "disabled", verified),
            )
            for kind in (["bot", "app"] if app else ["bot"]):
                conn.execute(
                    "INSERT INTO bot_connection_secret VALUES (1, ?, ?)",
                    (kind, b"encrypted-fixture"),
                )
            if master:
                conn.execute(
                    "INSERT INTO bot_connection VALUES (2, 'tec', 'master', 'slack_socket', 'enabled', 'T_TEC', ?, ?)",
                    master,
                )
        result = conn.execute(query, {"workspace": "tec"}).fetchone()
        assert bool(result) is expected
        if result:
            assert result[3] == (master[0] if master else "")
