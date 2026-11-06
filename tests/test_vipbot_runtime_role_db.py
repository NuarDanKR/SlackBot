"""B-83 VIPBot 런타임 역할 권한 — **실제 PostgreSQL 이 있어야 하는** 시험.

`TEST_DATABASE_URL` 에 **버려도 되는 전용 DB** 를 준다. 운영·계측 DB 는 거절한다.

여기서 보는 것은 「GRANT 문이 파일에 있나」 가 아니라 **그 역할로 실제로 무엇을
할 수 있나** 다. 둘은 다르다 — 상속 경로가 끊기거나 나중에 권한이 덧붙으면
파일은 그대로인데 계정은 달라져 있다.

1. 런타임 역할은 `vipbot_connection_config(text)` **실행만** 된다
2. `bot_connection` · `bot_connection_secret` 직접 SELECT 는 **거부**된다
3. PUBLIC 은 그 함수를 실행할 수 없다
4. 다른 봇의 토큰은 그 함수로도 나오지 않는다
5. **스키마를 다시 적용해도** 위 넷이 그대로다(권한 드리프트 없음)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_schema_isolated as verify  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "TEST_DATABASE_URL is absent; the VIPBot runtime role's execute-only "
        "grant, its inability to read bot_connection/bot_connection_secret, "
        "the PUBLIC denial, other bots' token isolation, and privilege drift "
        "across a schema re-apply are all UNVERIFIED in this session"
    ),
)

GROUP = "tybot_vipbot"
RUNTIME = "tybot_vipbot_runtime"
DEFINER = "tybot_vipbot_definer"
CONFIG_FN = "public.vipbot_connection_config(text)"
ACCESS_FN = "public.vipbot_authorization(text,text)"
SECRET_TABLES = (
    "bot_connection",
    "bot_connection_secret",
    "employee",
    "user_identity",
    "bot_access",
    "bot_source_channel",
)

#: 스키마를 적용할 순서. `deploy/apply-schema.sh` 의 목록에서 뽑는다 — 여기에
#: 손으로 베껴 두면 파일이 하나 늘어날 때마다 이 시험만 옛 순서로 돈다.
_UPTO = "vipbot_access_schema.sql"


def _ordered_files() -> tuple[str, ...]:
    text = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    body = text.split("FILES=(", 1)[1].split("\n)", 1)[0]
    names: list[str] = []
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return tuple(names[: names.index(_UPTO) + 1])


@pytest.fixture(scope="module")
def conn():
    refusal = verify.operational_db_refusal(DSN)
    if refusal:
        pytest.fail(f"refusing a non-isolated TEST_DATABASE_URL: {refusal}")
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(DSN, autocommit=True)
    created: list[str] = []
    granted_definer = False
    granted_runtime = False
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
                ([GROUP, RUNTIME, DEFINER],),
            )
            existing = [str(row[0]) for row in cur.fetchall()]
            if existing:
                pytest.fail(
                    "refusing a PostgreSQL cluster with pre-existing VIPBot roles: "
                    + ", ".join(existing)
                )
            # 그룹은 스키마가 **있을 때만** 권한을 준다. 그래서 스키마보다 먼저
            # 만든다 — 순서가 뒤집히면 GRANT 가 통째로 건너뛰어지고, 그 결과는
            # 「권한 없음」 이라 시험이 통과한 것처럼 보인다.
            for role, attrs in (
                (GROUP, "NOLOGIN"),
                (RUNTIME, "LOGIN INHERIT"),
                (DEFINER, "NOLOGIN"),
            ):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE ROLE {role} {attrs}")
                    created.append(role)
            cur.execute(f"GRANT {GROUP} TO {RUNTIME}")
            cur.execute(f"GRANT {DEFINER} TO CURRENT_USER")
            granted_definer = True
            # `SET ROLE` 로 실제 계정이 되어 보려면 멤버여야 한다. 비밀번호 없이
            # **진짜 거부**를 받아 보는 유일한 길이다.
            cur.execute("SELECT current_user")
            cur.execute(f"GRANT {RUNTIME} TO CURRENT_USER")
            granted_runtime = True
        verify.apply_files(connection, _ordered_files())
        with connection.cursor() as cur:
            cur.execute(f"REVOKE {DEFINER} FROM CURRENT_USER")
            granted_definer = False
        yield connection
    finally:
        try:
            with connection.cursor() as cur:
                if granted_definer:
                    cur.execute(f"REVOKE {DEFINER} FROM CURRENT_USER")
                if granted_runtime:
                    cur.execute(f"REVOKE {RUNTIME} FROM CURRENT_USER")
                for role in reversed(created):
                    cur.execute(f"DROP OWNED BY {role}")
                    cur.execute(f"DROP ROLE {role}")
        finally:
            connection.close()


def as_runtime(connection):
    """런타임 역할 그 자체로 한 문장을 돌린다."""

    class _Session:
        def __enter__(self):
            self.cur = connection.cursor()
            self.cur.execute(f"SET ROLE {RUNTIME}")
            return self.cur

        def __exit__(self, *_):
            self.cur.execute("RESET ROLE")
            self.cur.close()
            return False

    return _Session()


@pytest.fixture
def connections(conn):
    """VIPBot 하나와 Master 하나. 둘 다 토큰 암호문을 들고 있다."""
    workspace = "vip-" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace (key, label, archive_path, created_by)"
            " VALUES (%s, %s, %s, 'pytest')"
            " ON CONFLICT (key) DO NOTHING",
            (workspace, "VIPBot test workspace", f"/pytest/{workspace}"),
        )
        for bot_key, suffix in (("vipbot", "VIP"), ("master", "MASTER")):
            cur.execute(
                """INSERT INTO bot_connection
                       (workspace, bot_key, connector_type, state, team_id,
                        bot_user_id, identity_ok)
                   VALUES (%s, %s, 'slack_socket', 'enabled', %s, %s, true)
                   RETURNING id""",
                (workspace, bot_key, f"T{suffix}", f"U{suffix}"),
            )
            connection_id = cur.fetchone()[0]
            for kind in ("bot", "app"):
                cur.execute(
                    """INSERT INTO bot_connection_secret
                           (connection_id, kind, ciphertext, mask, updated_by)
                       VALUES (%s, %s, %s, %s, 'pytest')""",
                    (
                        connection_id,
                        kind,
                        f"cipher-{suffix}-{kind}".encode(),
                        f"synthetic-{suffix}-{kind}",
                    ),
                )
    return workspace


@pytest.fixture
def authorization_rows(conn):
    suffix = uuid.uuid4().hex[:8]
    workspace = f"vip-{suffix}"
    slack_user = "U" + suffix.upper()
    emp_no = "VIP" + suffix.upper()
    channel_id = "C" + suffix.upper()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO employee (emp_no, name) VALUES (%s, %s)",
            (emp_no, "VIP test user"),
        )
        cur.execute(
            "INSERT INTO user_identity"
            " (workspace, slack_user, emp_no, verified_by)"
            " VALUES (%s, %s, %s, 'manual')",
            (workspace, slack_user, emp_no),
        )
        cur.execute(
            "INSERT INTO bot_access (bot, emp_no, reason, granted_by)"
            " VALUES ('vipbot', %s, 'test', 'pytest')",
            (emp_no,),
        )
        cur.execute(
            "INSERT INTO bot_source_channel"
            " (bot, workspace, channel_id, reason, added_by)"
            " VALUES ('vipbot', %s, %s, 'test', 'pytest')",
            (workspace, channel_id),
        )
    try:
        yield workspace, slack_user, emp_no, channel_id
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_source_channel"
                " WHERE bot='vipbot' AND workspace=%s AND channel_id=%s",
                (workspace, channel_id),
            )
            cur.execute("DELETE FROM bot_access WHERE bot='vipbot' AND emp_no=%s", (emp_no,))
            cur.execute(
                "DELETE FROM user_identity WHERE workspace=%s AND slack_user=%s",
                (workspace, slack_user),
            )
            cur.execute("DELETE FROM employee WHERE emp_no=%s", (emp_no,))


# --- 1·2. 함수 실행만 된다 ---------------------------------------------------


def test_the_runtime_role_can_execute_the_one_function_it_was_made_for(
    conn, connections
):
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM public.vipbot_connection_config(%s)", (connections,))
        rows = cur.fetchall()
    assert len(rows) == 1


def test_the_runtime_role_receives_only_its_decision_and_managed_sources(
    conn, authorization_rows
):
    workspace, slack_user, emp_no, channel_id = authorization_rows
    with as_runtime(conn) as cur:
        cur.execute(
            "SELECT * FROM public.vipbot_authorization(%s, %s)",
            (workspace, slack_user),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    code, actual_emp_no, grant_id, source_workspace, actual_channel_id = rows[0]
    assert (code, actual_emp_no, source_workspace, actual_channel_id) == (
        "allowed",
        emp_no,
        workspace,
        channel_id,
    )
    assert type(grant_id) is int and grant_id > 0


def test_an_unmapped_slack_user_receives_no_source_coordinates(conn):
    with as_runtime(conn) as cur:
        cur.execute(
            "SELECT * FROM public.vipbot_authorization(%s, %s)",
            ("tyit", "UUNKNOWN00"),
        )
        rows = cur.fetchall()

    assert rows == [("no_identity", None, None, None, None)]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            "UPDATE employee SET active=false WHERE emp_no=%s",
            "inactive_employee",
        ),
        (
            "UPDATE bot_access SET granted_at=now()-interval '2 days', "
            "expires_at=now()-interval '1 day' "
            "WHERE bot='vipbot' AND emp_no=%s AND revoked_at IS NULL",
            "expired",
        ),
        (
            "UPDATE bot_access SET revoked_at=now(), revoked_by='pytest', "
            "revoke_reason='test' "
            "WHERE bot='vipbot' AND emp_no=%s AND revoked_at IS NULL",
            "no_grant",
        ),
    ],
)
def test_inactive_expired_or_revoked_people_receive_no_sources(
    conn, authorization_rows, mutation, expected
):
    workspace, slack_user, emp_no, _channel_id = authorization_rows
    with conn.cursor() as cur:
        cur.execute(mutation, (emp_no,))
    with as_runtime(conn) as cur:
        cur.execute(
            "SELECT * FROM public.vipbot_authorization(%s, %s)",
            (workspace, slack_user),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == expected
    assert rows[0][3:] == (None, None)


def test_removing_every_managed_source_returns_no_sources(
    conn, authorization_rows
):
    workspace, slack_user, emp_no, _channel_id = authorization_rows
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM bot_source_channel"
            " WHERE bot='vipbot' AND removed_at IS NULL"
        )
        assert cur.fetchone()[0] == 1, "isolated DB contains unrelated VIP sources"
        cur.execute(
            "UPDATE bot_source_channel SET removed_at=now(),"
            " removed_by='pytest', remove_reason='test'"
            " WHERE bot='vipbot' AND removed_at IS NULL"
        )
    with as_runtime(conn) as cur:
        cur.execute(
            "SELECT * FROM public.vipbot_authorization(%s, %s)",
            (workspace, slack_user),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    code, actual_emp_no, grant_id, source_workspace, channel_id = rows[0]
    assert (code, actual_emp_no, source_workspace, channel_id) == (
        "no_sources",
        emp_no,
        None,
        None,
    )
    assert type(grant_id) is int and grant_id > 0


@pytest.mark.parametrize("table", SECRET_TABLES)
def test_the_runtime_role_cannot_read_the_token_tables_directly(conn, table):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_runtime(conn) as cur:
            cur.execute(f"SELECT * FROM {table} LIMIT 1")


@pytest.mark.parametrize("table", SECRET_TABLES)
@pytest.mark.parametrize(
    "privilege",
    ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"],
)
def test_no_table_privilege_is_granted_on_the_token_tables(conn, table, privilege):
    assert verify.has_privilege(conn, RUNTIME, table, privilege) is False
    assert verify.has_privilege(conn, GROUP, table, privilege) is False


def test_the_execute_grant_reaches_the_runtime_through_the_group(conn):
    assert verify.has_function_privilege(conn, GROUP, CONFIG_FN, "EXECUTE") is True
    assert verify.has_function_privilege(conn, RUNTIME, CONFIG_FN, "EXECUTE") is True
    assert verify.has_function_privilege(conn, GROUP, ACCESS_FN, "EXECUTE") is True
    assert verify.has_function_privilege(conn, RUNTIME, ACCESS_FN, "EXECUTE") is True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolinherit FROM pg_roles WHERE rolname=%s", (GROUP,)
        )
        assert cur.fetchone()[0] is False


def test_the_two_windows_have_one_closed_definer_owner(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text, r.rolname"
            " FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner"
            " WHERE p.oid IN (%s::regprocedure, %s::regprocedure)"
            " ORDER BY p.proname",
            (CONFIG_FN, ACCESS_FN),
        )
        owners = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM pg_auth_members a"
            " JOIN pg_roles parent_role ON parent_role.oid=a.roleid"
            " JOIN pg_roles child_role ON child_role.oid=a.member"
            " WHERE parent_role.rolname=%s OR child_role.rolname=%s",
            (DEFINER, DEFINER),
        )
        memberships = cur.fetchone()[0]

    assert len(owners) == 2
    assert {owner for _function, owner in owners} == {DEFINER}
    assert memberships == 0


def test_the_runtime_cannot_execute_another_security_definer(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
            " WHERE n.nspname='public' AND p.prosecdef"
            " AND p.oid NOT IN (%s::regprocedure, %s::regprocedure)"
            " AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (CONFIG_FN, ACCESS_FN, RUNTIME),
        )
        unexpected = cur.fetchall()

    assert unexpected == []


# --- 3. PUBLIC ---------------------------------------------------------------


def _public_can_execute(connection, function: str) -> bool:
    """Read PUBLIC's effective function ACL; PUBLIC is not a pg_roles row."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(bool_or(acl.grantee = 0"
            " AND acl.privilege_type = 'EXECUTE'), false)"
            " FROM pg_proc p"
            " CROSS JOIN LATERAL aclexplode("
            " COALESCE(p.proacl, acldefault('f', p.proowner))) AS acl"
            " WHERE p.oid = %s::regprocedure",
            (function,),
        )
        return bool(cur.fetchone()[0])


