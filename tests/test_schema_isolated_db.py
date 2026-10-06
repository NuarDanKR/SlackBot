"""격리 DB 에서의 스키마 검증 — **DSN 을 주면 진짜로 돈다.**

PostgreSQL 없이 확인할 수 있는 것과 없는 것이 갈린다.

| 무엇 | DB 없이 |
|---|---|
| 선언에 `REVOKE` 가 있나 | 볼 수 있다(`test_archiving_schema.py`) |
| 그 `REVOKE` 가 **실제로 권한을 없애나** | **볼 수 없다** |

PostgreSQL 은 `GRANT` 를 쌓기만 한다. 선언 목록에서 표를 빼도 이미 준 권한은
그대로 남는다. 그래서 「선언에 없다」 와 「권한이 없다」 는 다른 사실이고, 둘을
구분하려면 진짜 DB 가 있어야 한다.

DSN 이 없으면 **건너뛰되 사유를 남긴다.** 조용히 통과하면 「DB 검증을 했다」 로
읽히고, 그게 이 파일에서 가장 나쁜 실패다.

    TYBOT_SCHEMA_TEST_DSN=postgresql://…/tybot_schema_test python -m pytest \\
        tests/test_schema_isolated_db.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_schema_isolated as verify

DSN_ENV = "TYBOT_SCHEMA_TEST_DSN"
_dsn = os.environ.get(DSN_ENV, "").strip()

needs_db = pytest.mark.skipif(
    not _dsn,
    reason=(
        f"{DSN_ENV} 이 없어 건너뜁니다. 선언은 test_archiving_schema.py 가 보지만, "
        "REVOKE 가 실제로 권한을 없애는지는 진짜 DB 에서만 확인됩니다"
    ),
)


# --- DB 없이도 도는 것 — 안전장치 자체 ---------------------------------------

def test_the_verifier_refuses_an_operational_dsn(monkeypatch):
    """이 스크립트는 역할과 권한을 바꾼다. 잘못된 DB 에 돌면 되돌리기가 사람 손이다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/tyslackai")

    assert "DATABASE_URL" in verify.operational_db_refusal(
        "postgresql://u:p@localhost/tybot_schema_test"
    )


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://u:p@db/tyslackai", "postgresql://u:p@db/tybot", "host=db dbname=prod"],
)
def test_the_verifier_refuses_a_database_that_is_not_marked_isolated(monkeypatch, dsn):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert verify.operational_db_refusal(dsn) != ""


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@db/tybot_archive_bench",
        "postgresql://u:p@db/archive_lab",
        "postgresql://u:p@db/tybot_bench_index",
    ],
    ids=["archive-bench", "archive-lab", "bench-index"],
)
def test_databases_that_hold_measurement_data_are_refused(monkeypatch, dsn):
    """**처음에는 이것들이 통과했다.**

    `bench` 와 `lab` 을 「시험용 이름」 으로 보고 안전 표시에 넣었는데,
    `tybot_archive_bench` 와 `archive_lab` 에는 저장 구조 실측 자료가 들어 있다.
    이 스크립트는 `DROP SCHEMA public CASCADE` 를 돌리므로 그대로 두면 자료가
    사라진다(2026-09-25 지적).

    「시험용처럼 보이는 이름」 과 「버려도 되는 DB」 는 다르다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    refusal = verify.operational_db_refusal(dsn)

    assert refusal != ""
    assert "다른 작업이 쓰는" in refusal


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@db/tybot_schema_test",
        "host=db dbname=schema_check_db",
        "postgresql://u:p@db/scratch_db",
    ],
)
def test_the_verifier_accepts_a_dedicated_database(monkeypatch, dsn, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TYBOT_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr(verify, "ROOT", tmp_path)

    assert verify.operational_db_refusal(dsn) == ""


def test_a_dsn_named_in_the_config_file_is_refused(monkeypatch, tmp_path):
    """`.env` 에 있는데 export 는 안 된 경우가 있다. 그때 환경변수 자물쇠는 **헛돈다.**

    2026-09-25 개발 PC 가 실제로 그 상태였다 — `.env` 의 DATABASE_URL 이 운영
    DB 를 가리키는데 환경에는 안 올라와 있었다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env = tmp_path / "tybot.env"
    env.write_text(
        "DATABASE_URL=postgresql://user:pw@localhost:55432/some_schema_test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TYBOT_ENV_FILE", str(env))

    refusal = verify.operational_db_refusal(
        "postgresql://u:p@localhost:55432/some_schema_test"
    )

    assert "설정 파일이 가리키는" in refusal


