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
        "postgresql://u:p@db/tybot_schema_test",
        "postgresql://u:p@db/archive_lab",
        "host=db dbname=schema_check_db",
    ],
)
def test_the_verifier_accepts_a_marked_database(monkeypatch, dsn):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert verify.operational_db_refusal(dsn) == ""


def test_the_forbidden_list_covers_what_the_draft_granted():
    """초안(`4ecf634`)이 준 뒤 선언에서 뺀 것이 전부 목록에 있어야 한다.

    빠지면 그 권한은 재적용 뒤에도 조용히 남는다.
    """
    forbidden = {(role, table, priv) for role, table, priv in verify.FORBIDDEN}

    assert ("tybot_archiver", "archive_config_audit", "INSERT") in forbidden
    assert ("tyslackai", "archive_message_revision", "UPDATE") in forbidden
    assert ("tyslackai", "bot_conversation_audit", "UPDATE") in forbidden
    assert ("tybot_archiver", "workspace_service_secret", "SELECT") in forbidden


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

@pytest.fixture(scope="module")
def conn():
    connection = verify._connect(_dsn)
    try:
        yield connection
    finally:
        connection.close()


@needs_db
def test_clean_install_on_an_empty_database(conn):
    verify.reset(conn)
    verify.ensure_roles(conn)
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

    verify.reset(conn)
    verify.ensure_roles(conn)
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
    verify.reset(conn)
    verify.ensure_roles(conn)
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

    verify.reset(conn)
    verify.ensure_roles(conn)
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

    verify.reset(conn)
    verify.ensure_roles(conn)
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