def _public_has_table_privilege(
    connection, table: str, privilege: str
) -> bool:
    """Read PUBLIC's effective table ACL without pretending it is a role."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(bool_or(acl.grantee = 0"
            " AND acl.privilege_type = %s), false)"
            " FROM pg_class c"
            " CROSS JOIN LATERAL aclexplode("
            " COALESCE(c.relacl, acldefault('r', c.relowner))) AS acl"
            " WHERE c.oid = %s::regclass",
            (privilege, table),
        )
        return bool(cur.fetchone()[0])


def test_public_cannot_execute_the_vipbot_function(conn):
    assert _public_can_execute(conn, CONFIG_FN) is False
    assert _public_can_execute(conn, ACCESS_FN) is False


def test_a_role_outside_the_group_cannot_execute_it_either(conn):
    outsider = "vipbot_test_outsider_" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(f"CREATE ROLE {outsider} LOGIN INHERIT")
        cur.execute(f"GRANT {outsider} TO CURRENT_USER")
    psycopg = pytest.importorskip("psycopg")
    try:
        assert (
            verify.has_function_privilege(conn, outsider, CONFIG_FN, "EXECUTE") is False
        )
        assert (
            verify.has_function_privilege(conn, outsider, ACCESS_FN, "EXECUTE") is False
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {outsider}")
                try:
                    cur.execute("SELECT * FROM public.vipbot_connection_config('x')")
                finally:
                    cur.execute("RESET ROLE")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP OWNED BY {outsider}")
            cur.execute(f"DROP ROLE {outsider}")


def test_reapplying_the_window_removes_old_execute_grants(conn):
    outsider = "vipbot_test_stale_acl_" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(f"CREATE ROLE {outsider} NOLOGIN")
        cur.execute(f"GRANT {DEFINER} TO CURRENT_USER")
        cur.execute(f"GRANT EXECUTE ON FUNCTION {CONFIG_FN} TO {outsider}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION {ACCESS_FN} TO {outsider}")
    try:
        assert verify.has_function_privilege(conn, outsider, CONFIG_FN, "EXECUTE")
        assert verify.has_function_privilege(conn, outsider, ACCESS_FN, "EXECUTE")

        verify.apply_files(conn, ("vipbot_access_schema.sql",))

        assert not verify.has_function_privilege(conn, outsider, CONFIG_FN, "EXECUTE")
        assert not verify.has_function_privilege(conn, outsider, ACCESS_FN, "EXECUTE")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"REVOKE {DEFINER} FROM CURRENT_USER")
            cur.execute(f"DROP OWNED BY {outsider}")
            cur.execute(f"DROP ROLE {outsider}")


# --- 4. 다른 봇의 토큰 --------------------------------------------------------


def test_another_bots_tokens_never_come_back_from_the_vipbot_window(conn, connections):
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM public.vipbot_connection_config(%s)", (connections,))
        rows = cur.fetchall()
    blob = repr(rows)
    assert "MASTER" not in blob
    assert "VIP" in blob


def test_the_runtime_role_cannot_open_another_bots_window(conn):
    # Archiver 의 창구가 이 DB 에 있다면, VIPBot 계정으로는 열리면 안 된다.
    got = verify.has_function_privilege(
        conn, RUNTIME, "archiver_connection_config(text)", "EXECUTE"
    )
    assert got in (False, None)


def test_the_window_refuses_a_disabled_or_unverified_vipbot_row(conn, connections):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_connection SET state = 'draft', identity_ok = false"
            " WHERE workspace=%s AND bot_key='vipbot'",
            (connections,),
        )
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM public.vipbot_connection_config(%s)", (connections,))
        assert cur.fetchall() == []
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_connection SET identity_ok = true, state = 'disabled'"
            " WHERE workspace=%s AND bot_key='vipbot'",
            (connections,),
        )
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM public.vipbot_connection_config(%s)", (connections,))
        assert cur.fetchall() == []


# --- 5. 다시 적용해도 그대로 --------------------------------------------------


def _privilege_snapshot(connection) -> dict:
    snapshot = {
        ("function", function, role): verify.has_function_privilege(
            connection, role, function, "EXECUTE"
        )
        for function in (CONFIG_FN, ACCESS_FN)
        for role in (GROUP, RUNTIME)
    }
    snapshot.update(
        {
            ("function", function, "public"): _public_can_execute(
                connection, function
            )
            for function in (CONFIG_FN, ACCESS_FN)
        }
    )
    for role in (GROUP, RUNTIME):
        for table in SECRET_TABLES:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                snapshot[(role, table, privilege)] = verify.has_privilege(
                    connection, role, table, privilege
                )
    for table in SECRET_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            snapshot[("public", table, privilege)] = _public_has_table_privilege(
                connection, table, privilege
            )
    return snapshot


def test_reapplying_the_schema_leaves_no_privilege_drift(conn, connections):
    before = _privilege_snapshot(conn)
    verify.apply_files(conn, _ordered_files())
    after = _privilege_snapshot(conn)
    assert before == after
    # 사실 자체도 다시 본다 — 스냅샷이 둘 다 「열려 있음」 으로 같아도 같다.
    assert after[("function", CONFIG_FN, RUNTIME)] is True
    assert after[("function", CONFIG_FN, "public")] is False
    assert after[("function", ACCESS_FN, RUNTIME)] is True
    assert after[("function", ACCESS_FN, "public")] is False
    assert after[(RUNTIME, "bot_connection_secret", "SELECT")] is False
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_runtime(conn) as cur:
            cur.execute("SELECT * FROM bot_connection_secret LIMIT 1")