def test_reading_the_config_file_never_applies_it(monkeypatch, tmp_path):
    """이름만 본다. `os.environ` 에 넣으면 다른 코드가 그 값으로 붙는다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env = tmp_path / "tybot.env"
    env.write_text(
        "DATABASE_URL=postgresql://user:secret@localhost/tyslackai\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TYBOT_ENV_FILE", str(env))

    names = verify.configured_databases()

    assert "tyslackai" in names
    assert "DATABASE_URL" not in os.environ
    assert not any("secret" in name for name in names), "비밀번호를 들고 오면 안 된다"


def test_a_missing_config_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("TYBOT_ENV_FILE", str(tmp_path / "gone.env"))
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    # 서버의 실제 /etc/tybot/tybot.env 유무가 단위시험 결과를 바꾸면 배포에서만
    # 실패한다. 시스템 후보 검사는 별도 통합 경로의 책임이고 여기서는 입력을 격리한다.
    monkeypatch.setattr(verify, "SYSTEM_ENV_FILES", ())

    assert verify.configured_databases() == set()


def test_the_forbidden_list_covers_what_the_draft_granted():
    """초안(`4ecf634`)이 준 뒤 선언에서 뺀 것이 전부 목록에 있어야 한다.

    빠지면 그 권한은 재적용 뒤에도 조용히 남는다.
    """
    forbidden = {(role, table, priv) for role, table, priv in verify.FORBIDDEN}

    assert ("tybot_archiver", "archive_config_audit", "INSERT") in forbidden
    assert ("tyslackai", "archive_message_revision", "UPDATE") in forbidden
    assert ("tyslackai", "bot_conversation_audit", "UPDATE") in forbidden
    assert ("tybot_archiver", "workspace_service_secret", "SELECT") in forbidden


def test_archiver_runtime_function_execute_is_required():
    assert (
        "tybot_archiver",
        "archiver_runtime_config(text)",
        "EXECUTE",
    ) in verify.REQUIRED_FUNCTIONS
    assert (
        "tybot_archiver",
        "archiver_save_membership(text,text,text,text,text,boolean,text,boolean,text)",
        "EXECUTE",
    ) in verify.REQUIRED_FUNCTIONS
    assert (
        "tybot_archiver",
        "archiver_mark_channel_event(text,text)",
        "EXECUTE",
    ) in verify.REQUIRED_FUNCTIONS


def test_the_draft_shape_actually_grants_what_we_then_check():
    """흉내가 비어 있으면 3번 시험이 아무것도 안 본다.

    `draft_shape` 가 주는 권한과 `FORBIDDEN` 이 겹쳐야 「줬다가 회수했다」 를
    잴 수 있다.
    """
    source = Path(verify.__file__).read_text(encoding="utf-8")
    draft = source[source.index("def draft_shape"):source.index("def check_privileges")]

    for _role, table, _priv in verify.FORBIDDEN[:1]:
        assert table in draft
    assert "archive_config_audit" in draft
    assert "bot_conversation_audit" in draft


def test_every_target_schema_file_exists():
    for name in verify.TARGET_FILES:
        assert (verify.SQL_DIR / name).is_file(), name


def test_the_trigram_fallback_installs_its_extension_before_creating_the_index():
    sql = (verify.SQL_DIR / "index_schema.sql").read_text(encoding="utf-8")
    fallback_start = sql.index("ELSE", sql.index("extname = 'pg_bigm'"))
    fallback_end = sql.index("END IF;", fallback_start)
    fallback = sql[fallback_start:fallback_end]

    extension = fallback.index("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    index = fallback.index("CREATE INDEX IF NOT EXISTS raw_line_trgm_fallback")
    assert extension < index


def test_the_schema_files_declare_what_the_verifier_calls_ours():
    """검증이 **자기가 만든 표**를 남의 것으로 보면 안 된다.

    2026-09-30 실제로 그랬다. `archiver_supervisor_schema.sql` 과
    `slack_license_schema.sql` 이 표를 더했는데 손으로 관리하던 목록은 그대로였고,
    첫 실행은 되는데 **두 번째부터** 「격리 DB 가 아니다」 로 거절했다. 사유가
    엉뚱해서 원인을 찾는 데 시간이 든다.

    그래서 목록을 SQL 에서 뽑는다. 이 시험은 그 연결이 살아 있는지만 본다.
    """
    declared = verify.declared_tables()

    assert declared, "선언된 표를 하나도 못 읽었다 — 정규식이 안 맞는다"
    for name in ("archiver_workspace_runtime", "archive_channel_cursor",
                 "archive_backfill_job", "slack_license", "bot_connection"):
        assert name in declared, name


def test_no_declared_table_reads_as_a_stranger():
    """하나라도 빠지면 그 표가 있는 DB 에서 검증이 통째로 안 돈다."""
    known = verify.OUR_TABLE_NAMES | verify.declared_tables()

    strangers = [
        name for name in verify.declared_tables()
        if name not in known
        and not any(name.startswith(prefix) for prefix in verify.OUR_TABLE_PREFIXES)
    ]

    assert strangers == []


class _FakeCursor:
    def __init__(self, names):
        self.names = names

    def execute(self, sql, params=None):
        assert "pg_tables" in sql

    def fetchall(self):
        return [(name,) for name in self.names]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, names):
        self.names = names

    def cursor(self):
        return _FakeCursor(self.names)


def test_foreign_tables_accepts_what_the_schema_files_declare():
    """판정 함수 자체를 본다. 목록만 맞고 함수가 안 쓰면 아무것도 안 바뀐다."""
    ours = ["archiver_workspace_runtime", "slack_license", "slack_license_manual",
            "archive_channel_cursor", "bot_connection"]

    assert verify.foreign_tables(_FakeConn(ours)) == []


def test_foreign_tables_still_names_a_stranger():
    """넓히다가 판정을 없애면 남의 DB 를 지우는 것을 막을 수 없다."""
    mixed = ["archiver_workspace_runtime", "payroll", "customer_orders"]

    assert verify.foreign_tables(_FakeConn(mixed)) == ["payroll", "customer_orders"]


def test_a_real_stranger_is_still_refused():
    """넓히다가 판정 자체를 없애면, 남의 DB 를 지우는 것을 막을 수 없다."""
    known = verify.OUR_TABLE_NAMES | verify.declared_tables()

    for name in ("payroll", "customer_orders", "django_migrations"):
        assert name not in known, name
        assert not any(
            name.startswith(prefix) for prefix in verify.OUR_TABLE_PREFIXES
        ), name


# --- DSN 이 있을 때만 -------------------------------------------------------

def _prepared(conn):
    """지우기 **전에** preflight 를 통과했는지 보고 나서 비운다.

    시험이 이 순서를 지켜야 스크립트의 순서도 지켜진다 — 확인을 뒤에 두면
    「지우고 나서 못 한다고 말하는」 모양이 되고, 그건 개발 PC 에서 실제로 났다.
    """
    blockers = verify.preflight(conn)
    assert blockers == [], f"격리 DB 준비가 안 됐습니다: {blockers}"
    verify.reset(conn)


@pytest.fixture(scope="module")
def conn():
    connection = verify._connect(_dsn)
    try:
        yield connection
    finally:
        connection.close()


@needs_db
def test_clean_install_on_an_empty_database(conn):
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)

    assert verify.check_privileges(conn) == []


@needs_db
def test_applying_the_same_schema_twice_is_safe(conn):
    """재적용이 안전하지 않으면 배포가 한 번짜리가 된다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    verify.apply_files(conn, verify.TARGET_FILES)

    assert verify.check_privileges(conn) == []


