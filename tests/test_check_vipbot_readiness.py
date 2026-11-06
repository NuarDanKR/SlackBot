"""VIPBot 활성화 준비 점검 시험 (B-83). **DB·Slack·systemctl 0회.**

여기 쓰는 값은 전부 합성이다. 실제 토큰·키·DSN 을 넣지 않는다.

붙들고 있는 것은 넷이다.

1. 판정(`evaluate`)은 **순수 함수**다 — 사실 묶음만 보고 말한다
2. 점검은 **아무것도 바꾸지 않는다** — `systemctl`·`GRANT`·`auth_test` 가 없다
3. 비밀 값은 **출력에도 사유 코드에도** 안 들어간다
4. 사유 코드는 고정 집합 밖으로 못 나간다
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_vipbot_readiness.py"
UNIT = ROOT / "deploy" / "tybot-vipbot.service"

_spec = importlib.util.spec_from_file_location("check_vipbot_readiness", SCRIPT)
check = importlib.util.module_from_spec(_spec)
# dataclass 가 자기 모듈을 `sys.modules` 에서 찾는다. 등록 전에 실행하면 거기서 터진다.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

DSN = "postgresql://tybot_vipbot_runtime:pw@/tyslackai?host=/run/postgresql"
KEY = "sk" + "-ant-" + "synthetic-readiness-000000000000"


def facts(**changes) -> check.Facts:
    """막는 것이 하나도 없는 상태. 시험마다 한 군데씩 무너뜨린다."""
    values = {
        "roles": {
            check.GROUP_ROLE: {
                "can_login": False,
                "unsafe": "",
                "memberships": [],
                "members": [(check.RUNTIME_ROLE, False)],
            },
            check.RUNTIME_ROLE: {
                "can_login": True,
                "unsafe": "",
                "memberships": [(check.GROUP_ROLE, False)],
                "members": [],
            },
            check.DEFINER_ROLE: {
                "can_login": False,
                "unsafe": "",
                "memberships": [],
                "members": [],
            },
        },
        "functions": {
            signature: {
                "owner": check.DEFINER_ROLE,
                "runtime_execute": True,
                "public_execute": False,
            }
            for signature in (check.CONFIG_FN, check.ACCESS_FN)
        },
        "base_tables": dict.fromkeys(check.BASE_TABLES, False),
        "connection": {"workspace": "pilot", "rows": 1, "complete": True},
        "credentials": (
            {"name": "tybot-vipbot-database-url", "present": True},
            {"name": "tybot-workspace-secret-key", "present": True},
            {"name": "tybot-vipbot-anthropic-api-key", "present": True},
        ),
        "archive": {"configured": True, "readable": True, "path": "/var/lib/tybot/archive"},
        "settings": {"workspace": "pilot", "budget": "ok"},
        "settings_file": {
            "present": True,
            "path": check.SETTINGS_FILE,
            "missing": (),
            "forbidden": (),
            "invalid_lines": 0,
        },
        "unit": {
            "present": True,
            "path": str(UNIT),
            "shared_env": (),
            "credentials": ("tybot-vipbot-database-url",),
            "settings_file": True,
            "hardcoded_settings": (),
        },
    }
    values.update(changes)
    return check.Facts(**values)


def codes(found) -> list[str]:
    return [item.code for item in found]


# --- 1. 다 갖춰진 상태 ---------------------------------------------------------


def test_a_fully_prepared_server_has_nothing_to_report():
    assert check.evaluate(facts()) == []


def test_every_code_this_check_can_say_is_fixed():
    for code in check.FINDING_CODES:
        assert check.Finding(code).code == code
    with pytest.raises(ValueError, match="unknown readiness finding"):
        check.Finding("그럭저럭")


def test_a_finding_detail_is_never_a_secret_value():
    # 사유에 들어가는 것은 이름·경로·개수뿐이다. 값이 들어갈 자리가 없다.
    found = check.evaluate(
        facts(
            credentials=(
                {"name": "tybot-vipbot-database-url", "present": True, "invalid": True},
            )
        )
    )
    assert codes(found) == ["credential_invalid"]
    assert DSN not in str(found[0]) and KEY not in str(found[0])
    # 사유에 적히는 것은 **이름 하나**다. 사실 묶음을 통째로 붙이면 언젠가
    # 값이 들어 있는 칸이 생기고, 그날 이 출력이 티켓으로 나간다.
    assert found[0].detail == "tybot-vipbot-database-url"


# --- 2. DB 역할·창구 ----------------------------------------------------------


@pytest.mark.parametrize(
    "role", [check.GROUP_ROLE, check.RUNTIME_ROLE, check.DEFINER_ROLE]
)
def test_a_missing_role_is_reported(role):
    roles = dict(facts().roles)
    roles.pop(role)
    assert "role_missing" in codes(check.evaluate(facts(roles=roles)))


@pytest.mark.parametrize("role", [check.GROUP_ROLE, check.DEFINER_ROLE])
def test_a_group_role_that_can_log_in_is_reported(role):
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[role]["can_login"] = True
    assert "role_can_login" in codes(check.evaluate(facts(roles=roles)))


def test_an_unsafe_role_attribute_is_reported():
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[check.RUNTIME_ROLE]["unsafe"] = "SUPERUSER"
    found = check.evaluate(facts(roles=roles))
    assert "role_unsafe_attributes" in codes(found)


@pytest.mark.parametrize(
    ("role", "memberships"),
    [
        (check.RUNTIME_ROLE, []),
        (check.RUNTIME_ROLE, ["tybot_vipbot", "tyslackai"]),
        (check.DEFINER_ROLE, ["tybot_vipbot"]),
        (check.GROUP_ROLE, ["tyslackai"]),
    ],
)
def test_an_unexpected_membership_is_reported(role, memberships):
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[role]["memberships"] = memberships
    assert "role_membership_unexpected" in codes(check.evaluate(facts(roles=roles)))


def test_a_stranger_inside_the_privilege_group_is_reported():
    """**양방향**으로 안 읽으면 이게 안 보인다.

    낯선 역할이 `tybot_vipbot` 의 멤버가 되면 그 역할도 창구를 실행한다.
    「우리 역할이 누구의 멤버인가」 만 묻는 점검은 끝까지 초록이다.
    """
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[check.GROUP_ROLE]["members"] = [
        (check.RUNTIME_ROLE, False),
        ("tyslackai", False),
    ]
    found = check.evaluate(facts(roles=roles))
    assert codes(found) == ["role_membership_unexpected"]
    assert "tyslackai" in found[0].detail


def test_a_member_of_the_definer_role_is_reported():
    """definer 는 창구의 **소유자**다. 멤버가 생기면 그 역할이 곧 definer 다."""
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[check.DEFINER_ROLE]["members"] = [(check.RUNTIME_ROLE, False)]
    found = check.evaluate(facts(roles=roles))
    assert codes(found) == ["role_membership_unexpected"]
    assert check.DEFINER_ROLE in found[0].detail


def test_the_definer_may_not_inherit_anything_either():
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[check.DEFINER_ROLE]["memberships"] = [("tyslackai", False)]
    assert "role_membership_unexpected" in codes(check.evaluate(facts(roles=roles)))


@pytest.mark.parametrize(
    ("role", "direction"),
    [
        (check.RUNTIME_ROLE, "memberships"),
        (check.GROUP_ROLE, "members"),
    ],
)
def test_admin_option_anywhere_is_reported(role, direction):
    """ADMIN OPTION 이 있으면 그 역할이 **멤버십을 남에게 준다.**"""
    roles = {name: dict(value) for name, value in facts().roles.items()}
    edges = list(roles[role][direction])
    roles[role][direction] = [(edges[0][0], True)]
    found = check.evaluate(facts(roles=roles))
    assert codes(found) == ["role_admin_option"]


def test_the_group_must_have_exactly_the_runtime_as_its_member():
    roles = {name: dict(value) for name, value in facts().roles.items()}
    roles[check.GROUP_ROLE]["members"] = []
    assert "role_membership_unexpected" in codes(check.evaluate(facts(roles=roles)))


def test_the_role_query_reads_membership_in_both_directions():
    """질의가 한쪽만 보면 위 시험들은 **사실을 못 받는다.**"""
    source_text = SCRIPT.read_text(encoding="utf-8")
    query = source_text[source_text.index("FROM pg_auth_members") :][:400]
    assert "parent.rolname = ANY" in query and "child.rolname = ANY" in query
    assert "a.admin_option" in source_text


def test_the_public_execute_check_never_asks_pg_roles_for_public():
    """PUBLIC 은 `pg_roles` 의 행이 아니다.

    `has_function_privilege('public', …)` 는 `role "public" does not exist` 로
    터진다. 터지지 않는다면 **엉뚱한 역할**을 물어본 것이고, 그 점검은 PUBLIC 이
    열려 있어도 조용하다. ACL 을 직접 펴서 grantee=0 을 본다.
    """
    source_text = SCRIPT.read_text(encoding="utf-8")
    assert "has_function_privilege('public'" not in source_text
    assert 'has_function_privilege("public"' not in source_text
    assert "acldefault('f', p.proowner)" in source_text
    assert "acl.grantee = 0" in source_text
    assert "acl.privilege_type = 'EXECUTE'" in source_text


@pytest.mark.parametrize("signature", [check.CONFIG_FN, check.ACCESS_FN])
def test_a_missing_window_is_reported(signature):
    functions = dict(facts().functions)
    functions.pop(signature)
    assert "function_missing" in codes(check.evaluate(facts(functions=functions)))


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("owner", "tyslackai", "function_owner_wrong"),
        ("runtime_execute", False, "function_not_executable"),
        ("public_execute", True, "function_public_executable"),
    ],
)
def test_a_window_with_the_wrong_grants_is_reported(field, value, expected):
    functions = {name: dict(item) for name, item in facts().functions.items()}
    functions[check.CONFIG_FN][field] = value
    assert expected in codes(check.evaluate(facts(functions=functions)))


@pytest.mark.parametrize("table", check.BASE_TABLES)
def test_a_base_table_the_runtime_can_read_is_reported(table):
    tables = dict(facts().base_tables)
    tables[table] = True
    found = check.evaluate(facts(base_tables=tables))
    assert "base_table_readable" in codes(found)
    assert any(table in str(item) for item in found)


# --- 3. 연결 1건 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        ({"workspace": "pilot", "rows": 0}, "connection_missing"),
        ({"workspace": "pilot", "rows": 2, "complete": True}, "connection_ambiguous"),
        ({"workspace": "pilot", "rows": 1, "complete": False}, "connection_incomplete"),
    ],
)
def test_the_connection_must_be_exactly_one_complete_row(connection, expected):
    assert codes(check.evaluate(facts(connection=connection))) == [expected]


# --- 4. 자격증명 세 개 ---------------------------------------------------------


def test_a_missing_credential_is_reported():
    found = check.evaluate(
        facts(credentials=({"name": "tybot-workspace-secret-key", "present": False},))
    )
    assert codes(found) == ["credential_missing"]


def test_a_world_readable_credential_is_reported():
    found = check.evaluate(
        facts(
            credentials=(
                {
                    "name": "tybot-vipbot-database-url",
                    "present": True,
                    "mode_open": "0o644",
                },
            )
        )
    )
    assert codes(found) == ["credential_mode_open"]


def test_credential_facts_read_the_paths_the_unit_declares(tmp_path):
    dsn = tmp_path / "tybot-vipbot-database-url"
    dsn.write_text(DSN, encoding="utf-8")
    os.chmod(dsn, 0o600)
    unit = {
        "credential_paths": {
            "tybot-vipbot-database-url": str(dsn),
            "tybot-workspace-secret-key": str(tmp_path / "absent.key"),
        }
    }
    got = {item["name"]: item for item in check.credential_facts(unit)}
    assert got["tybot-vipbot-database-url"]["present"] is True
    assert got["tybot-vipbot-database-url"]["invalid"] is False
    assert got["tybot-workspace-secret-key"]["present"] is False


def test_a_malformed_credential_is_invalid_without_echoing_it(tmp_path):
    dsn = tmp_path / "tybot-vipbot-database-url"
    dsn.write_text("postgresql://tyslackai:pw@/tyslackai", encoding="utf-8")
    os.chmod(dsn, 0o600)
    unit = {"credential_paths": {"tybot-vipbot-database-url": str(dsn)}}
    got = check.credential_facts(unit)[0]
    assert got["invalid"] is True
    assert "tyslackai:pw" not in repr(got)


@pytest.mark.skipif(os.name != "posix", reason="파일 모드 비트는 posix 에서만 본다")
def test_an_open_mode_is_detected(tmp_path):
    dsn = tmp_path / "tybot-vipbot-database-url"
    dsn.write_text(DSN, encoding="utf-8")
    os.chmod(dsn, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    unit = {"credential_paths": {"tybot-vipbot-database-url": str(dsn)}}
    assert check.credential_facts(unit)[0].get("mode_open")


# --- 5. archive·설정 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("archive", "expected"),
    [
        ({"configured": False}, "archive_unconfigured"),
        ({"configured": True, "readable": False, "path": "/nope"}, "archive_unreadable"),
    ],
)
def test_the_archive_path_must_be_absolute_and_present(archive, expected):
    assert codes(check.evaluate(facts(archive=archive))) == [expected]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"workspace": "", "budget": "ok"}, "workspace_unconfigured"),
        ({"workspace": "pilot", "budget": "missing"}, "budget_unconfigured"),
        ({"workspace": "pilot", "budget": "invalid"}, "budget_invalid"),
        (
            {"workspace": "pilot", "budget": "ok", "model_not_approved": True},
            "model_not_approved",
        ),
    ],
)
def test_a_missing_or_unsafe_setting_is_reported(settings, expected):
    assert expected in codes(check.evaluate(facts(settings=settings)))


def test_settings_facts_read_only_vipbot_names(tmp_path):
    settings, archive = check.settings_facts(
        {
            "VIPBOT_WORKSPACE": "pilot",
            "VIPBOT_ARCHIVE_DIR": str(tmp_path),
            "VIPBOT_DAILY_LIMIT_USD": "12.50",
            "ARCHIVE_DIR": "/shared/archive",
            "DATABASE_URL": "postgresql://tyslackai@/tyslackai",
        }
    )
    assert settings == {
        "workspace": "pilot",
        "budget": "ok",
        "model_not_approved": False,
        "state_directory_missing": False,
    }
    assert archive["configured"] and archive["readable"]
    assert archive["path"] == str(tmp_path)


def test_a_shared_archive_never_fills_a_missing_vipbot_one():
    settings, archive = check.settings_facts({"ARCHIVE_DIR": "/shared/archive"})
    assert archive["configured"] is False
    assert settings["workspace"] == ""


@pytest.mark.parametrize("value", ["0", "-1", "nan", "1_0", "+10", ".5"])
def test_an_unusable_budget_is_invalid_not_ok(value):
    settings, _archive = check.settings_facts({"VIPBOT_DAILY_LIMIT_USD": value})
    assert settings["budget"] == "invalid"


# --- 6. 유닛의 공용 env 미사용 -------------------------------------------------


def test_the_real_unit_declares_the_three_credentials_and_no_shared_env():
    got = check.unit_facts(UNIT)
    assert got["present"] is True
    assert got["shared_env"] == ()
    assert set(got["credentials"]) == {
        "tybot-vipbot-database-url",
        "tybot-workspace-secret-key",
        "tybot-vipbot-anthropic-api-key",
    }


def test_the_real_unit_is_still_not_enablable():
    assert check.unit_facts(UNIT)["enablable"] is False


def test_the_real_unit_points_at_the_settings_file_and_hardcodes_nothing():
    """값은 유닛이 아니라 **전용 설정 파일**에 있다.

    유닛에 박으면 서버마다 다른 값을 쓰는 순간 저장소의 유닛과 서버의 유닛이
    갈리고, 그 갈림은 아무 데도 안 보인다.
    """
    got = check.unit_facts(UNIT)
    assert got["settings_file"] is True
    assert got["environment_files"] == (check.SETTINGS_FILE,)
    assert got["hardcoded_settings"] == ()
    assert got["shared_env"] == ()
    assert check.evaluate(facts(unit=got)) == []


def test_a_unit_that_hardcodes_a_setting_is_reported(tmp_path):
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text(
        "[Service]\n"
        f"EnvironmentFile={check.SETTINGS_FILE}\n"
        "Environment=VIPBOT_WORKSPACE=pilot\n"
        "Environment=VIPBOT_DAILY_LIMIT_USD=5\n",
        encoding="utf-8",
    )
    got = check.unit_facts(unit)
    assert set(got["hardcoded_settings"]) == {
        "VIPBOT_WORKSPACE",
        "VIPBOT_DAILY_LIMIT_USD",
    }
    assert codes(check.evaluate(facts(unit=got))) == ["unit_settings_hardcoded"] * 2


def test_a_unit_without_the_settings_file_is_reported(tmp_path):
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text("[Service]\nUser=tybot\n", encoding="utf-8")
    got = check.unit_facts(unit)
    assert got["settings_file"] is False
    assert codes(check.evaluate(facts(unit=got))) == ["unit_settings_missing"]


@pytest.mark.parametrize("name", check.SHARED_ENV)
def test_a_unit_that_pulls_in_a_shared_setting_is_reported(tmp_path, name):
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text(
        f"[Service]\nEnvironment={name}=/shared/value\n", encoding="utf-8"
    )
    got = check.unit_facts(unit)
    assert name in got["shared_env"]
    assert "unit_shared_env" in codes(check.evaluate(facts(unit=got)))


def test_the_shared_env_file_is_still_forbidden(tmp_path):
    """`EnvironmentFile=` 자체는 금지가 아니다. **어느 파일이냐**가 전부다."""
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text(
        "[Service]\n"
        "EnvironmentFile=/etc/tybot/tybot.env\n"
        f"EnvironmentFile={check.SETTINGS_FILE}\n",
        encoding="utf-8",
    )
    got = check.unit_facts(unit)
    assert "EnvironmentFile=/etc/tybot/tybot.env" in got["shared_env"]
    assert got["settings_file"] is True
    assert "unit_shared_env" in codes(check.evaluate(facts(unit=got)))


@pytest.mark.parametrize("prefix", ["", "-"])
def test_only_the_dedicated_settings_file_is_allowed(tmp_path, prefix):
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text(
        f"[Service]\nEnvironmentFile={prefix}/etc/tybot/other.conf\n", encoding="utf-8"
    )
    got = check.unit_facts(unit)
    assert got["shared_env"] == ("EnvironmentFile=/etc/tybot/other.conf",)
    assert got["settings_file"] is False


def test_a_comment_mentioning_a_shared_name_is_not_a_finding(tmp_path):
    unit = tmp_path / "tybot-vipbot.service"
    unit.write_text(
        "[Service]\n# DATABASE_URL 을 읽지 않는다\nUser=tybot\n", encoding="utf-8"
    )
    assert check.unit_facts(unit)["shared_env"] == ()


def test_a_missing_unit_is_reported(tmp_path):
    got = check.unit_facts(tmp_path / "absent.service")
    assert got["present"] is False
    assert codes(check.evaluate(facts(unit=got))) == ["unit_missing"]


# --- 6b. 전용 설정 파일 --------------------------------------------------------


EXAMPLE = ROOT / "deploy" / "tybot-vipbot.conf.example"


def conf(tmp_path, body: str):
    path = tmp_path / "tybot-vipbot.conf"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def root_owned_conf_facts(tmp_path, body: str) -> dict:
    """Exercise config parsing without conflating it with the owner check."""
    got = check.settings_file_facts(conf(tmp_path, body))
    got.pop("owner", None)
    return got


GOOD = (
    "# 주석\n"
    "VIPBOT_WORKSPACE=pilot\n"
    "VIPBOT_ARCHIVE_DIR=/var/lib/tybot/archive\n"
    "VIPBOT_DAILY_LIMIT_USD=5.00\n"
)


def test_a_complete_settings_file_has_nothing_to_report(tmp_path):
    got = root_owned_conf_facts(tmp_path, GOOD)
    assert got["present"] is True
    assert got["missing"] == () and got["forbidden"] == ()
    assert check.evaluate(facts(settings_file=got)) == []


def test_the_optional_model_key_is_allowed(tmp_path):
    got = root_owned_conf_facts(tmp_path, GOOD + "VIPBOT_MODEL=claude-opus-5-5\n")
    assert got["forbidden"] == ()


def test_the_shipped_example_passes_its_own_check():
    got = check.settings_file_facts(EXAMPLE)
    assert got["missing"] == () and got["forbidden"] == ()
    assert got.get("invalid_lines") == 0


def test_a_missing_settings_file_is_reported(tmp_path):
    got = check.settings_file_facts(tmp_path / "absent.conf")
    assert got["present"] is False
    assert codes(check.evaluate(facts(settings_file=got))) == ["settings_file_missing"]


@pytest.mark.parametrize("key", check.REQUIRED_SETTINGS)
def test_a_missing_required_key_is_reported_by_name(tmp_path, key):
    body = "\n".join(line for line in GOOD.splitlines() if not line.startswith(key))
    got = root_owned_conf_facts(tmp_path, body + "\n")
    assert got["missing"] == (key,)
    found = check.evaluate(facts(settings_file=got))
    assert codes(found) == ["settings_key_missing"]
    assert found[0].detail == key


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "WORKSPACE_SECRET_KEY",
        "ARCHIVE_DIR",
        "TYBOT_ENV_FILE",
        "VIPBOT_TYPO",
        "PATH",
    ],
)
def test_any_key_outside_the_allowlist_is_refused(tmp_path, key):
    """허용 목록 방식이다. **모르는 키는 전부 거절**한다.

    금지 목록을 쓰면 내일 생기는 비밀 이름을 못 막는다. `EnvironmentFile=` 은
    이 파일의 모든 줄을 환경변수로 만들므로, 모르는 줄 하나가 곧 프로세스의
    환경 하나다.
    """
    got = root_owned_conf_facts(tmp_path, GOOD + f"{key}=값\n")
    assert got["forbidden"] == (key,)
    found = check.evaluate(facts(settings_file=got))
    assert codes(found) == ["settings_key_forbidden"]
    assert found[0].detail == key


def test_a_forbidden_key_is_reported_by_name_without_its_value(tmp_path):
    secret = "postgresql://tyslackai:SUPERSECRET@/tyslackai"
    got = root_owned_conf_facts(tmp_path, GOOD + f"DATABASE_URL={secret}\n")
    blob = repr(got) + "".join(str(item) for item in check.evaluate(facts(settings_file=got)))
    assert "SUPERSECRET" not in blob and secret not in blob
    assert "DATABASE_URL" in blob


def test_the_settings_file_values_never_enter_the_facts(tmp_path):
    got = root_owned_conf_facts(tmp_path, GOOD)
    assert "pilot" not in repr(got)
    assert "/var/lib/tybot/archive" not in repr(got)
    assert "5.00" not in repr(got)


def test_a_line_that_is_not_a_key_value_pair_is_counted(tmp_path):
    got = root_owned_conf_facts(tmp_path, GOOD + "잘못된줄\n= 값만\n")
    assert got["invalid_lines"] == 2
    assert "settings_file_invalid" in codes(check.evaluate(facts(settings_file=got)))


@pytest.mark.skipif(os.name != "posix", reason="파일 모드 비트는 posix 에서만 본다")
@pytest.mark.parametrize("mode", [0o666, 0o644, 0o660, 0o604])
def test_a_loosely_moded_settings_file_is_reported(tmp_path, mode):
    """0640 을 기대한다. other 가 읽기만 해도 근거 범위가 밖으로 보인다."""
    path = conf(tmp_path, GOOD)
    os.chmod(path, mode)
    got = check.settings_file_facts(path)
    assert got.get("mode_open")
    assert "settings_file_mode_open" in codes(check.evaluate(facts(settings_file=got)))


@pytest.mark.skipif(os.name != "posix", reason="소유자는 posix 에서만 본다")
@pytest.mark.parametrize("mode", [0o640, 0o600, 0o400])
def test_a_file_the_service_account_owns_is_reported_even_at_0600(tmp_path, mode):
    """**쓰기 비트가 없어도** 소유자면 `chmod` 한 줄로 바꿀 수 있다.

    그러면 봇이 자기 근거 범위와 예산을 스스로 고치게 된다. 그건 권한이 아니다.
    시험을 돌리는 계정은 root 가 아니므로, 이 파일은 그 자체로 「서비스 계정이
    소유한 설정」 과 같은 모양이다.
    """
    if os.getuid() == 0:
        pytest.skip("root 로 돌면 소유자 검사를 재현할 수 없다")
    path = conf(tmp_path, GOOD)
    os.chmod(path, mode)
    got = check.settings_file_facts(path)
    assert got.get("owner") == os.getuid()
    assert "settings_file_owner" in codes(check.evaluate(facts(settings_file=got)))


def test_the_mode_mask_refuses_group_write_and_every_other_bit():
    """`0o022`(쓰기만) 로 두면 **0644 가 통과한다.** 기대는 0640 이다.

    posix 가 아닌 곳에서는 실제 `chmod` 시험이 건너뛰므로, 마스크 자체를
    여기서 고정한다 — 개발 PC 에서도 이 어긋남이 보여야 한다.
    """
    assert check.MODE_FORBIDDEN_BITS == 0o027
    for mode in (0o644, 0o660, 0o604, 0o666):
        assert mode & check.MODE_FORBIDDEN_BITS, f"{oct(mode)} 가 통과한다"
    for mode in (0o640, 0o600, 0o400):
        assert not mode & check.MODE_FORBIDDEN_BITS, f"{oct(mode)} 를 막는다"


def test_the_owner_check_looks_at_the_uid_not_only_the_mode():
    """모드만 보면 `tybot` 소유 0600 이 통과한다 — 그게 이 결함이었다."""
    source_text = SCRIPT.read_text(encoding="utf-8")
    assert "info.st_uid != 0" in source_text
    assert "settings_file_owner" in check.FINDING_CODES
    got = check.evaluate(facts(settings_file={"present": True, "owner": 1001}))
    assert codes(got) == ["settings_file_owner"]
    assert "1001" in got[0].detail


def test_allowed_values_reads_only_the_four_keys(tmp_path):
    path = conf(tmp_path, GOOD + "DATABASE_URL=postgresql://x\nVIPBOT_MODEL=m\n")
    got = check.allowed_values(path)
    assert set(got) == {
        "VIPBOT_WORKSPACE",
        "VIPBOT_ARCHIVE_DIR",
        "VIPBOT_DAILY_LIMIT_USD",
        "VIPBOT_MODEL",
    }
    assert "DATABASE_URL" not in got


def test_the_check_reads_its_settings_from_the_file_not_the_shell(tmp_path):
    path = conf(tmp_path, GOOD)
    got = check.gather(unit_path=UNIT, skip_db=True, environ={}, settings_path=str(path))
    assert got.settings["workspace"] == "pilot"
    assert got.settings["budget"] == "ok"
    assert got.settings_file["missing"] == ()


# --- 7. 읽기 전용이라는 계약 ---------------------------------------------------


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def code_only() -> str:
    """주석과 docstring 을 걷어낸 코드.

    머리말이 **무엇을 안 하는지**를 설명하므로, 그대로 훑으면 설명문이 금지어
    검사에 걸린다. 서버에서 도는 것은 코드뿐이다.
    """
    import ast

    tree = ast.parse(source())
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.mark.parametrize(
    "forbidden",
    ["systemctl", "subprocess", "os.system", "auth_test", "chmod(", "unlink("],
)
def test_the_check_never_changes_the_server(forbidden):
    assert forbidden not in code_only()


@pytest.mark.parametrize(
    "statement", ["GRANT ", "REVOKE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "DROP "]
)
def test_the_check_runs_no_write_statement(statement):
    assert statement not in code_only()


def test_the_check_never_opens_a_slack_client():
    assert "slack_bolt" not in source() and "slack_sdk" not in source()


def test_skipping_the_database_silences_only_the_database_findings():
    broken = facts(roles={}, functions={}, base_tables={}, connection={})
    offline = check.Facts(
        credentials=broken.credentials,
        archive=broken.archive,
        settings=broken.settings,
        unit=broken.unit,
        database_checked=False,
    )
    assert check.evaluate(offline) == []


def test_an_unreachable_database_is_reported_as_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(
        check, "database_facts", lambda _ws: (_ for _ in ()).throw(RuntimeError("down"))
    )
    got = check.gather(unit_path=UNIT, skip_db=False, environ={})
    assert got.database_checked is False
    assert got.database_error == "RuntimeError"


def _real_credentials(tmp_path) -> tuple[dict, dict]:
    """값이 **실제로 들어 있는** 자격증명 세 개. 어디로도 새면 안 된다."""
    from cryptography.fernet import Fernet

    fernet = Fernet.generate_key().decode("ascii")
    files = {
        "tybot-vipbot-database-url": (tmp_path / "tybot-vipbot-database-url", DSN),
        "tybot-vipbot-anthropic-api-key": (
            tmp_path / "tybot-vipbot-anthropic-api-key",
            KEY,
        ),
        "tybot-workspace-secret-key": (tmp_path / "workspace-secret.key", fernet),
    }
    for path, value in files.values():
        path.write_text(value, encoding="utf-8")
        os.chmod(path, 0o600)
    unit = {
        "present": True,
        "path": str(UNIT),
        "shared_env": (),
        "credentials": tuple(files),
        "missing_settings": (),
        "credential_paths": {name: str(path) for name, (path, _) in files.items()},
    }
    return unit, {name: value for name, (_, value) in files.items()}


def test_gathering_the_credential_facts_prints_nothing_at_all(
    tmp_path, capsys, caplog
):
    """값을 **쥐었다가 버리는** 자리다. 한 줄이라도 찍으면 그게 유출이다.

    `_credential_shape_ok` 는 DSN·API 키를 해석기에 통과시킨다. 그 반환값을
    실수로 출력하면 점검 결과가 그대로 티켓에 붙는다. 그래서 「비밀이 없다」 가
    아니라 **「아무것도 안 찍는다」** 를 고정한다.
    """
    import logging

    unit, secrets = _real_credentials(tmp_path)
    capsys.readouterr()
    with caplog.at_level(logging.DEBUG):
        got = {item["name"]: item for item in check.credential_facts(unit)}

    captured = capsys.readouterr()
    noise = captured.out + captured.err + caplog.text
    assert noise.strip() == "", f"점검이 무언가를 찍었다: {noise[:120]!r}"
    for value in secrets.values():
        assert value not in repr(got)
    assert all(item["invalid"] is False for item in got.values())


def test_a_full_run_never_writes_a_secret_to_stdout(tmp_path, capsys, monkeypatch):
    """끝에서 끝까지. 유닛·자격증명·설정을 다 주고 출력 전체를 뒤진다."""
    unit, secrets = _real_credentials(tmp_path)
    monkeypatch.setattr(check, "unit_facts", lambda _path: unit)
    monkeypatch.setattr(
        check,
        "settings_facts",
        lambda _environ=None: (
            {"workspace": "pilot", "budget": "ok", "model_not_approved": False},
            {"configured": True, "readable": True, "path": str(tmp_path)},
        ),
    )
    capsys.readouterr()

    check.main(["--skip-db"])
    plain = capsys.readouterr().out
    check.main(["--skip-db", "--json"])
    machine = capsys.readouterr().out

    for value in secrets.values():
        assert value not in plain and value not in machine
    assert "pw@" not in plain and "pw@" not in machine


def test_checking_a_credential_leaves_the_process_environment_as_it_was(tmp_path):
    """DSN 해석기는 `os.environ` 만 본다. 빌렸으면 **되돌려 놔야** 한다.

    안 되돌리면 점검이 끝난 뒤에도 그 변수가 남고, 같은 셸에서 이어 치는 명령이
    엉뚱한 파일을 DSN 으로 읽는다.
    """
    unit, _secrets = _real_credentials(tmp_path)
    before = dict(os.environ)

    check.credential_facts(unit)

    assert dict(os.environ) == before


def test_checking_a_credential_never_touches_the_file(tmp_path):
    unit, _secrets = _real_credentials(tmp_path)
    paths = [Path(path) for path in unit["credential_paths"].values()]
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]

    check.credential_facts(unit)

    assert [
        (path.read_bytes(), path.stat().st_mtime_ns) for path in paths
    ] == before


def test_the_report_counts_the_credentials_instead_of_printing_them(tmp_path):
    """구조를 통째로 찍으면, 그 칸에 값이 생기는 날 함께 샌다."""
    unit, _secrets = _real_credentials(tmp_path)
    got = check.Facts(
        credentials=check.credential_facts(unit),
        archive={"configured": True, "readable": True},
        settings={"workspace": "pilot", "budget": "ok"},
        unit=unit,
        database_checked=False,
    )
    report = check.render(got, check.evaluate(got))

    assert "자격증명    : 3개 확인" in report
    for structure in ("{'name'", "'present'", "'invalid'", "credential_paths"):
        assert structure not in report


def test_the_rendered_report_carries_no_secret(tmp_path):
    dsn = tmp_path / "tybot-vipbot-database-url"
    dsn.write_text(DSN, encoding="utf-8")
    os.chmod(dsn, 0o600)
    unit = check.unit_facts(UNIT)
    unit["credential_paths"] = {"tybot-vipbot-database-url": str(dsn)}
    got = check.Facts(
        credentials=check.credential_facts(unit),
        archive={"configured": False},
        settings={"workspace": "", "budget": "missing"},
        unit=unit,
        database_checked=False,
    )
    report = check.render(got, check.evaluate(got))
    assert DSN not in report and "pw@" not in report
    assert "tybot-vipbot-database-url" in report or "VIPBOT_WORKSPACE" in report


def test_the_exit_code_separates_ready_from_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(check, "gather", lambda **_kwargs: facts())
    assert check.main(["--skip-db"]) == check.EXIT_OK
    monkeypatch.setattr(
        check, "gather", lambda **_kwargs: facts(archive={"configured": False})
    )
    assert check.main(["--skip-db"]) == check.EXIT_PROBLEM
    assert "archive_unconfigured" in capsys.readouterr().out


def test_the_json_output_is_one_machine_readable_line(monkeypatch, capsys):
    import json

    monkeypatch.setattr(
        check, "gather", lambda **_kwargs: facts(archive={"configured": False})
    )
    assert check.main(["--skip-db", "--json"]) == check.EXIT_PROBLEM
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["findings"] == [
        {"code": "archive_unconfigured", "detail": "VIPBOT_ARCHIVE_DIR"}
    ]
