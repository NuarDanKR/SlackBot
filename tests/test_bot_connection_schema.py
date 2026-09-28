"""봇 연결 스키마가 **선언한 것을 실제로 막는지.**

설계: `docs/design/workspace-service-console-redesign.md` §4·§5·§11.1 (2026-09-28)

옛 `workspace_service` 는 세 가지를 막고 있었다 — 오류 문구와 상태의 모순, 신원
검사 없는 `enabled`, 같은 워크스페이스의 봇 사용자 중복. 새 표가 그걸 물려받지
않으면 **이관과 동시에 보호가 사라진다.** 옮기는 작업에서 제일 흔한 손실이고,
아무 오류도 안 난다.

SQL 을 **파싱해서** 본다. 진짜 DB 를 요구하면 개발 PC 에서 안 돌고, 안 도는 시험은
지켜 주지 않는다. 대신 「문자열이 들어 있나」 로 보지 않는다 — 주석에 적어 놓기만
해도 통과하기 때문이다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "deploy" / "sql" / "bot_connection_schema.sql"
APPLY = ROOT / "deploy" / "apply-schema.sh"
VERIFIER = ROOT / "scripts" / "verify_schema_isolated.py"


def _sql() -> str:
    """주석을 **지운** SQL. 주석에 적어 둔 것이 선언으로 세어지면 안 된다."""
    return re.sub(r"--[^\n]*", "", SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sql() -> str:
    return _sql()


def _constraint(sql: str, name: str) -> str:
    """`ADD CONSTRAINT <name> CHECK (...)` 의 괄호 안."""
    start = sql.find(f"ADD CONSTRAINT {name}")
    if start < 0:
        return ""
    check = sql.find("CHECK", start)
    if check < 0:
        return ""
    depth, out = 0, []
    for ch in sql[check:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            out.append(ch)
    return " ".join("".join(out).split())


def _column(sql: str, table: str, column: str) -> str:
    """`CREATE TABLE <table>` 안의 한 열 선언."""
    start = sql.find(f"CREATE TABLE IF NOT EXISTS {table} (")
    if start < 0:
        return ""
    depth, out = 0, []
    for ch in sql[start + len(f"CREATE TABLE IF NOT EXISTS {table}"):]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    body = "".join(out)
    for piece in re.split(r",(?![^()]*\))", body):
        cleaned = " ".join(piece.split()).lstrip("( ")
        if cleaned.startswith(f"{column} "):
            return cleaned
    return ""


def _section(sql: str, head: str, nxt: str) -> str:
    """두 표식 사이의 SQL. 이관 구문만 떼어 볼 때 쓴다."""
    start = sql.find(head)
    end = sql.find(nxt, start + 1) if start >= 0 else -1
    return sql[start:end] if start >= 0 and end > start else ""


# --- 배포 경로에 들어 있나 ---------------------------------------------------

def test_the_schema_is_registered_for_deployment():
    """목록에 없으면 **한 번도 적용되지 않는다.**

    실제로 그래서 콘솔이 `column does not exist` 로 죽었다(2026-09-14).
    파일을 만드는 것과 적용되는 것은 다른 일이다.
    """
    assert "bot_connection_schema.sql" in APPLY.read_text(encoding="utf-8")


def test_the_schema_is_verified_in_an_isolated_database():
    """격리 DB 검증 대상이 아니면 권한 선언을 **아무도 확인하지 않는다.**"""
    assert "bot_connection_schema.sql" in VERIFIER.read_text(encoding="utf-8")


def test_it_runs_after_the_tables_it_references():
    """`workspace`·`specialist_bot`·`workspace_service` 를 참조한다. 먼저 서야 한다."""
    order = [
        line.split("#")[0].strip()
        for line in APPLY.read_text(encoding="utf-8").splitlines()
        if line.strip().endswith(".sql") or ".sql" in line
    ]
    order = [name for name in order if name.endswith(".sql")]

    assert order.index("bot_connection_schema.sql") > order.index("console_schema.sql")
    assert order.index("bot_connection_schema.sql") > order.index(
        "workspace_service_schema.sql"
    )
    # 이관 감사가 `archive_config_audit` 에 쓴다. 그 표가 먼저 서야 한다.
    assert order.index("bot_connection_schema.sql") > order.index("archiving_schema.sql")


def test_the_schema_is_one_transaction():
    """중간에 실패하면 절반만 선 표가 남는다. 그 상태가 제일 고치기 어렵다."""
    text = _sql()
    assert text.lstrip().startswith("BEGIN;")
    assert text.rstrip().endswith("COMMIT;")


def test_reapplying_never_overwrites(sql):
    """재적용이 콘솔에서 넣은 값을 옛 값으로 되돌리면 **조용한 롤백**이다."""
    assert "DO UPDATE" not in sql, "이관 구문에 갱신이 있으면 옛 값이 새 값을 덮는다"
    assert sql.count("ON CONFLICT") >= 4


# --- 옛 표가 막던 것을 물려받았나 ---------------------------------------------

def test_an_enabled_connection_must_have_passed_identity(sql):
    """검사 전에 켤 수 있으면 **토큰을 잘못 붙인 채로 수집이 시작된다.**"""
    body = _constraint(sql, "bot_connection_enabled_needs_identity")

    assert body, "제약이 없다"
    assert "identity_ok IS TRUE" in body
    assert "team_id" in body and "bot_user_id" in body


def test_an_error_message_cannot_outlive_the_error_state(sql):
    """「정상인데 빨간 글씨」 를 본 사람은 무엇을 믿어야 할지 모른다."""
    body = _constraint(sql, "bot_connection_error_only_when_error")

    assert body
    assert "'error'" in body and "runtime_error" in body


def test_one_bot_user_per_workspace(sql):
    """같은 봇 사용자를 두 연결로 등록하면 Socket Mode 가 두 곳에서 열린다.

    이벤트를 양쪽이 받아 **중복 답변·비용 2배**가 된다(CLAUDE.md 금지사항).
    """
    found = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS bot_connection_distinct_bot_user"
        r"(.*?);", sql, re.S,
    )

    assert found, "봇 사용자 유니크 인덱스가 없다"
    body = " ".join(found.group(1).split())
    assert "(workspace, bot_user_id)" in body
    # 빈 값끼리는 겹쳐도 된다 — 아직 신원을 모르는 연결이 서로를 막으면 등록이 멈춘다.
    assert "bot_user_id <> ''" in body


def test_states_are_closed_sets(sql):
    """제3의 상태가 생기면 「이 중 하나」 라는 전제가 조용히 깨진다."""
    state = _column(sql, "bot_connection", "state")
    mode = _column(sql, "specialist_route", "route_mode")
    category = _column(sql, "bot_catalog", "category")

    assert set(re.findall(r"'(\w+)'", state)) == {
        "draft", "disabled", "enabled", "error", "retired",
    }
    assert set(re.findall(r"'(\w+)'", mode)) == {"disabled", "shadow", "active"}
    assert set(re.findall(r"'(\w+)'", category)) == {
        "orchestrator", "collector", "specialist",
    }


def test_only_slack_connector_exists_today(sql):
    """`master_internal` 을 가짜 연결로 넣을 자리를 만들지 않는다(§4.3)."""
    connector = _column(sql, "bot_connection", "connector_type")

    assert set(re.findall(r"'(\w+)'", connector)) == {"slack_socket"}


def test_the_catalog_has_no_closed_key_check(sql):
    """네 값만 허용하면 새 전문 봇을 등록할 때 **스키마를 고쳐야 한다**(§4.2)."""
    key = _column(sql, "bot_catalog", "key")

    assert "~" in key, "이름 모양 검사는 있어야 한다"
    assert "'hermes'" not in key and "'master'" not in key


# --- 봇은 하나다 --------------------------------------------------------------

def test_the_seed_has_four_bots_and_no_hermes_direct(sql):
    """`Hermes Direct` 행을 만들면 콘솔에서 Hermes 가 다시 둘이 된다(§13)."""
    found = re.search(r"INSERT INTO bot_catalog(.*?)ON CONFLICT", sql, re.S)

    assert found
    keys = set(re.findall(r"\('([a-z-]+)',", found.group(1)))
    assert keys == {"master", "archiver", "hermes", "clio"}
    assert "hermes_direct" not in found.group(1)


def test_the_seed_is_idempotent(sql):
    """사람이 고친 표시 이름을 재적용이 되돌리면 안 된다."""
    found = re.search(r"INSERT INTO bot_catalog.*?ON CONFLICT \(key\) (\w+ \w+)", sql, re.S)

    assert found and found.group(1) == "DO NOTHING"


def test_master_is_not_internally_invokable(sql):
    """Master 가 자기를 내부 호출 대상으로 두면 라우팅이 자기 자신으로 돈다."""
    found = re.search(r"\('master',.*?\)", sql)

    assert found and found.group(0).endswith("true,  false)")


# --- 이관 --------------------------------------------------------------------

def test_hermes_direct_becomes_hermes(sql):
    """이 한 줄이 이관의 요점이다 — 콘솔에서 Hermes 가 하나로 보이기 시작한다."""
    migration = _section(sql, "WITH moved AS", "INSERT INTO bot_connection_secret")
    # **넣는 열 자리**를 본다. 표 참조(JOIN bot_catalog)에도 같은 CASE 가 있어서,
    # 파일 어딘가에 있는지만 보면 정작 bot_key 가 `hermes_direct` 로 들어가도 통과한다.
    bot_key_column = _section(migration, "SELECT s.workspace,", "'slack_socket',")

    assert bot_key_column, "이관 SELECT 를 찾지 못했다"
    assert "WHEN 'hermes_direct' THEN 'hermes'" in bot_key_column
    assert "'slack_socket'" in migration


def test_the_migration_copies_and_does_not_move(sql):
    """옛 표를 지우면 Archiver 가 다음 기동에서 뜨지 않는다 — 아직 그쪽을 읽는다."""
    assert "DROP TABLE" not in sql
    assert "DELETE FROM workspace_service" not in sql
    assert "TRUNCATE" not in sql


def test_the_ciphertext_is_copied_unchanged(sql):
    """토큰을 다시 암호화하면 **키가 바뀐 날 전부 못 읽는다.**

    이관 때문에 Slack 앱을 재설치하거나 토큰을 재발급하지 않는다(§5).
    """
    migration = _section(sql, "INSERT INTO bot_connection_secret", "INSERT INTO specialist_route")
    body = " ".join(migration.split())

    assert "sec.ciphertext, sec.mask" in body
    assert "ON CONFLICT (connection_id, kind) DO NOTHING" in body


def test_the_migration_is_audited(sql):
    """무엇이 어디로 갔는지 없으면, 이관을 되짚을 좌표가 남지 않는다(§5)."""
    migration = _section(sql, "WITH moved AS", "INSERT INTO bot_connection_secret")

    assert "INSERT INTO archive_config_audit" in migration
    assert "RETURNING" in migration, "실제로 들어간 행만 감사에 남아야 한다"
    assert "workspace_service.hermes_direct" in migration


def test_routes_are_not_backfilled_as_active_wholesale(sql):
    """확인 없이 `active` 로 적으면 이관이 라우팅을 **바꾸는** 일이 된다(§4.5)."""
    backfill = _section(sql, "INSERT INTO specialist_route", "DO $$")
    body = " ".join(backfill.split())

    assert "CASE WHEN" in body
    # 오늘 실제로 라우팅되는 조건이다(`specialist_router` 의 질의와 같다).
    assert "b.state = 'enabled'" in body and "b.health <> 'error'" in body
    assert "ELSE 'disabled'" in body


def test_a_route_row_defaults_to_disabled(sql):
    """행이 없는 것도, 새로 생긴 것도 `disabled` 다 — 기본이 켜짐이면 잊은 봇이 답한다."""
    mode = _column(sql, "specialist_route", "route_mode")

    assert "DEFAULT 'disabled'" in mode


def test_the_route_table_is_separate_from_the_assignment(sql):
    """배정은 저장할 때마다 전량 삭제·재삽입된다(`specialist_store`).

    같은 행에 운영 상태를 두면 배정을 한 번 저장할 때마다 shadow/active 가 조용히
    초기화된다.
    """
    assert "CREATE TABLE IF NOT EXISTS specialist_route" in sql
    assert "ALTER TABLE specialist_workspace" not in sql


# --- 토큰은 나가지 않는다 ------------------------------------------------------

def test_the_archiver_role_cannot_read_connection_secrets(sql):
    """다른 연결의 토큰이 같은 계정에 보이면, 그 계정 하나로 전부 열린다(§4.4)."""
    assert "REVOKE ALL PRIVILEGES ON TABLE bot_connection_secret FROM tybot_archiver" in sql
    grants = re.findall(r"GRANT ([^']*?) ON TABLE ([^']*?) TO tybot_archiver", sql)
    for privileges, tables in grants:
        assert "bot_connection_secret" not in tables, privileges


def test_the_archiver_reads_its_own_tokens_through_one_function(sql):
    """표 SELECT 를 주면 열을 고를 수 없다. 함수가 열을 정한다."""
    assert "CREATE OR REPLACE FUNCTION archiver_connection_config(requested_workspace text)" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = public, pg_temp" in sql
    assert "REVOKE ALL ON FUNCTION archiver_connection_config(text) FROM PUBLIC" in sql


def test_the_function_keeps_the_old_start_conditions(sql):
    """조건이 갈리면 이관 전후로 기동 가능 여부가 달라진다 — 그건 정책 변경이다."""
    body = " ".join(_section(sql, "CREATE OR REPLACE FUNCTION archiver_connection_config", "$$;").split())

    assert "a.bot_key = 'archiver'" in body
    assert "a.state = 'enabled'" in body
    assert "a.identity_ok IS TRUE" in body
    # Master 신원이 확인되기 전에는 Archiver 가 기동하지 않는다.
    assert "m.identity_ok IS TRUE" in body
    assert "m.bot_key = 'master'" in body


def test_the_console_role_can_manage_connections(sql):
    """권한이 없으면 콘솔에게는 그 표가 **없는 것과 같다**(2026-09-14 실측)."""
    assert re.search(
        r"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'\s*'\s*"
        r" bot_catalog, bot_connection, bot_connection_secret,'\s*'\s*"
        r" specialist_route TO tyslackai",
        sql,
    )
    # bigserial 이라 시퀀스 권한이 따로 필요하다 — 없으면 INSERT 가 권한 오류로 죽는다.
    assert "GRANT USAGE, SELECT ON SEQUENCE bot_connection_id_seq TO tyslackai" in sql