@needs_db
def test_reapplying_over_the_draft_revokes_what_it_granted(conn):
    """**이 시험이 이 파일의 이유다.**

    선언에서 표를 빼는 것만으로는 이미 준 권한이 사라지지 않는다. 명시적
    `REVOKE` 가 실제로 도는지는 진짜 DB 에서만 보인다.
    """
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    verify.draft_shape(conn)
    assert verify.has_privilege(
        conn, "tybot_archiver", "archive_config_audit", "INSERT"
    ) is True, "흉내 낸 초안 권한이 안 붙었다 — 이 시험이 무의미하다"

    verify.apply_files(conn, verify.TARGET_FILES)

    assert verify.check_privileges(conn) == []
    assert verify.has_privilege(
        conn, "tybot_archiver", "archive_config_audit", "INSERT"
    ) is False


@needs_db
def test_the_message_revision_trigger_refuses_a_gap(conn):
    """번호를 건너뛰면 감사에 구멍이 생기는데 `CHECK` 로는 못 잡는다."""
    import psycopg

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO archive_message_revision"
            " (workspace, channel_id, message_ts, revision_no, kind)"
            " VALUES ('tyit','C1','1.0001',1,'create')"
        )
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "INSERT INTO archive_message_revision"
                " (workspace, channel_id, message_ts, revision_no, kind)"
                " VALUES ('tyit','C1','1.0001',3,'change')"
            )


