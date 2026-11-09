"""B-83 VIPBot 전용 DB 계정과 서비스 경계 — **DB 없이 도는** 계약 시험.

실제 권한은 `tests/test_vipbot_runtime_role_db.py` 가 PostgreSQL 에 물어본다.
여기서 붙드는 것은 **설치 절차와 유닛의 모양**이다. 그 둘은 서버에서 한 번만
읽히고, 틀려도 조용하다 — 자격증명 이름 한 글자가 어긋나면 런타임은
「설정이 없다」 로 멎고, 사람은 DSN 파일을 몇 번이고 다시 적는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tybot import vipbot_anthropic_credential, vipbot_dsn

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "deploy" / "setup-tybot-vipbot-runtime.sh"
UNIT = ROOT / "deploy" / "tybot-vipbot.service"
SCHEMA = ROOT / "deploy" / "sql" / "bot_connection_schema.sql"
RUNTIME_MODULE = ROOT / "src" / "tybot" / "vipbot_runtime.py"
ROLE_DB_TEST = ROOT / "tests" / "test_vipbot_runtime_role_db.py"

GROUP = "tybot_vipbot"
RUNTIME = "tybot_vipbot_runtime"
CONFIG_FN = "public.vipbot_connection_config(text)"
ACCESS_FN = "public.vipbot_authorization(text,text)"


def setup() -> str:
    return SETUP.read_text(encoding="utf-8")


def code() -> str:
    """주석을 뺀 스크립트. 머리말은 **안 하는 것**을 설명하므로, 그대로 훑으면
    설명문이 금지어 검사에 걸린다. 서버에서 도는 것은 코드뿐이다."""
    return "\n".join(raw for raw in setup().splitlines() if not raw.lstrip().startswith("#"))


def unit() -> str:
    return UNIT.read_text(encoding="utf-8")


def role_db_test() -> str:
    return ROLE_DB_TEST.read_text(encoding="utf-8")


def directives() -> list[str]:
    """주석을 뺀 유닛 지시어들. 주석은 **안 넣은 것**을 설명하므로 그대로
    훑으면 설명문에 걸린다. 설치되는 것은 지시어뿐이다."""
    return [line.strip() for raw in unit().splitlines() if (line := raw.split("#", 1)[0]).strip()]


# --- 1. 두 역할과 그 사이 ----------------------------------------------------


def test_the_group_is_the_one_that_holds_the_grant_and_never_logs_in():
    body = setup()
    assert f"ROLE_GROUP={GROUP}" in body
    assert f"ROLE_RUNTIME={RUNTIME}" in body
    # 그룹이 LOGIN 이면 권한을 쥔 역할이 곧 접속 자격을 갖는다.
    assert 'fail "$ROLE_GROUP 이 LOGIN 입니다' in body


def test_the_runtime_role_name_matches_the_one_the_code_demands():
    # 이름이 갈리면 DSN 은 멀쩡해 보이는데 런타임이 `wrong_role` 로 거절한다.
    assert vipbot_dsn.RUNTIME_ROLE == RUNTIME
    assert RUNTIME in setup()


def test_the_login_role_gets_exactly_the_least_privilege_attributes():
    attrs = re.search(r'^ATTRS="([^"]+)"', setup(), re.M).group(1).split()
    assert attrs == [
        "LOGIN",
        "INHERIT",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ]


def test_membership_is_granted_without_admin_option():
    body = setup()
    assert 'SQL_MEMBER="GRANT $ROLE_GROUP TO $ROLE_RUNTIME;"' in body
    # ADMIN OPTION 을 주면 접속 계정이 그룹을 **남에게 줄 수 있다.**
    assert "WITH ADMIN OPTION" not in code()
    assert "ADMIN OPTION" in body  # 거절 사유로는 이름이 나와야 한다


def test_the_script_grants_nothing_to_public():
    assert not re.search(r"GRANT[^;]*TO\s+PUBLIC", code(), re.I)


# --- 2. 남의 역할을 넘겨받지 않는다 -------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "의 기존 멤버십이 계약과 다릅니다",
        "에 과한 속성이 있습니다",
        "이 스크립트는 남이 만든 역할을 넘겨받지 않습니다",
    ],
)
def test_a_role_that_is_already_something_else_is_refused_not_repaired(marker):
    assert marker in setup()


def test_nothing_is_altered_before_the_preflight_has_passed():
    """순서가 뒤집히면 **검증이 실패하는 동안 로그인할 수 있는 창**이 열린다."""
    body = setup()
    preflight = body.index("기존 역할 사전 검토 —")
    for statement in (
        'psql_run "$SQL_CREATE"',
        'psql_run "$SQL_ENFORCE"',
        'psql_run "$SQL_MEMBER"',
    ):
        assert body.index(statement) > preflight


def test_the_preflight_never_strips_a_membership_or_lowers_an_attribute():
    body = code()
    preflight = body[body.index("runtime_exists=") : body.index('if [[ "$MODE" == apply')]
    assert "REVOKE" not in preflight
    assert "ALTER ROLE" not in preflight
    assert "DROP ROLE" not in preflight
    assert preflight.count("exit 1") == 2


def test_the_password_never_travels_as_an_argument():
    body = setup()
    assert "\\\\password $ROLE_RUNTIME" in body or "\\password $ROLE_RUNTIME" in body
    assert "--set-password" in body
    assert "PGPASSWORD" not in body


def test_plan_mode_contacts_nothing_so_it_can_be_read_before_any_change():
    body = setup()
    # 계획은 root 검사와 psql 검사보다 **앞에서** 끝난다.
    assert body.index('if [[ "$MODE" == plan ]]') < body.index("[[ $EUID -eq 0 ]]")
    assert body.index("PLAN\n  exit 0") < body.index("command -v psql")


# --- 3. 확인은 「실제로 무엇을 할 수 있나」 를 본다 -----------------------------


def test_verify_asserts_the_function_is_executable_through_the_group():
    body = setup()
    assert f'CONFIG_FN="{CONFIG_FN}"' in body
    assert "has_function_privilege('$ROLE_RUNTIME','$CONFIG_FN','EXECUTE')" in body
    assert f'ACCESS_FN="{ACCESS_FN}"' in body
    assert "has_function_privilege('$ROLE_RUNTIME','$ACCESS_FN','EXECUTE')" in body


@pytest.mark.parametrize(
    "table",
    [
        "bot_connection",
        "bot_connection_secret",
        "employee",
        "user_identity",
        "bot_access",
        "bot_source_channel",
    ],
)
def test_verify_asserts_the_vipbot_base_tables_stay_closed(table):
    body = setup()
    assert table in body
    assert "has_table_privilege('$ROLE_RUNTIME','public.$table','$privilege')" in body
    assert "for privilege in SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER" in body


def test_verify_asserts_public_and_other_bots_are_closed():
    body = setup()
    assert "has_function_privilege('public','$CONFIG_FN','EXECUTE')" in body
    assert "has_function_privilege('public','$ACCESS_FN','EXECUTE')" in body
    assert "archiver_connection_config(text)" in body


def test_verify_checks_every_table_privilege_and_other_definer_function():
    body = setup()

    assert "SELECT INSERT UPDATE DELETE TRUNCATE REFERENCES TRIGGER" in body
    assert "p.prosecdef" in body
    assert "other_definers" in body
    assert "ROLE_DEFINER=tybot_vipbot_definer" in body
    assert "p.oid IN ('$CONFIG_FN'::regprocedure, '$ACCESS_FN'::regprocedure)" in body
    assert "r.rolname <> '$ROLE_DEFINER'" in body


# --- 4. 스키마 쪽 계약 --------------------------------------------------------


def test_the_schema_gives_the_group_the_function_and_takes_the_tables_away():
    body = SCHEMA.read_text(encoding="utf-8")
    block = body[body.index(f"rolname = '{GROUP}'") :]
    block = block[: block.index("END")]
    assert "REVOKE ALL PRIVILEGES ON TABLE bot_connection_secret" in block
    assert re.search(r"REVOKE ALL PRIVILEGES ON TABLE bot_connection\b", block)
    assert "GRANT EXECUTE ON FUNCTION public.vipbot_connection_config(text)" in block
    assert "GRANT SELECT" not in block


def test_the_window_is_revoked_from_public_where_it_is_defined():
    body = SCHEMA.read_text(encoding="utf-8")
    assert f"REVOKE ALL ON FUNCTION {CONFIG_FN} FROM PUBLIC" in body


def test_the_window_pins_the_bot_key_so_the_caller_cannot_choose_it():
    body = SCHEMA.read_text(encoding="utf-8")
    window = body[body.index("CREATE OR REPLACE FUNCTION vipbot_connection_config(") :]
    window = window[: window.index("$$;")]
    assert "v.bot_key = 'vipbot'" in window
    assert "SECURITY DEFINER" in window
    assert "SET search_path = pg_catalog" in window
    assert "FROM public.bot_connection v" in window


# --- 5. systemd 유닛 ----------------------------------------------------------


def test_the_dsn_arrives_as_a_credential_under_the_name_the_code_looks_for():
    assert (
        f"LoadCredential={vipbot_dsn.CREDENTIAL_NAME}:/etc/tybot/{vipbot_dsn.CREDENTIAL_NAME}"
    ) in unit()


def test_the_model_key_arrives_as_a_dedicated_credential():
    assert (
        f"LoadCredential={vipbot_anthropic_credential.CREDENTIAL_NAME}:"
        f"/etc/tybot/{vipbot_anthropic_credential.CREDENTIAL_NAME}"
    ) in unit()
    assert "Environment=ANTHROPIC_API_KEY" not in unit()


def test_the_unit_never_hands_the_process_the_master_database_url():
    """`/etc/tybot/tybot.env` 를 읽으면 전용 계정을 만든 의미가 사라진다.

    2026-11-06: `EnvironmentFile=` 자체는 이제 하나 있다 — VIPBot 전용 설정
    파일이다. 금지는 **그 파일이 아니라 공용 파일**이고, 허용되는 경로는 하나뿐.
    """
    lines = directives()
    assert [
        line for line in lines if line.startswith("EnvironmentFile=")
    ] == ["EnvironmentFile=/etc/tybot/tybot-vipbot.conf"]
    assert not any("tybot.env" in line for line in lines)
    assert not any(line.startswith("Environment=DATABASE_URL") for line in lines)
    assert not any("TYBOT_ENV_FILE" in line for line in lines)


def test_no_secret_value_is_written_into_the_unit_itself():
    for line in directives():
        assert "postgresql://" not in line
        assert "xoxb-" not in line
        assert "xapp-" not in line


def test_the_encryption_key_also_arrives_as_a_credential():
    lines = directives()
    assert any(line.startswith("LoadCredential=tybot-workspace-secret-key:") for line in lines)
    assert "Environment=WORKSPACE_SECRET_KEY_FILE=%d/tybot-workspace-secret-key" in lines


def test_the_draft_cannot_be_enabled_until_the_runtime_module_exists():
    """`[Install]` 이 없으면 `systemctl enable` 이 거부된다.

    2026-11-06: 런타임 모듈(`tybot.vipbot_runtime`)이 생겼다. 그래도 `[Install]`
    은 **여전히 없다** — 「모듈이 섰다」 와 「켜도 된다」 는 다른 완료다. 실제
    Slack 앱 설치와 운영 승인이 끝난 날 이 시험을 고치는 것이, 그 결정을
    저장소에 남기는 방법이다.
    """
    assert RUNTIME_MODULE.exists(), "런타임 모듈이 사라졌다 — ExecStartPre 가 깨진다"
    assert "[Install]" not in directives()
    assert not any(line.startswith("WantedBy=") for line in directives())
    assert 'ExecStartPre=/opt/tybot/.venv/bin/python -c "import tybot.vipbot_runtime"' in unit()


def test_the_unit_runs_as_the_service_account_in_a_sandbox():
    lines = directives()
    assert "User=tybot" in lines
    assert "Group=tybot" in lines
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "UMask=0077",
    ):
        assert directive in lines


def test_the_unit_opens_no_inbound_port_and_never_writes_the_archive():
    lines = directives()
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in lines
    assert "ReadOnlyPaths=/var/lib/tybot/archive" in lines
    assert not any(line.startswith("ReadWritePaths=") and "archive" in line for line in lines)
    assert not any(line.startswith("ListenStream=") for line in lines)


def test_only_one_vipbot_process_can_run():
    lines = directives()
    assert "Type=simple" in lines
    assert "Restart=always" in lines
    assert not any(line.startswith("ExecStart=") and "&" in line for line in lines)


def test_role_attribute_checks_do_not_depend_on_postgres_boolean_text():
    script = SETUP.read_text(encoding="utf-8")

    # `boolean::text` is `true`/`false`, while unaligned psql display is `t`/`f`.
    # Mixing the two made a real NOLOGIN role look like LOGIN on PostgreSQL 18.
    assert "rolcanlogin FROM pg_roles" not in script
    assert "rolcanlogin||" not in script
    assert "CASE WHEN rolcanlogin THEN 't' ELSE 'f' END" in script
    assert "CASE WHEN rolinherit THEN 't' ELSE 'f' END" in script
    assert script.count("CASE WHEN rolbypassrls THEN 't' ELSE 'f' END") == 2
    assert (
        "has_function_privilege('$ROLE_RUNTIME','$CONFIG_FN','EXECUTE')::text"
        not in script
    )
    assert (
        "has_function_privilege('$ROLE_RUNTIME','$ACCESS_FN','EXECUTE')::text"
        not in script
    )
    assert (
        "has_function_privilege("
        "'$ROLE_RUNTIME','archiver_connection_config(text)','EXECUTE')::text"
        not in script
    )


def test_function_owner_check_compares_roles_not_rendered_signatures():
    script = SETUP.read_text(encoding="utf-8")

    assert "pg_get_function_identity_arguments" not in script
    assert "AND r.rolname <> '$ROLE_DEFINER'" in script
    assert '[[ -z "$wrong_function_owners" ]]' in script


def test_the_installer_stages_vipbot_only_after_an_explicit_opt_in():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    block = install.split("# VIPBOT_STAGE:BEGIN", 1)[1].split(
        "# VIPBOT_STAGE:END", 1
    )[0]

    assert block.count('if [[ "${WITH_VIPBOT:-0}" == "1" ]]') == 1
    assert "tybot-vipbot.service" in block
    assert "tybot-vipbot.conf.example" in block
    assert "systemctl start" not in block
    assert "systemctl enable" not in block
    assert "--set-password" not in block
    assert "tybot-vipbot-database-url" not in block
    assert "tybot-vipbot-anthropic-api-key" not in block


def test_the_vipbot_stage_preserves_existing_settings_and_creates_no_secret():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    block = install.split("# VIPBOT_STAGE:BEGIN", 1)[1].split(
        "# VIPBOT_STAGE:END", 1
    )[0]

    assert 'if [[ ! -f "$CONF_DIR/tybot-vipbot.conf" ]]' in block
    assert "install -o root -g tybot -m 0640" in block
    assert "sed -i" not in block
    assert "REPLACE_ME" not in block


def test_the_one_time_activation_procedure_stops_the_restarting_service():
    guide = (
        ROOT / "docs" / "verification" / "2026-11-06-vipbot-activation.md"
    ).read_text(encoding="utf-8")
    start = guide.index("### 9. 한 번 띄워 보기")
    stop = guide.index("### 10.", start)
    procedure = guide[start:stop]

    assert "Restart=always" in procedure
    assert "systemctl start tybot-vipbot" in procedure
    assert "systemctl stop tybot-vipbot" in procedure
    assert procedure.index("systemctl start tybot-vipbot") < procedure.index(
        "systemctl stop tybot-vipbot"
    )


def test_the_database_test_refuses_an_operational_cluster_before_any_grant():
    body = role_db_test()
    refusal = body.index("refusing a PostgreSQL cluster with pre-existing VIPBot roles")
    first_grant = body.index('cur.execute(f"GRANT {GROUP} TO {RUNTIME}")')
    assert refusal < first_grant
    assert "if granted_runtime:" in body
    assert 'cur.execute(f"REVOKE {RUNTIME} FROM CURRENT_USER")' in body
