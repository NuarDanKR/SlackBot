"""Vision 접속 계정 프로비저닝 스크립트. **계획 모드**와 금지 사항을 건다.

실제 역할 생성은 서버에서만 일어난다. 여기서 재는 것은 두 가지다 —
인자 없이 돌렸을 때 **아무것도 바꾸지 않는가**, 그리고 소스에
**넣지 않기로 한 것**이 정말 없는가.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "setup-tybot-vision-runtime.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")

GROUP = "tybot_vision"
RUNTIME = "tybot_vision_runtime"
ATTRS = (
    "LOGIN", "INHERIT", "NOSUPERUSER", "NOCREATEDB",
    "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS",
)

bash = shutil.which("bash")


def _bash_works(path):
    if not path:
        return False
    try:
        return subprocess.run(
            [path, "-c", "exit 0"],
            capture_output=True,
            timeout=5,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_bash = pytest.mark.skipif(
    not _bash_works(bash), reason="an executable Bash runtime is unavailable"
)


def run(*args, env=None):
    return subprocess.run(
        [bash, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env,
    )


# --- 계획 모드는 아무것도 바꾸지 않는다 --------------------------------------


@needs_bash
@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_the_default_is_a_plan_that_touches_nothing(args):
    """인자를 잊은 사람이 운영 DB 를 바꾸면 안 된다. 기본은 계획이다."""
    said = run(*args)
    assert said.returncode == 0, said.stderr
    assert "계획만" in said.stdout
    assert "DB 에 접속하지 않음" in said.stdout


@needs_bash
def test_the_plan_needs_neither_root_nor_psql():
    """root 도 psql 도 없는 개발 PC 에서 돌아야 시험이 성립한다.

    실행으로 확인하고(개발 PC 에는 psql 도 sudo 도 없다), 그것이 우연이 아님은
    아래 `test_the_plan_block_never_touches_the_database` 가 소스로 못 박는다.
    """
    said = run("--dry-run")
    assert said.returncode == 0, said.stderr


def _plan_block() -> str:
    """계획 모드가 `exit 0` 으로 끝나기까지의 본문."""
    start = SOURCE.index('if [[ "$MODE" == plan ]]')
    return SOURCE[start : SOURCE.index("\nfi\n", start)]


def test_the_plan_block_never_touches_the_database():
    body = _plan_block()
    for word in ("psql", "sudo -u postgres", "$EUID"):
        assert word not in body.replace("sudo $0", ""), word
    assert "exit 0" in body


@needs_bash
def test_the_plan_shows_the_exact_sql_it_would_run():
    said = run("--dry-run").stdout
    assert f"CREATE ROLE {RUNTIME}" in said
    assert f"ALTER ROLE {RUNTIME}" in said
    assert f"GRANT {GROUP} TO {RUNTIME}" in said


@needs_bash
@pytest.mark.parametrize("attribute", ATTRS)
def test_the_plan_names_every_attribute(attribute):
    assert attribute in run("--dry-run").stdout


@needs_bash
def test_the_plan_does_not_claim_the_worker_runs():
    """「LOGIN 계정 준비」 와 「worker 활성화」 는 다른 완료다."""
    said = run("--dry-run").stdout
    assert "worker 가 도는 것은 아니다" in said
    assert "정책은 OFF 그대로다" in said


@needs_bash
def test_the_target_can_be_named_and_is_echoed():
    env = os.environ | {"TYBOT_DB_NAME": "tybot_role_test", "TYBOT_DB_PORT": "6543"}
    said = run("--dry-run", env=env)
    assert "db=tybot_role_test port=6543" in said.stdout


@needs_bash
def test_an_unknown_argument_is_refused():
    said = run("--make-it-so")
    assert said.returncode == 2
    assert "알 수 없는 인자" in said.stderr


@needs_bash
def test_the_plan_prints_no_secret_shaped_value():
    said = run("--dry-run").stdout
    for word in ("password=", "PGPASSWORD"):
        assert word not in said, word
    # DSN 예시는 자리표시자뿐이다. 실제 값이 들어가면 안 된다.
    assert "<비밀번호>" in said
    assert ":<비밀번호>@" in said


@needs_bash
def test_the_plan_states_the_dsn_form_the_console_accepts():
    """키워드 형식이나 ?user= 로 덮어쓴 DSN 은 콘솔이 거절한다. 미리 말해 둔다."""
    said = run("--dry-run").stdout
    assert f"postgresql://{RUNTIME}:" in said
    assert "?user=" in said


# --- 기존 역할 사전 검토 -----------------------------------------------------


def _preflight() -> str:
    """사전 검토 블록의 **본문** — `--apply` 가 시작되기 전까지, 주석 제외.

    주석에는 「`ALTER ROLE` 을 먼저 켜면 안 된다」 같은 설명이 들어 있다.
    그것을 위반으로 세면 설명을 지워야 통과하는 시험이 된다.
    """
    start = SOURCE.index("기존 역할 사전 검토")
    block = SOURCE[start : SOURCE.index('if [[ "$MODE" == apply ]]', start)]
    return "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_existing_role_is_judged_before_anything_is_changed():
    """LOGIN 을 먼저 켜고 나중에 검증하면, 기존 비밀번호가 있는 역할은
    그 사이 **과한 권한으로 로그인할 수 있다.**"""
    preflight = SOURCE.index("기존 역할 사전 검토")
    apply_block = SOURCE.index('if [[ "$MODE" == apply ]]')
    assert preflight < apply_block
    # `ALTER ROLE … LOGIN` 이 실제로 도는 자리보다 앞이어야 한다.
    assert preflight < SOURCE.index('psql_run "$SQL_ENFORCE"')


def test_the_password_mode_is_behind_the_preflight():
    """과한 권한을 가진 역할에 비밀번호를 붙이는 것이 바로 그 노출이다."""
    assert SOURCE.index("기존 역할 사전 검토") < SOURCE.index('if [[ "$MODE" == password ]]')


def test_the_preflight_only_reads():
    block = _preflight()
    assert "psql_run" not in block
    for word in ("REVOKE", "ALTER ROLE", "GRANT ", "DROP ", "CREATE ROLE"):
        assert word not in block, word


def test_the_preflight_refuses_instead_of_repairing():
    """자동으로 떼거나 낮추면 남이 다른 용도로 만든 역할을 말없이 빼앗는다."""
    block = _preflight()
    assert "자동으로 떼지 않습니다" in block
    assert "넘겨받지 않습니다" in block
    assert block.count("exit 1") == 2


def test_the_preflight_refuses_an_extra_membership():
    block = _preflight()
    assert "pg_auth_members" in block
    assert '"$existing_members" != "$ROLE_GROUP:false"' in block


def test_the_preflight_refuses_an_admin_option():
    """ADMIN OPTION 이 있으면 접속 계정이 그룹을 남에게 줄 수 있다."""
    block = _preflight()
    assert "a.admin_option" in block
    # 기대값에 `:false` 가 붙어 있어야 ADMIN OPTION 이 걸린다.
    assert "$ROLE_GROUP:false" in block


@pytest.mark.parametrize("column", [
    "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls",
])
def test_the_preflight_looks_at_every_dangerous_attribute(column):
    assert column in _preflight()


def test_a_resumed_apply_is_not_refused_for_a_missing_membership():
    """CREATE 와 GRANT 사이에서 끊기면 멤버십이 0건이다. 그건 거절 사유가 아니다."""
    assert '-n "$existing_members"' in _preflight()


# --- 소스에 넣지 않기로 한 것 ------------------------------------------------


def _code() -> str:
    """주석을 뺀 본문. 설명에 적은 금지어가 위반으로 잡히면 안 된다."""
    return "\n".join(
        line for line in SOURCE.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_script_fails_fast():
    assert "set -euo pipefail" in SOURCE


def test_no_password_ever_reaches_the_command_line():
    """인자로 받으면 `ps` 와 shell 이력에 남는다."""
    body = _code()
    for word in ("PASSWORD '", 'PASSWORD "', "PGPASSWORD", "--password",
                 "ENCRYPTED PASSWORD", "read -s"):
        assert word not in body, word
    # 비밀번호 설정은 psql 의 대화형 메타명령으로만 한다.
    assert "\\\\password" in body or "\\password" in body


def test_nothing_is_granted_to_public():
    body = _code().upper()
    assert "TO PUBLIC" not in body
    assert "GRANT ALL" not in body


def test_master_and_collector_roles_are_never_granted():
    body = _code()
    for role in ("tyslackai", "tybot_archiver", "tybot_archiver_runtime"):
        assert f"GRANT {role}" not in body
        assert f"TO {role}" not in body


def test_the_runtime_membership_has_no_admin_option():
    """있으면 접속 계정이 그룹을 **남에게 줄 수 있다.**"""
    # 계획 문구에는 「ADMIN OPTION 없음」 이 나온다. 막는 것은 SQL 쪽이다.
    assert "WITH ADMIN OPTION" not in _code().upper()


def test_existing_membership_is_checked_before_login_is_enabled():
    body = _code()
    preflight = body.index('existing_members="$(psql_q')
    enable_login = body.index('psql_run "$SQL_ENFORCE"')
    assert preflight < enable_login
    assert '"$ROLE_GROUP:false"' in body
    assert "기존 멤버십이 계약과 다릅니다" in body


def test_the_group_stays_nologin():
    body = _code()
    assert f"CREATE ROLE {GROUP} " not in body
    assert f"ALTER ROLE {GROUP} " not in body
    # 그룹이 LOGIN 이면 중단한다.
    assert "rolcanlogin" in body


def test_no_database_is_created_or_dropped():
    # 사전 검토는 `rolcreatedb` 를 **읽어서** 거절 사유로 쓴다. 그 리터럴은
    # 권한을 주는 것이 아니므로 여기서 세면 안 된다.
    body = (
        _code().upper()
        .replace("NOCREATEDB", "")
        .replace("ROLCREATEDB", "")
        .replace("'CREATEDB'", "")
    )
    for word in ("CREATEDB ", "CREATE DATABASE", "DROP ", "DROP ROLE"):
        assert word not in body, word


def test_the_target_is_checked_with_a_setting_that_works_over_a_socket():
    """`inet_server_port()` 은 유닉스 소켓에서 NULL 이라 대조가 **늘 통과**한다."""
    body = _code()
    assert "current_setting('port')" in body
    assert "inet_server_port" not in body


def test_the_create_and_the_enforce_use_the_same_attributes():
    """손으로 만든 역할이 섞여 있어도 이 스크립트를 돌리면 같은 모양이 된다."""
    body = _code()
    assert body.count('ATTRS="') == 1
    # 역할 이름도 속성도 **변수 하나**에서 온다. 두 줄이 갈라질 수 없다.
    assert "CREATE ROLE $ROLE_RUNTIME $ATTRS" in body
    assert "ALTER ROLE $ROLE_RUNTIME $ATTRS" in body


def test_the_credential_file_is_owner_only():
    assert "-m 0600" in SOURCE


def test_the_script_points_at_the_read_only_checker():
    assert "check_vision_runtime.py" in SOURCE


def test_server_commands_use_the_virtualenv_interpreter():
    """서버에는 `python` 이 PATH 에 없다. 전역을 쓰면 다른 버전으로 돌아 진단이 어긋난다."""
    for line in SOURCE.splitlines():
        if "scripts/check_vision_runtime.py" in line and "python" in line:
            assert "/opt/tybot/.venv/bin/python" in line