@needs_db
def test_legacy_workspace_secrets_migrate_to_the_master_service(conn):
    """봇이 아직 옛 표를 읽는다. 옮기되 **지우지 않는다.**"""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace (key, label, archive_path, created_by)"
            " VALUES ('tyit','전산팀','/var/lib/tybot/archive/tyit','test')"
        )
        for kind in ("bot", "app"):
            cur.execute(
                "INSERT INTO workspace_secret (workspace, kind, ciphertext, mask, updated_by)"
                " VALUES ('tyit', %s, '\\x00', 'masked', 'test')",
                (kind,),
            )

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM workspace_service WHERE workspace='tyit' AND service='master'"
        )
        assert cur.fetchone()[0] == "disabled", "이관된 것도 신원 검사를 거쳐야 켜진다"
        cur.execute(
            "SELECT count(*) FROM workspace_service_secret"
            " WHERE workspace='tyit' AND service='master'"
        )
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM workspace_secret WHERE workspace='tyit'")
        assert cur.fetchone()[0] == 2, "옛 표를 지우면 다음 기동에서 전부 안 뜬다"
        cur.execute("SELECT archive_path FROM workspace WHERE key='tyit'")
        assert cur.fetchone()[0] == "/var/lib/tybot/archive/workspaces/tyit"


@needs_db
def test_a_service_cannot_be_enabled_without_identity(conn):
    import psycopg

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace (key, label, archive_path, created_by)"
            " VALUES ('tyit','전산팀','/var/lib/tybot/archive/workspaces/tyit','test')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO workspace_service (workspace, service, state)"
                " VALUES ('tyit','archiver','enabled')"
            )


@needs_db
def test_two_services_cannot_share_a_bot_user(conn):
    """같은 앱을 두 번 등록하면 Socket Mode 를 두 곳에서 열게 된다."""
    import psycopg

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace (key, label, archive_path, created_by)"
            " VALUES ('tyit','전산팀','/var/lib/tybot/archive/workspaces/tyit','test')"
        )
        cur.execute(
            "INSERT INTO workspace_service (workspace, service, team_id, bot_user_id)"
            " VALUES ('tyit','master','T1','U1')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO workspace_service (workspace, service, team_id, bot_user_id)"
                " VALUES ('tyit','archiver','T1','U1')"
            )


# --- 봇 연결 이관 (2026-09-28 콘솔 개편 §5·§11.1) -----------------------------

def _workspace(cur, key: str = "tyit") -> None:
    cur.execute(
        "INSERT INTO workspace (key, label, archive_path, created_by)"
        " VALUES (%s,'전산팀','/var/lib/tybot/archive/workspaces/tyit','test')"
        " ON CONFLICT (key) DO NOTHING",
        (key,),
    )


def _legacy_service(cur, service: str, *, bot_user: str, cipher: str, mask: str) -> None:
    """옛 표에 있는 연결 하나 — 신원까지 확인돼 `enabled` 인 상태."""
    cur.execute(
        "INSERT INTO workspace_service"
        " (workspace, service, state, team_id, bot_user_id, identity_ok)"
        " VALUES ('tyit', %s, 'enabled', 'T1', %s, true)",
        (service, bot_user),
    )
    for kind in ("bot", "app"):
        cur.execute(
            "INSERT INTO workspace_service_secret"
            " (workspace, service, kind, ciphertext, mask, updated_by)"
            " VALUES ('tyit', %s, %s, %s, %s, 'test')",
            (service, kind, cipher.encode(), f"{mask}-{kind}"),
        )


