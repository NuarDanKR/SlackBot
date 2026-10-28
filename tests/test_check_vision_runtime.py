"""Vision 접속 계정 대조기. **판정만** 건다 — DB 없이 돈다."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    """`scripts/` 는 패키지가 아니다. 파일에서 직접 연다."""
    path = ROOT / "scripts" / "check_vision_runtime.py"
    spec = importlib.util.spec_from_file_location("check_vision_runtime", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def runtime(**over) -> dict:
    """계약에 맞는 접속 계정. 시험마다 한 칸씩 틀어 본다."""
    base = {
        "login": True, "inherit": True, "super": False, "createdb": False,
        "createrole": False, "replication": False, "bypassrls": False,
    }
    return base | over


def inherited(missing=()) -> dict:
    """계약이 요구하는 표·권한을 전부 상속한 상태. `missing` 만 끊는다."""
    said = {
        (table, privilege): True
        for table, privileges in check.VISION_REQUIRED.items()
        for privilege in privileges
    }
    return said | dict.fromkeys(missing, False)


def facts(**over) -> check.Facts:
    base = {
        "group_exists": True,
        "group_login": False,
        "runtime": runtime(),
        "memberships": ((check.VISION_ROLE, False),),
        "foreign_grants": (),
        "inherited": inherited(),
        "clearance_table_update": False,
        "clearance_update_columns": tuple(
            sorted(check.VISION_COLUMN_REQUIRED[check.CLEARANCE]["UPDATE"])
        ),
        "public_grants": 0,
    }
    return check.Facts(**(base | over))


def judge(said: check.Facts, *, require_login: bool = False):
    return check.evaluate(said, require_login=require_login)


# --- 맞는 모양 ---------------------------------------------------------------


def test_a_correct_runtime_is_ready():
    problems, state = judge(facts())
    assert (problems, state) == ([], "ready")


def test_the_column_order_does_not_matter():
    columns = tuple(reversed(facts().clearance_update_columns))
    assert judge(facts(clearance_update_columns=columns))[0] == []


# --- 준비 상태 ↔ 실패 --------------------------------------------------------


def test_no_login_role_is_prepared_not_a_failure():
    """정책 OFF·worker 없음. 지금은 이것이 맞는 모양이다."""
    assert judge(facts(runtime=None)) == ([], "prepared")


def test_no_login_role_is_a_failure_when_activation_is_asked():
    problems, state = judge(facts(runtime=None), require_login=True)
    assert state == "blocked"
    assert "runtime_role_missing" in problems


def test_a_group_problem_is_reported_even_without_a_login_role():
    """계정이 없다고 넘어가면 그룹이 LOGIN 으로 바뀐 사고를 활성화 날까지 못 본다."""
    problems, state = judge(facts(runtime=None, group_login=True))
    assert state == "blocked"
    assert "group_role_can_login" in problems


def test_a_missing_group_is_reported():
    assert "group_role_missing" in judge(facts(group_exists=False))[0]


# --- 접속 계정 속성 ----------------------------------------------------------


def test_a_runtime_that_cannot_login_is_refused():
    assert "runtime_cannot_login" in judge(facts(runtime=runtime(login=False)))[0]


def test_a_runtime_that_does_not_inherit_is_refused():
    """NOINHERIT 면 권한이 「있는데 안 되는」 상태가 된다."""
    assert "runtime_does_not_inherit" in judge(facts(runtime=runtime(inherit=False)))[0]


@pytest.mark.parametrize("attribute", check.FORBIDDEN)
def test_every_forbidden_attribute_is_refused(attribute):
    problems = judge(facts(runtime=runtime(**{attribute: True})))[0]
    assert f"runtime_has_{attribute}" in problems


def test_the_forbidden_list_covers_the_dangerous_attributes():
    """목록이 줄면 조용히 통과한다. 무엇을 막기로 했는지 여기 박아 둔다."""
    assert set(check.FORBIDDEN) == {
        "super", "createdb", "createrole", "replication", "bypassrls",
    }


# --- 멤버십 ------------------------------------------------------------------


def test_an_extra_membership_is_refused():
    members = ((check.VISION_ROLE, False), ("tybot_archiver", False))
    assert "runtime_membership_mismatch" in judge(facts(memberships=members))[0]


def test_a_missing_membership_is_refused():
    assert "runtime_membership_mismatch" in judge(facts(memberships=()))[0]


def test_a_wrong_membership_is_refused():
    assert "runtime_membership_mismatch" in judge(
        facts(memberships=(("tyslackai", False),))
    )[0]


def test_admin_option_is_refused():
    """있으면 접속 계정이 그룹을 **남에게 줄 수 있다.**"""
    members = ((check.VISION_ROLE, True),)
    assert "runtime_membership_admin_option" in judge(facts(memberships=members))[0]


def test_an_adopted_over_privileged_role_reports_every_reason_at_once():
    """손으로 만들어 둔 역할을 넘겨받는 상황이다. **하나만 보고하면** 고치고
    다시 와서 또 막힌다 — 걸린 것을 한 번에 다 말한다."""
    problems, state = judge(facts(
        runtime=runtime(super=True, createrole=True),
        memberships=((check.VISION_ROLE, True), ("tybot_archiver", False)),
    ))
    assert state == "blocked"
    assert {"runtime_has_super", "runtime_has_createrole",
            "runtime_membership_mismatch",
            "runtime_membership_admin_option"} <= set(problems)


def test_a_membership_problem_is_reported_even_while_login_is_still_off():
    """설치 스크립트가 LOGIN 을 켜기 **전에** 거절하는 것과 같은 축이다.
    로그인할 수 없다고 멤버십 문제를 넘기면, 켜는 순간 과권한 로그인이 된다."""
    problems, state = judge(facts(runtime=runtime(login=False),
                                  memberships=((check.VISION_ROLE, True),)))
    assert state == "blocked"
    assert "runtime_membership_admin_option" in problems
    assert "runtime_cannot_login" in problems


def test_ready_is_never_returned_with_a_problem():
    """상태와 목록이 어긋나면 운영자는 상태만 본다."""
    for broken in (
        facts(runtime=runtime(bypassrls=True)),
        facts(memberships=()),
        facts(public_grants=3),
        facts(group_login=True),
        facts(clearance_table_update=True),
    ):
        problems, state = judge(broken)
        assert problems and state == "blocked"


# --- Master/Archiver · PUBLIC ------------------------------------------------


def test_a_master_grant_is_reported_with_its_coordinates():
    grants = (("tyslackai", "archiver_image_description_job", "SELECT"),)
    problems = judge(facts(foreign_grants=grants))[0]
    assert "foreign_grant:tyslackai:archiver_image_description_job:select" in problems


def test_a_public_grant_is_reported():
    assert "public_grant_present" in judge(facts(public_grants=1))[0]


def test_the_master_role_list_does_not_include_the_vision_roles():
    """Vision 역할이 목록에 섞이면 정상 권한이 문제로 보고된다."""
    assert check.VISION_ROLE not in check.MASTER_ROLES
    assert check.RUNTIME_ROLE not in check.MASTER_ROLES


# --- 상속된 권한 -------------------------------------------------------------


@pytest.mark.parametrize(
    "table,privilege",
    sorted(
        (table, privilege)
        for table, privileges in check.VISION_REQUIRED.items()
        for privilege in privileges
    ),
)
def test_every_required_privilege_must_actually_be_inherited(table, privilege):
    problems = judge(facts(inherited=inherited([(table, privilege)])))[0]
    assert f"not_inherited:{table}:{privilege.lower()}" in problems


def test_the_required_contract_comes_from_the_drift_checker():
    """여기 베껴 적으면 한쪽만 고쳐지는 날이 온다."""
    import importlib

    drift = importlib.import_module("check_schema_drift")
    assert check.VISION_REQUIRED is drift.VISION_REQUIRED
    assert check.VISION_COLUMN_REQUIRED is drift.VISION_COLUMN_REQUIRED
    assert check.VISION_ROLE == drift.VISION_ROLE


# --- clearance 철회 칸 -------------------------------------------------------


def test_a_table_wide_clearance_update_is_refused():
    """표 전체 UPDATE 면 런타임이 검토된 범위를 다시 쓸 수 있다."""
    assert "clearance_table_update_granted" in judge(
        facts(clearance_table_update=True)
    )[0]


def test_one_extra_updatable_column_is_refused():
    columns = (*facts().clearance_update_columns, "workspace")
    assert "clearance_update_columns_too_wide" in judge(
        facts(clearance_update_columns=columns)
    )[0]


def test_a_missing_revoke_column_is_refused():
    columns = facts().clearance_update_columns[:-1]
    assert "clearance_update_columns_missing" in judge(
        facts(clearance_update_columns=columns)
    )[0]


def test_no_updatable_column_is_refused():
    assert "clearance_update_columns_missing" in judge(
        facts(clearance_update_columns=())
    )[0]


def test_the_revoke_columns_are_exactly_the_three():
    assert check.VISION_COLUMN_REQUIRED[check.CLEARANCE]["UPDATE"] == {
        "revoked_at", "revoked_by", "revoke_reason",
    }


# --- 보고 --------------------------------------------------------------------


def test_the_prepared_report_does_not_claim_success(capsys):
    assert check.report([], "prepared", require_login=False) == check.EXIT_OK
    said = capsys.readouterr().out
    assert "준비 상태" in said
    assert "✅" not in said


def test_the_ready_report_does_not_claim_the_worker_runs(capsys):
    assert check.report([], "ready", require_login=False) == check.EXIT_OK
    said = capsys.readouterr().out
    assert "worker 가 도는 것은 아닙니다" in said


def test_problems_exit_non_zero_and_name_every_code(capsys):
    codes = ["runtime_has_super", "public_grant_present"]
    assert check.report(codes, "blocked", require_login=True) == check.EXIT_PROBLEM
    said = capsys.readouterr().out
    assert all(code in said for code in codes)
    assert "활성화할 수 없습니다" in said
    assert "Master/수집기 권한을 넓혀 해결하지 마세요" in said


# --- 읽기 전용 ---------------------------------------------------------------


def test_the_script_only_reads():
    source = (ROOT / "scripts" / "check_vision_runtime.py").read_text(encoding="utf-8")
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    body = "\n".join(code)
    for word in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ROLE", "ALTER ROLE",
                 "GRANT ", "REVOKE ", "DROP "):
        assert word not in body.replace("'UPDATE'", "").replace('"UPDATE"', ""), word
    assert "SET TRANSACTION READ ONLY" in body
