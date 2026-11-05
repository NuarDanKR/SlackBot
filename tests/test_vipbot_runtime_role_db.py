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
CONFIG_FN = "vipbot_connection_config(text)"
SECRET_TABLES = ("bot_connection", "bot_connection_secret")

#: 스키마를 적용할 순서. `deploy/apply-schema.sh` 의 목록에서 뽑는다 — 여기에
#: 손으로 베껴 두면 파일이 하나 늘어날 때마다 이 시험만 옛 순서로 돈다.
_UPTO = "bot_connection_schema.sql"


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
    try:
        with connection.cursor() as cur:
            # 그룹은 스키마가 **있을 때만** 권한을 준다. 그래서 스키마보다 먼저
            # 만든다 — 순서가 뒤집히면 GRANT 가 통째로 건너뛰어지고, 그 결과는
            # 「권한 없음」 이라 시험이 통과한 것처럼 보인다.
            for role, attrs in ((GROUP, "NOLOGIN"), (RUNTIME, "LOGIN INHERIT")):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE ROLE {role} {attrs}")
                    created.append(role)
            cur.execute(f"GRANT {GROUP} TO {RUNTIME}")
            # `SET ROLE` 로 실제 계정이 되어 보려면 멤버여야 한다. 비밀번호 없이
            # **진짜 거부**를 받아 보는 유일한 길이다.
            cur.execute("SELECT current_user")
            cur.execute(f"GRANT {RUNTIME} TO CURRENT_USER")
        verify.apply_files(connection, _ordered_files())
        yield connection
    finally:
        try:
            with connection.cursor() as cur:
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
            "INSERT INTO workspace (key, name) VALUES (%s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            (workspace, "시험 워크스페이스"),
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
                           (connection_id, kind, ciphertext)
                       VALUES (%s, %s, %s)""",
                    (connection_id, kind, f"cipher-{suffix}-{kind}".encode()),
                )
    return workspace


# --- 1·2. 함수 실행만 된다 ---------------------------------------------------


def test_the_runtime_role_can_execute_the_one_function_it_was_made_for(
    conn, connections
):
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM vipbot_connection_config(%s)", (connections,))
        rows = cur.fetchall()
    assert len(rows) == 1


@pytest.mark.parametrize("table", SECRET_TABLES)
def test_the_runtime_role_cannot_read_the_token_tables_directly(conn, table):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_runtime(conn) as cur:
            cur.execute(f"SELECT * FROM {table} LIMIT 1")


@pytest.mark.parametrize("table", SECRET_TABLES)
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE"])
def test_no_table_privilege_is_granted_on_the_token_tables(conn, table, privilege):
    assert verify.has_privilege(conn, RUNTIME, table, privilege) is False
    assert verify.has_privilege(conn, GROUP, table, privilege) is False


def test_the_execute_grant_reaches_the_runtime_through_the_group(conn):
    assert verify.has_function_privilege(conn, GROUP, CONFIG_FN, "EXECUTE") is True
    assert verify.has_function_privilege(conn, RUNTIME, CONFIG_FN, "EXECUTE") is True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolinherit FROM pg_roles WHERE rolname=%s", (GROUP,)
        )
        assert cur.fetchone()[0] is False


# --- 3. PUBLIC ---------------------------------------------------------------


def test_public_cannot_execute_the_vipbot_function(conn):
    assert verify.has_function_privilege(conn, "public", CONFIG_FN, "EXECUTE") is False


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
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {outsider}")
                try:
                    cur.execute("SELECT * FROM vipbot_connection_config('x')")
                finally:
                    cur.execute("RESET ROLE")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP OWNED BY {outsider}")
            cur.execute(f"DROP ROLE {outsider}")


# --- 4. 다른 봇의 토큰 --------------------------------------------------------


def test_another_bots_tokens_never_come_back_from_the_vipbot_window(conn, connections):
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM vipbot_connection_config(%s)", (connections,))
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
            "UPDATE bot_connection SET identity_ok = false"
            " WHERE workspace=%s AND bot_key='vipbot'",
            (connections,),
        )
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM vipbot_connection_config(%s)", (connections,))
        assert cur.fetchall() == []
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_connection SET identity_ok = true, state = 'disabled'"
            " WHERE workspace=%s AND bot_key='vipbot'",
            (connections,),
        )
    with as_runtime(conn) as cur:
        cur.execute("SELECT * FROM vipbot_connection_config(%s)", (connections,))
        assert cur.fetchall() == []


# --- 5. 다시 적용해도 그대로 --------------------------------------------------


def _privilege_snapshot(connection) -> dict:
    snapshot = {
        ("function", role): verify.has_function_privilege(
            connection, role, CONFIG_FN, "EXECUTE"
        )
        for role in (GROUP, RUNTIME, "public")
    }
    for role in (GROUP, RUNTIME, "public"):
        for table in SECRET_TABLES:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                snapshot[(role, table, privilege)] = verify.has_privilege(
                    connection, role, table, privilege
                )
    return snapshot


def test_reapplying_the_schema_leaves_no_privilege_drift(conn, connections):
    before = _privilege_snapshot(conn)
    verify.apply_files(conn, _ordered_files())
    after = _privilege_snapshot(conn)
    assert before == after
    # 사실 자체도 다시 본다 — 스냅샷이 둘 다 「열려 있음」 으로 같아도 같다.
    assert after[("function", RUNTIME)] is True
    assert after[("function", "public")] is False
    assert after[(RUNTIME, "bot_connection_secret", "SELECT")] is False
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_runtime(conn) as cur:
            cur.execute("SELECT * FROM bot_connection_secret LIMIT 1")