@needs_db
def test_the_three_legacy_services_move_to_their_bot_keys(conn):
    """`hermes_direct` 가 `hermes` 가 되는 것이 이 이관의 요점이다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        _legacy_service(cur, "master", bot_user="U1", cipher="c1", mask="m1")
        _legacy_service(cur, "archiver", bot_user="U2", cipher="c2", mask="m2")
        _legacy_service(cur, "hermes_direct", bot_user="U3", cipher="c3", mask="m3")

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT bot_key, connector_type, state, bot_user_id FROM bot_connection"
            " WHERE workspace='tyit' ORDER BY bot_key"
        )
        rows = cur.fetchall()
        assert [r[0] for r in rows] == ["archiver", "hermes", "master"]
        assert {r[1] for r in rows} == {"slack_socket"}
        assert {r[2] for r in rows} == {"enabled"}, "상태가 바뀌면 이관이 아니다"
        cur.execute("SELECT count(*) FROM bot_connection WHERE bot_key='hermes_direct'")
        assert cur.fetchone()[0] == 0, "Hermes 가 콘솔에서 다시 둘이 된다"
        cur.execute("SELECT count(*) FROM workspace_service WHERE workspace='tyit'")
        assert cur.fetchone()[0] == 3, "옛 표를 지우면 Archiver 가 다음 기동에서 안 뜬다"


@needs_db
def test_the_migration_carries_the_ciphertext_and_mask_unchanged(conn):
    """토큰을 다시 암호화하면 키가 바뀐 날 전부 못 읽는다. 재발급도 하지 않는다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        _legacy_service(cur, "hermes_direct", bot_user="U3", cipher="secret3", mask="m3")

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.kind, s.ciphertext, s.mask FROM bot_connection_secret s"
            "  JOIN bot_connection c ON c.id = s.connection_id"
            " WHERE c.workspace='tyit' AND c.bot_key='hermes' ORDER BY s.kind"
        )
        rows = cur.fetchall()
        assert [r[0] for r in rows] == ["app", "bot"]
        assert {bytes(r[1]) for r in rows} == {b"secret3"}
        assert {r[2] for r in rows} == {"m3-app", "m3-bot"}


@needs_db
def test_a_newer_token_is_not_overwritten_by_the_legacy_copy(conn):
    """재적용이 콘솔에서 넣은 새 토큰을 옛 값으로 되돌리면 **조용한 롤백**이다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        _legacy_service(cur, "archiver", bot_user="U2", cipher="old", mask="old")

    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_connection_secret SET ciphertext = %s, mask = 'new'"
            "  FROM bot_connection c"
            " WHERE c.id = bot_connection_secret.connection_id AND c.bot_key='archiver'",
            (b"new",),
        )

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT s.mask FROM bot_connection_secret s"
            "  JOIN bot_connection c ON c.id = s.connection_id WHERE c.bot_key='archiver'"
        )
        assert [r[0] for r in cur.fetchall()] == ["new"]


@needs_db
def test_the_migration_is_recorded_once(conn):
    """감사가 재적용마다 쌓이면 **언제 옮겼는지**를 세는 것이 무의미해진다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        _legacy_service(cur, "hermes_direct", bot_user="U3", cipher="c3", mask="m3")

    verify.apply_files(conn, verify.TARGET_FILES)
    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT old_value, new_value FROM archive_config_audit"
            " WHERE subject='bot_connection' AND workspace='tyit'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1, rows
        assert rows[0][0] == "workspace_service.hermes_direct"
        assert rows[0][1] == "bot_connection.hermes/slack_socket"


@needs_db
def test_two_connections_cannot_share_a_bot_user(conn):
    """같은 앱을 두 번 등록하면 Socket Mode 를 두 곳에서 열게 된다."""
    import psycopg

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        cur.execute(
            "INSERT INTO bot_connection (workspace, bot_key, team_id, bot_user_id)"
            " VALUES ('tyit','master','T1','U1')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO bot_connection (workspace, bot_key, team_id, bot_user_id)"
                " VALUES ('tyit','archiver','T1','U1')"
            )


@needs_db
def test_a_connection_cannot_be_enabled_without_identity(conn):
    """검사 전에 켤 수 있으면 토큰을 잘못 붙인 채로 수집이 시작된다."""
    import psycopg

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO bot_connection (workspace, bot_key, state)"
                " VALUES ('tyit','archiver','enabled')"
            )


