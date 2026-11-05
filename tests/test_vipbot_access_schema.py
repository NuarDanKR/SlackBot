from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "deploy" / "sql" / "vipbot_access_schema.sql"
APPLY = ROOT / "deploy" / "apply-schema.sh"


def sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def function_body(text: str) -> str:
    return text.split("AS $function$", 1)[1].split("$function$;", 1)[0]


def test_the_authorization_window_is_applied_after_its_source_tables():
    apply = APPLY.read_text(encoding="utf-8")

    assert apply.index("bot_access_schema.sql") < apply.index("vipbot_access_schema.sql")
    assert apply.index("bot_source_schema.sql") < apply.index("vipbot_access_schema.sql")


def test_the_window_is_security_definer_with_a_closed_search_path():
    text = sql()

    assert "DROP FUNCTION IF EXISTS public.vipbot_authorization(text, text)" in text
    assert "CREATE FUNCTION public.vipbot_authorization(" in text
    assert "SECURITY DEFINER" in text
    assert "SET search_path = pg_catalog" in text
    assert "REVOKE ALL ON FUNCTION public.vipbot_authorization(text, text) FROM PUBLIC" in text


def test_the_window_reads_only_the_four_authoritative_tables():
    body = function_body(sql())

    for table in ("user_identity", "employee", "bot_access", "bot_source_channel"):
        assert f"public.{table}" in body
    assert "bot_connection" not in body
    assert "bot_connection_secret" not in body


def test_the_window_uses_fixed_vipbot_grants_and_sources():
    body = function_body(sql())

    assert "a.bot = 'vipbot'" in body
    assert "a.revoked_at IS NULL" in body
    assert "g.expires_at <= statement_timestamp()" in body
    assert body.count("s.bot = 'vipbot'") == 2
    assert body.count("s.removed_at IS NULL") == 2


def test_denied_requests_never_receive_source_coordinates():
    body = function_body(sql())

    assert "CASE WHEN d.code = 'allowed' THEN s.workspace END" in body
    assert "CASE WHEN d.code = 'allowed' THEN s.channel_id END" in body
    assert "ON d.code = 'allowed'" in body


def test_every_runtime_decision_is_a_fixed_code():
    body = function_body(sql())

    for code in (
        "no_identity",
        "unknown_employee",
        "inactive_employee",
        "no_grant",
        "expired",
        "no_sources",
        "allowed",
    ):
        assert f"'{code}'" in body


def test_the_runtime_role_gets_no_direct_access_to_authorization_tables():
    text = sql()

    for table in ("employee", "user_identity", "bot_access", "bot_source_channel"):
        assert f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM tybot_vipbot" in text
    assert "GRANT EXECUTE ON FUNCTION public.vipbot_authorization(text, text)" in text
    assert "TO tybot_vipbot" in text


def test_the_window_has_no_identity_write_path():
    body = function_body(sql()).upper()

    assert "INSERT " not in body
    assert "UPDATE " not in body
    assert "DELETE " not in body


def test_the_two_windows_have_a_closed_dedicated_owner():
    text = sql()

    assert "tybot_vipbot_definer role is required" in text
    assert (
        "ALTER FUNCTION public.vipbot_connection_config(text)\n"
        "        OWNER TO tybot_vipbot_definer"
    ) in text
    assert (
        "ALTER FUNCTION public.vipbot_authorization(text, text)\n"
        "        OWNER TO tybot_vipbot_definer"
    ) in text
    assert "TO tybot_vipbot_definer" in text


def test_reapply_removes_old_connection_window_execute_grants():
    text = sql()

    assert "aclexplode(" in text
    assert "acl.privilege_type = 'EXECUTE'" in text
    assert "REVOKE EXECUTE ON FUNCTION public.vipbot_connection_config(text)" in text


def test_both_security_definers_explicitly_revoke_public_execute():
    text = sql()

    assert "REVOKE ALL ON FUNCTION public.vipbot_authorization(text, text) FROM PUBLIC" in text
    assert "REVOKE ALL ON FUNCTION public.vipbot_connection_config(text) FROM PUBLIC" in text


def test_connection_acl_cleanup_covers_named_roles_and_public():
    text = sql()

    assert "JOIN pg_roles r ON r.oid = acl.grantee" in text
    assert "REVOKE EXECUTE ON FUNCTION public.vipbot_connection_config(text)" in text
    assert "REVOKE ALL ON FUNCTION public.vipbot_connection_config(text) FROM PUBLIC" in text
