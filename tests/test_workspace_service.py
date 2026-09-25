"""워크스페이스 하나 · 서비스 여럿 — **토큰은 나가지 않는다.**

결정: 2026-09-25 오너 확정 §2·§3. 스키마: `deploy/sql/workspace_service_schema.sql`.

여기서 지키는 것.

1. 봇마다 워크스페이스를 만들지 않는다. 권한이 두 곳에 있으면 한 곳만 고치는
   날이 오고, 그날 **한쪽에서만** 새어 나간다
2. 평문 토큰이 화면·로그·감사 어디에도 안 나간다
3. 최종 Hermes specialist 는 Slack 토큰을 갖지 않는다
4. 신원 검사를 통과하기 전에는 서비스가 켜지지 않는다
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tybot.console import workspace_service_store as svc
from tybot.console.workspace_store import canonical_archive_path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "deploy" / "sql" / "workspace_service_schema.sql"
APPLY = ROOT / "deploy" / "apply-schema.sh"


def _sql() -> str:
    """주석을 지운 SQL. 주석에 적어 둔 것이 선언으로 세어지면 안 된다."""
    return re.sub(r"--[^\n]*", "", SCHEMA.read_text(encoding="utf-8"))


# --- 정본 아카이브 경로 -------------------------------------------------------

def test_archive_path_points_where_the_files_actually_are():
    """화면에 보이는 경로가 실제와 다르면 그건 정보가 아니라 **오정보**다.

    등록 때 `<root>/<key>` 로 적어 두었는데 실제 원문은
    `<root>/workspaces/<key>/channels/...` 에 있다. 그 화면을 보고 서버에 들어간
    사람은 빈 디렉터리를 보고 「수집이 죽었다」 고 읽는다.
    """
    assert canonical_archive_path("/var/lib/tybot/archive", "tyit") == (
        "/var/lib/tybot/archive/workspaces/tyit"
    )


def test_archive_path_never_uses_backslashes():
    """개발 PC 에서 등록해도 리눅스 서버 경로가 나와야 한다."""
    got = canonical_archive_path(Path(r"C:\var\lib\tybot\archive"), "tyit")

    assert "\\" not in got
    assert got.endswith("/workspaces/tyit")


def test_archive_path_does_not_double_the_separator():
    assert canonical_archive_path("/var/lib/tybot/archive/", "tyit") == (
        "/var/lib/tybot/archive/workspaces/tyit"
    )


def test_the_store_writes_the_canonical_path():
    """호출부가 직접 경로를 조립하면 한 군데가 옛 모양으로 남는다."""
    source = (ROOT / "src" / "tybot" / "console" / "workspace_store.py").read_text(
        encoding="utf-8"
    )

    assert "canonical_archive_path(archive_root, key)" in source
    assert "str(archive_root / key)" not in source


def test_the_migration_only_touches_the_old_shape():
    """사람이 손으로 넣은 경로까지 고치지 않는다. 그건 이 migration 이 판단할 일이 아니다."""
    sql = _sql()

    assert "UPDATE workspace" in sql
    assert "archive_path NOT LIKE '%/workspaces/%'" in sql
    assert "archive_path LIKE '%/' || key" in sql


# --- 서비스 모델 -------------------------------------------------------------

def test_the_three_services_and_no_more():
    """`hermes_direct` 는 공존 기간에만 쓴다. 늘어나면 표와 코드가 갈린다."""
    sql = _sql()
    found = re.search(r"service\s+text NOT NULL\s*CHECK \(service IN \(([^)]*)\)", sql)

    assert found
    assert set(re.findall(r"'(\w+)'", found.group(1))) == {
        "master", "archiver", "hermes_direct"
    }
    assert {member.value for member in svc.Service} == {
        "master", "archiver", "hermes_direct"
    }


def test_several_services_share_one_workspace():
    """PK 가 `(workspace, service)` 라 한 워크스페이스에 여럿이 붙는다."""
    assert "PRIMARY KEY (workspace, service)" in _sql()


def test_services_reference_the_single_workspace_row():
    """워크스페이스를 새로 만들지 않고 **기존 행을 참조**한다."""
    sql = _sql()

    assert "REFERENCES workspace(key) ON DELETE CASCADE" in sql


def test_the_hermes_specialist_holds_no_slack_token():
    """전문 봇이 Slack 에 직접 붙으면 우리가 권한을 판정할 자리가 사라진다.

    계약이 「우리가 필터한 텍스트만 준다」 인데 자기 토큰이 있으면 그 계약이
    무의미해진다. 접근 범위는 `specialist_workspace` 가 정한다.
    """
    sql = _sql()
    services = re.search(r"service\s+text NOT NULL\s*CHECK \(service IN \(([^)]*)\)", sql)

    # 토큰을 가질 수 있는 서비스 목록에 specialist 가 **없다.**
    assert services and "specialist" not in services.group(1)
    # 토큰 표는 오직 `workspace_service` 만 참조한다. 다른 표에서 딸려 오는
    # 경로가 생기면 그 경로로 전문 봇이 토큰을 갖게 된다.
    secret = sql[sql.index("CREATE TABLE IF NOT EXISTS workspace_service_secret"):]
    secret = secret[: secret.index(");")]
    assert secret.count("REFERENCES") == 1
    assert "REFERENCES workspace_service (workspace, service)" in secret
    # 그러면 범위는 어디서 오나 — 주석이 그 답을 들고 있어야 한다.
    assert "specialist_workspace" in SCHEMA.read_text(encoding="utf-8")


# --- 신원 검사 ---------------------------------------------------------------

def test_a_token_from_another_workspace_is_refused():
    """토큰을 잘못 붙이면 **다른 워크스페이스에 수집한다.** 오류가 아니라 유출이다."""
    problem = svc.check_identity(
        svc.Identity("T_OURS", ""), svc.Identity("T_THEIRS", "U_ARCHIVER")
    )

    assert "다른 워크스페이스" in problem


def test_reusing_the_same_bot_user_is_refused():
    """같은 봇 사용자면 별도 앱이 아니라 같은 앱을 두 번 등록한 것이다.

    그 상태로 Socket Mode 를 두 곳에서 열면 이벤트를 양쪽이 받는다 —
    중복 답변과 비용 2배(CLAUDE.md 금지사항).
    """
    problem = svc.check_identity(
        svc.Identity("T_OURS", "U_MASTER"), svc.Identity("T_OURS", "U_MASTER")
    )

    assert "이미 다른 서비스가" in problem


def test_a_matching_identity_passes():
    assert svc.check_identity(
        svc.Identity("T_OURS", "U_MASTER"), svc.Identity("T_OURS", "U_ARCHIVER")
    ) == ""


def test_missing_identity_fields_are_refused():
    """Slack 이 값을 안 주면 **모르는 것**이다. 모르면 막는다."""
    assert svc.check_identity(svc.Identity("T", ""), svc.Identity("", "U")) != ""
    assert svc.check_identity(svc.Identity("T", ""), svc.Identity("T", "")) != ""


def test_a_service_cannot_be_enabled_without_passing_identity():
    """등록과 동시에 켤 수 있으면 토큰을 잘못 붙인 채로 수집이 시작된다."""
    sql = _sql()
    found = re.search(
        r"ADD CONSTRAINT workspace_service_enabled_needs_identity\s*CHECK \((.*?)\);",
        sql, re.S,
    )

    assert found
    body = " ".join(found.group(1).split())
    assert "state <> 'enabled'" in body
    assert "identity_ok IS TRUE" in body


def test_two_services_cannot_share_a_bot_user_in_the_database():
    """코드가 막아도 psql 로 들어오는 길이 있다. 표도 막아야 한다."""
    sql = _sql()

    assert "CREATE UNIQUE INDEX IF NOT EXISTS workspace_service_distinct_bot_user" in sql
    assert "WHERE bot_user_id <> ''" in sql, "빈 값끼리 충돌하면 등록 자체가 막힌다"


# --- 토큰이 나가지 않는다 -----------------------------------------------------

def test_there_is_no_decrypt_helper():
    """있으면 언젠가 로그에 찍히고, **로그는 감사보다 오래 남는다.**"""
    source = (
        ROOT / "src" / "tybot" / "console" / "workspace_service_store.py"
    ).read_text(encoding="utf-8")

    assert "decrypt" not in source


def test_listing_selects_masks_not_ciphertext():
    source = (
        ROOT / "src" / "tybot" / "console" / "workspace_service_store.py"
    ).read_text(encoding="utf-8")
    query = source[source.index("def list_services"):source.index("def save_service")]

    assert "mask" in query
    assert "ciphertext" not in query


def test_audit_records_only_the_mask():
    """평문은 화면·로그·감사 어디에도 안 나간다(오너 결정 §3)."""
    summary = svc.mask_summary({
        "bot": (b"cipher", "xoxb-1234…9f0c"),
        "app": (b"cipher", "xapp-5678…1a2b"),
    })

    assert summary == "app=xapp-5678…1a2b bot=xoxb-1234…9f0c"
    assert "cipher" not in summary


def test_the_archiver_role_cannot_read_service_secrets():
    """봇은 토큰을 env 로 받는다. DB 에서 읽을 수 있으면 DB 를 읽는 것이 전부 읽는다."""
    sql = _sql()

    assert "REVOKE ALL PRIVILEGES ON TABLE workspace_service_secret FROM tybot_archiver" in sql
    assert "GRANT SELECT ON TABLE workspace_service TO tybot_archiver" in sql


# --- 기존 토큰 이관 -----------------------------------------------------------

def test_legacy_secrets_move_to_the_master_service():
    sql = _sql()

    assert "INSERT INTO workspace_service_secret" in sql
    assert "FROM workspace_secret s" in sql
    assert "'master'" in sql


def test_the_migration_does_not_delete_the_old_table():
    """봇이 아직 `workspace_secret` 을 읽는다. 지우면 **다음 기동에서 전부 안 뜬다.**"""
    sql = _sql()

    assert "DROP TABLE" not in sql
    assert "DELETE FROM workspace_secret" not in sql


def test_reapplying_does_not_roll_back_a_newer_token():
    """콘솔에서 새 토큰을 넣었는데 재적용이 옛 값으로 되돌리면 조용한 롤백이다."""
    sql = _sql()
    block = sql[sql.index("INSERT INTO workspace_service_secret"):]

    assert "ON CONFLICT (workspace, service, kind) DO NOTHING" in block
    assert "DO UPDATE" not in block[: block.index(";")]


def test_migrated_services_start_disabled():
    """이관된 것도 신원 검사를 거쳐야 켜진다."""
    sql = _sql()
    block = sql[sql.index("INSERT INTO workspace_service ("):]

    assert "'disabled'" in block[: block.index(";")]


# --- 배포 경로 ---------------------------------------------------------------

def test_the_schema_is_registered_for_deployment():
    """목록에 없으면 **한 번도 적용되지 않는다**(2026-09-14 사고 경로)."""
    text = APPLY.read_text(encoding="utf-8")

    assert "workspace_service_schema.sql" in text
    # `workspace` 표를 참조하므로 `console_schema.sql` 뒤여야 한다.
    assert text.index("console_schema.sql") < text.index("workspace_service_schema.sql")


def test_the_schema_is_one_transaction():
    text = _sql()

    assert text.lstrip().startswith("BEGIN;")
    assert text.rstrip().endswith("COMMIT;")


@pytest.mark.parametrize("service", list(svc.Service))
def test_every_service_is_token_bearing_except_the_specialist(service):
    """이 집합에 **specialist 가 없는 것**이 요점이다."""
    assert service in svc.TOKEN_BEARING
    assert "specialist" not in service.value