@needs_db
def test_routes_are_backfilled_by_what_actually_routes_today(conn):
    """오늘 안 불리는 배정을 `active` 로 적으면 이관이 라우팅을 **바꾼다**."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        for key, state, health in (
            ("hermes", "enabled", "ok"),
            ("clio", "disabled", "ok"),
            ("atlas", "enabled", "error"),
        ):
            cur.execute(
                "INSERT INTO specialist_bot"
                " (key, name, domain, adapter, state, health, created_by, updated_by)"
                " VALUES (%s, %s, '업무', 'prompt', %s, %s, 'test', 'test')",
                (key, key, state, health),
            )
            cur.execute(
                "INSERT INTO specialist_workspace (specialist, workspace)"
                " VALUES (%s, 'tyit')",
                (key,),
            )

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT specialist, route_mode FROM specialist_route"
            " WHERE workspace='tyit' ORDER BY specialist"
        )
        assert cur.fetchall() == [
            ("atlas", "disabled"), ("clio", "disabled"), ("hermes", "active"),
        ]


@needs_db
def test_the_route_survives_a_reassignment(conn):
    """배정 저장은 전량 삭제·재삽입이다. 같은 행에 상태를 두면 매번 초기화된다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        cur.execute(
            "INSERT INTO specialist_bot"
            " (key, name, domain, adapter, state, health, created_by, updated_by)"
            " VALUES ('hermes','Hermes','업무','prompt','enabled','ok','test','test')"
        )
        cur.execute(
            "INSERT INTO specialist_workspace (specialist, workspace)"
            " VALUES ('hermes','tyit')"
        )
        cur.execute(
            "INSERT INTO specialist_route (specialist, workspace, route_mode)"
            " VALUES ('hermes','tyit','shadow')"
        )
        # `specialist_store` 가 배정을 저장하는 방식 그대로.
        cur.execute("DELETE FROM specialist_workspace WHERE specialist='hermes'")
        cur.execute(
            "INSERT INTO specialist_workspace (specialist, workspace)"
            " VALUES ('hermes','tyit')"
        )
        cur.execute(
            "SELECT route_mode FROM specialist_route WHERE specialist='hermes'"
        )
        assert cur.fetchone()[0] == "shadow"


@needs_db
def test_an_existing_specialist_bot_enters_the_catalog(conn):
    """승인받아 쓰던 전문 봇이 카탈로그에 없으면, 그 봇의 라우트는 주인이 없다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO specialist_bot"
            " (key, name, domain, adapter, state, health, created_by, updated_by)"
            " VALUES ('atlas','아틀라스','건설','prompt','disabled','ok','test','test')"
        )

    verify.apply_files(conn, verify.TARGET_FILES)
    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, category, slack_connectable, internally_invokable,"
            "       state"
            "  FROM bot_catalog WHERE key = 'atlas'"
        )
        row = cur.fetchone()
        assert row is not None, "기존 전문 봇이 카탈로그에 없다"
        assert row[0] == "아틀라스"
        assert row[1] == "specialist"
        assert (row[2], row[3]) == (False, True), "전문 봇은 Slack 에 직접 안 붙는다"
        # 런타임 상태를 복제하면 두 값이 갈리는 날이 온다(§4.2).
        assert row[4] == "active"
        cur.execute("SELECT count(*) FROM bot_catalog")
        assert cur.fetchone()[0] == 5, "재적용이 행을 늘리면 멱등이 아니다"


@needs_db
def test_the_catalog_backfill_does_not_overwrite_edits(conn):
    """재적용이 사람이 고친 표시 이름을 되돌리면 **조용한 롤백**이다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO specialist_bot"
            " (key, name, domain, adapter, state, health, created_by, updated_by)"
            " VALUES ('atlas','아틀라스','건설','prompt','enabled','ok','test','test')"
        )

    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        cur.execute("UPDATE bot_catalog SET display_name = '건설봇' WHERE key='atlas'")

    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        cur.execute("SELECT display_name FROM bot_catalog WHERE key='atlas'")
        assert cur.fetchone()[0] == "건설봇"


