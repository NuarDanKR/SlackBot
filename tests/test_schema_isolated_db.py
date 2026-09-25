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
    verify.apply_files(conn, verify.TARGET_FILES)

    assert verify.check_privileges(conn) == []


@needs_db
def test_reapplying_over_the_draft_revokes_what_it_granted(conn):
    """**이 시험이 이 파일의 이유다.**

    선언에서 표를 빼는 것만으로는 이미 준 권한이 사라지지 않는다. 명시적
    `REVOKE` 가 실제로 도는지는 진짜 DB 에서만 보인다.
    """
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
    cur = _Cur([(1,), (1,), [("archive_channel_mode",), ("workspace_service",)]])

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
