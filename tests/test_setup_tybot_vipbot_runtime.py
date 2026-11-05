from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "setup-tybot-vipbot-runtime.sh"


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_plan_mode_precedes_every_privileged_command():
    text = source()
    plan = text.index('if [[ "$MODE" == plan ]]')
    plan_exit = text.index("exit 0", plan)

    assert "psql_q" not in text[plan:plan_exit]
    assert "psql_run" not in text[plan:plan_exit]
    assert "sudo -u postgres" not in text[plan:plan_exit]
    assert text.index("[[ $EUID -eq 0 ]]", plan_exit) > plan_exit


def test_every_database_command_pins_the_target_host_port_and_database():
    text = source()

    assert 'HOST="${TYBOT_DB_HOST:-/run/postgresql}"' in text
    assert 'psql_q() { sudo -u postgres psql' in text
    assert 'psql_run() { sudo -u postgres psql' in text
    assert '-h "$HOST" -p "$PORT" -d "$DB"' in text


def test_roles_are_separate_and_narrow():
    text = source()

    assert "ROLE_GROUP=tybot_vipbot" in text
    assert "ROLE_RUNTIME=tybot_vipbot_runtime" in text
    assert "LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE" in text
    assert "NOREPLICATION NOBYPASSRLS" in text
    assert "GRANT $ROLE_GROUP TO $ROLE_RUNTIME" in text
    assert "WITH ADMIN OPTION" not in text
    assert "$ROLE_GROUP 이 다른 역할의 멤버입니다" in text


def test_existing_excess_privilege_is_refused_before_role_changes():
    text = source()
    review = text.index('if [[ "$runtime_exists" == 1 ]]')
    create = text.index('psql_run "$SQL_CREATE"')

    assert review < create
    for word in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"):
        assert word in text[review:create]
    assert "기존 멤버십이 계약과 다릅니다" in text[review:create]


def test_the_privilege_group_rejects_unexpected_existing_members():
    text = source()
    group_review = text.index('group_members="$(psql_q')
    create = text.index('psql_run "$SQL_CREATE"')

    assert group_review < create
    assert "WHERE g.rolname='$ROLE_GROUP'" in text[group_review:create]
    assert '"$ROLE_RUNTIME:false"' in text[group_review:create]
    assert "$ROLE_GROUP 의 기존 멤버가 계약과 다릅니다" in text[group_review:create]


def test_password_never_enters_an_argument_or_sql_literal():
    text = source()

    assert "PASSWORD '" not in text
    assert "--password" not in text
    assert "\\password $ROLE_RUNTIME" in text


def test_verification_denies_direct_secret_table_access():
    text = source()

    assert "role_table_grants" in text
    assert "bot_connection','bot_connection_secret" in text
    assert "vipbot_connection_config(text)" in text
    assert "acl.grantee = 0" in text


def test_the_documented_credential_matches_the_resolver():
    from tybot import vipbot_dsn

    text = source()
    assert vipbot_dsn.CREDENTIAL_NAME in text
    assert vipbot_dsn.RUNTIME_ROLE in text
    assert "VIPBOT_DATABASE_URL" not in text, "DSN 값은 명령 인자로 받지 않는다"