@needs_db
def test_the_console_role_cannot_delete_connection_rows(conn):
    """그만 쓰는 것은 retired·disabled 다. 지우면 있었다는 사실까지 사라진다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)

    with conn.cursor() as cur:
        for table in (
            "bot_catalog", "bot_connection", "bot_connection_secret", "specialist_route",
        ):
            cur.execute(
                "SELECT has_table_privilege('tyslackai', %s, 'DELETE'),"
                "       has_table_privilege('tyslackai', %s, 'SELECT'),"
                "       has_table_privilege('tyslackai', %s, 'UPDATE')",
                (table, table, table),
            )
            can_delete, can_read, can_update = cur.fetchone()
            assert not can_delete, f"{table} 에 DELETE 가 남아 있다"
            assert can_read and can_update, f"{table} 을 콘솔이 못 쓴다"


@needs_db
def test_archiver_can_update_progress_but_not_control_coordinates(conn):
    """Archiver는 진행 상황만 쓰고 desired·좌표·요청자는 고치지 못한다."""
    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)

    checks = (
        ("archiver_workspace_runtime", "observed_state", True),
        ("archiver_workspace_runtime", "desired_mode", False),
        ("archive_channel_cursor", "last_history_ts", True),
        ("archive_channel_cursor", "workspace", False),
        ("archive_channel_cursor", "channel_id", False),
        ("archive_backfill_job", "state", True),
        ("archive_backfill_job", "requested_by", False),
        ("archive_backfill_job", "reason", False),
    )
    with conn.cursor() as cur:
        for table in (
            "archiver_workspace_runtime", "archive_channel_cursor", "archive_backfill_job",
        ):
            cur.execute(
                "SELECT has_table_privilege('tybot_archiver', %s, 'UPDATE')",
                (table,),
            )
            assert cur.fetchone()[0] is False, f"{table} 에 표 단위 UPDATE 가 남아 있다"
        for table, column, expected in checks:
            cur.execute(
                "SELECT has_column_privilege('tybot_archiver', %s, %s, 'UPDATE')",
                (table, column),
            )
            assert cur.fetchone()[0] is expected, f"{table}.{column} UPDATE 권한이 틀렸다"


@needs_db
def test_removing_an_assignment_disables_its_route(conn):
    """재배정 때 과거 `active` 가 되살아나면, 켠 적 없는 봇이 답하기 시작한다."""
    from tybot.console.specialist_store import disable_routes_outside

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        cur.execute(
            "INSERT INTO workspace (key, label, archive_path, created_by)"
            " VALUES ('pf','PF','/var/lib/tybot/archive/workspaces/pf','test')"
        )
        cur.execute(
            "INSERT INTO specialist_bot"
            " (key, name, domain, adapter, state, health, created_by, updated_by)"
            " VALUES ('hermes','Hermes','업무','prompt','enabled','ok','test','test')"
        )
        for ws, mode in (("tyit", "active"), ("pf", "shadow")):
            cur.execute(
                "INSERT INTO specialist_route (specialist, workspace, route_mode)"
                " VALUES ('hermes', %s, %s)",
                (ws, mode),
            )

        # tyit 배정만 남기고 pf 를 뗀다.
        disable_routes_outside(cur, "hermes", ["tyit"], "dan")

        cur.execute(
            "SELECT workspace, route_mode, updated_by FROM specialist_route"
            " WHERE specialist='hermes' ORDER BY workspace"
        )
        assert cur.fetchall() == [
            ("pf", "disabled", "dan"), ("tyit", "active", ""),
        ]


@needs_db
def test_disabling_routes_leaves_already_disabled_rows_alone(conn):
    """이미 꺼진 행을 다시 쓰면 `updated_by` 가 바뀌어 누가 껐는지가 흐려진다."""
    from tybot.console.specialist_store import disable_routes_outside

    _prepared(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    with conn.cursor() as cur:
        _workspace(cur)
        cur.execute(
            "INSERT INTO specialist_bot"
            " (key, name, domain, adapter, state, health, created_by, updated_by)"
            " VALUES ('hermes','Hermes','업무','prompt','enabled','ok','test','test')"
        )
        cur.execute(
            "INSERT INTO specialist_route (specialist, workspace, route_mode, updated_by)"
            " VALUES ('hermes','tyit','disabled','first')"
        )

        changed = disable_routes_outside(cur, "hermes", [], "second")

        assert changed == 0
        cur.execute("SELECT updated_by FROM specialist_route WHERE workspace='tyit'")
        assert cur.fetchone()[0] == "first"


# --- 지우기 전에 확인한다 ----------------------------------------------------

class _Cur:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def test_preflight_reports_missing_roles_instead_of_creating_them():
    """개발 PC 계정에 `CREATEROLE` 이 없다.

    전에는 여기서 `CREATE ROLE` 을 시도했는데, 그 실패가 **`reset()` 뒤에** 났다 —
    스키마를 지우고 나서 권한 오류로 죽는다. 순서가 그 자체로 사고였다.
    """
    cur = _Cur([None, None, []])          # 역할 둘 다 없음, 남의 표 없음

    problems = verify.preflight(_Conn(cur))

    assert len(problems) == 1
    assert "CREATE ROLE tyslackai NOLOGIN" in problems[0]
    assert "CREATE ROLE tybot_archiver NOLOGIN" in problems[0]
    assert not [sql for sql in cur.sql if sql.startswith("CREATE ROLE")]


def test_preflight_refuses_a_database_that_holds_someone_elses_tables():
    """이름이 맞아도 남이 쓰는 DB 일 수 있고, `DROP SCHEMA` 는 되돌릴 수 없다."""
    cur = _Cur([(1,), (1,), [("orders",), ("archive_channel_mode",), ("invoices",)]])

    problems = verify.preflight(_Conn(cur))

    assert len(problems) == 1
    assert "우리 것이 아닌 표가 2개" in problems[0]
    assert "orders" in problems[0]
    assert "archive_channel_mode" not in problems[0], "우리 표는 세지 않는다"


def test_preflight_passes_on_a_database_that_only_holds_our_tables():
    """앞선 실행이 남긴 우리 표는 정상이다. 그걸 막으면 두 번 못 돌린다."""
    cur = _Cur([
        (1,),
        (1,),
        [
            ("anomaly",),
            ("archive_channel_mode",),
            ("audit_query",),
            ("bot_catalog",),
            ("bot_connection",),
            ("bot_connection_secret",),
            ("channel",),
            ("sync_run",),
            ("workspace_service",),
        ],
    ])

    assert verify.preflight(_Conn(cur)) == []


def test_reset_is_never_called_before_preflight():
    """순서가 뒤집히면 「지우고 나서 못 한다고 말하는」 스크립트가 된다."""
    import ast

    source = Path(verify.__file__).read_text(encoding="utf-8")
    body = source[source.index("def main()"):]
    tree = ast.parse(source)
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    names = [
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "preflight" in names and "reset" in names
    assert names.index("preflight") < names.index("reset")
    assert "ensure_roles" not in body, "역할을 만들지 않는다 — 확인만 한다"


# --- 검토 주기 (666d966) ------------------------------------------------------
#
# DSN 이 없으면 **실제 거부는 확인할 수 없다.** 그래도 「무엇을 확인할 것인가」 는
# 여기 적혀 있어야 한다 — 목록이 코드에만 있으면 조용히 줄어든다.
def test_the_schedule_cases_cover_every_boundary_the_check_declares():
    """CHECK 가 가르는 자리를 **빠짐없이** 시험 목록에 적었나.

    선언은 `(daily AND weekday IS NULL) OR (weekly AND weekday BETWEEN 0 AND 6)` 다.
    경계는 넷 — daily 에 요일이 있는 경우, weekly 에 요일이 없는 경우, 그리고
    범위의 양쪽 밖(-1, 7).
    """
    accepted = set(verify.SCHEDULE_ACCEPTED)
    rejected = {(kind, weekday) for kind, weekday, _ in verify.SCHEDULE_REJECTED}

    assert ("daily", None) in accepted
    assert {("weekly", 0), ("weekly", 6)} <= accepted, "범위의 양 끝을 안 본다"
    assert ("weekly", None) in rejected
    assert any(kind == "daily" and weekday is not None for kind, weekday in rejected)
    assert {("weekly", -1), ("weekly", 7)} <= rejected, "범위 밖 양쪽을 안 본다"


def test_the_reviewer_schema_is_actually_applied_by_the_verifier():
    """목록에 없으면 **아무리 검사를 적어도 돌지 않는다.**"""
    assert "reviewer_schema.sql" in verify.TARGET_FILES


def test_the_legacy_row_helper_does_not_create_the_new_columns():
    """이전 전 모양을 흉내 내는 자리라, 새 열이 있으면 시험이 무의미해진다."""
    import inspect

    source = inspect.getsource(verify.legacy_reviewer_row)

    assert "schedule_kind" not in source.split("CREATE TABLE", 1)[1].split(")", 1)[0]
    assert "weekday" not in source.split("CREATE TABLE", 1)[1].split(")", 1)[0]


@needs_db
def test_an_existing_row_survives_as_daily_with_no_weekday(conn):
    """이미 돌고 있는 DB 의 검토자가 **매일로 보존**되는가.

    여기서 틀리면 전환 다음 날부터 검토 DM 이 안 간다 — 그 사실은 아무도 묻지
    않으므로 며칠 뒤에야 드러난다.
    """
    verify.reset(conn)
    verify.apply_files(conn, verify.TARGET_FILES)
    verify.legacy_reviewer_row(conn)
    verify.apply_files(conn, ("reviewer_schema.sql",))

    assert verify.check_review_schedule(conn) == []
