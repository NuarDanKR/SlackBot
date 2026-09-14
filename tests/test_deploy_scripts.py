import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_update_restarts_bot_after_install():
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    install = 'TYBOT_INSTALL_HINTS=0 bash "$SRC/deploy/install.sh"'
    restart = "systemctl restart tybot"

    assert install in script
    assert restart in script
    assert script.index(install) < script.index(restart)


def test_install_hides_onboarding_hints_during_update():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'if [[ "${TYBOT_INSTALL_HINTS:-1}" != "1" ]]' in script
    assert 'echo "설치 파일 갱신 완료"' in script
    assert script.index('echo "설치 파일 갱신 완료"') < script.index("설치 완료. 다음 순서")


def test_console_install_enables_fixed_deploy_path():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'if [[ "${WITH_CONSOLE:-0}" == "1" ]]' in script
    assert "systemctl enable --now tybot-deploy.path" in script


def test_install_does_not_use_rsync_at_all():
    """SELinux 가 rsync 를 갇힌 도메인(rsync_t)으로 전이시켜 root 여도 막힌다.

    rsync_t 는 var_lib_t 를 읽지 못하는데 소스 체크아웃이 거기 있다.
    SSH 로 직접 돌리면 전이가 없어 되고, 콘솔 배포에서만 실패했다(2026-09-02).
    정책으로 뚫으면 rsync_t 가 다른 서비스의 var_lib_t 까지 읽게 된다.
    """
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    commands = [
        line for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert not any("rsync" in line for line in commands)
    assert "rsync_t" in script, "왜 안 쓰는지가 남아 있어야 한다"


def test_deploy_runner_records_output_and_commit_titles():
    script = (ROOT / "deploy" / "deploy-runner.sh").read_text(encoding="utf-8")

    assert 'bash "$APP/deploy/update.sh" 2>&1 | tee "$OUTPUT"' in script
    assert 'git -C "$SRC" log -1 --format=%s' in script
    assert 'DEPLOY_DETAIL_FILE="$OUTPUT" status failed' in script


def test_source_checkout_is_not_under_tmp():
    """/tmp 는 소스 체크아웃을 둘 곳이 아니다.

    systemd-tmpfiles 가 오래된 파일을 지워 체크아웃이 통째로 사라지고,
    SELinux 가 /tmp 를 user_tmp_t 로 라벨해 서비스가 읽지 못하는 일이 생긴다.
    2026-09-02 콘솔 배포가 rsync Permission denied 로 실패한 것이 그 경우다.
    """
    for name in ("deploy-runner.sh", "update.sh"):
        script = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "TYBOT_SRC:-/var/lib/tybot/src" in script, name
        assert "TYBOT_SRC:-/tmp" not in script, name

    unit = (ROOT / "deploy" / "tybot-deploy.service").read_text(encoding="utf-8")
    assert "Environment=TYBOT_SRC=/var/lib/tybot/src" in unit
    assert "/tmp/tybot-src" not in unit


def test_deploy_runner_does_not_widen_umask_for_the_whole_deploy():
    """umask 077 이 update.sh 까지 걸리면 배치된 파일이 600 이 되고,

    봇 계정이 자기 코드를 못 읽는다. 로그 파일만 좁게 만든다.
    """
    script = (ROOT / "deploy" / "deploy-runner.sh").read_text(encoding="utf-8")

    # 주석에는 사유로 남아 있어도 된다. 실행되는 줄에만 없으면 된다.
    commands = [
        line.strip() for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("umask") for line in commands)
    assert 'chmod 600 "$OUTPUT"' in script


def test_install_grants_group_read_not_just_removes_bits():
    """`g-w,o-rwx` 는 비트를 빼기만 한다. 600 파일은 그대로 600 이라 봇이 못 읽는다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "g+rX,g-w,o-rwx" in script
    assert 'sudo -u tybot test -r "$APP_DIR/src/tybot/slack/pilot.py"' in script


def test_update_deploys_when_source_is_current_but_opt_is_stale():
    """손으로 git pull 한 뒤 update.sh 를 돌리면 '변경 없음' 으로 끝나던 문제.

    소스와 원격이 같아져 새 커밋이 없다고 판단하는데, 정작 /opt 에는 아무것도
    들어가지 않는다. 운영자는 배포됐다고 믿고 넘어간다(2026-09-02 실제 발생).
    배포된 커밋을 따로 기록해 두고 그것과 비교한다.
    """
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert '.deployed-commit' in script
    assert 'git rev-parse HEAD > "$APP/.deployed-commit"' in script
    # 건너뛰기 조건에 '배포본도 같은가' 가 들어가야 한다.
    assert '"$DEPLOYED" == "$LOCAL"' in script


def test_update_can_be_forced():
    """설치 옵션만 바뀌었을 때(WITH_CONSOLE 등) 같은 커밋을 다시 배포할 길이 필요하다."""
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert 'TYBOT_FORCE' in script


def test_install_does_not_take_ownership_of_the_source_checkout():
    """/var/lib/tybot 을 통째로 tybot 소유로 바꾸면 다음 배포가 막힌다.

    git 은 root 가 남의 소유 저장소에서 도는 것을 dubious ownership 으로 거부한다.
    소스가 그 아래로 들어왔으므로 제외해야 한다(2026-09-02 실제 발생).
    """
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "! -name src" in script
    assert 'chown -R tybot:tybot "$DATA_DIR"\n' not in script


def test_update_refuses_a_non_root_checkout_with_a_fix():
    """git 의 dubious ownership 오류를 그대로 만나면 무엇을 해야 할지 알기 어렵다."""
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "chown -R root:root $SRC" in script
    # safe.directory 로 방어를 끄지 않는다.
    assert "safe.directory" not in script.replace("`safe.directory` 로 예외를 두는", "")


def test_schedule_sync_also_runs_the_exporter():
    """받는 쪽만 돌면 inbox 가 영원히 비어 있다.

    방식 A(봇 서버가 Oracle 을 직접 조회)로 바꾸면서, 스냅샷을 **만드는** 쪽이
    어느 유닛에도 없었다. 로그에는 '반영할 스냅샷이 없다' 만 남아 정상처럼 보였다.
    """
    unit = (ROOT / "deploy" / "tybot-schedule-sync.service").read_text(encoding="utf-8")

    assert "schedule_export.py" in unit
    assert "--mode live" in unit
    # 추출이 먼저다. 반대로면 늘 한 주기 늦은 자료를 반영한다.
    assert unit.index("schedule_export.py") < unit.index("-m tybot.schedulesync")


def test_reconcile_uses_a_separate_inbox():
    """live 와 한 폴더를 쓰면 이름 정렬에서 'reconcile' 이 'live' 를 항상 이긴다.

    newest_snapshot 이 이름 최대값을 고르므로, 낡은 reconcile 이 새 live 를 가린다.
    """
    unit = (ROOT / "deploy" / "tybot-schedule-reconcile.service").read_text(encoding="utf-8")

    assert "Environment=SCHEDULE_INBOX=/var/lib/tybot/inbox-schedule-reconcile" in unit
    assert "--mode reconcile" in unit
    assert "/var/lib/tybot/inbox-schedule " not in unit


def test_reconcile_timer_is_installed_and_switchable():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "deploy" / "tybot-console-timers").read_text(encoding="utf-8")

    assert "tybot-schedule-reconcile" in install
    # 콘솔에서 켜고 끌 수 있어야 한다 — 없으면 SSH 로만 만질 수 있다.
    assert "tybot-schedule-reconcile.timer" in wrapper


def test_copy_keeps_nested_archive_but_drops_the_top_level_one():
    """`archive` 로만 제외하면 src/tybot/archive/ 까지 빠져 봇이 죽는다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "--exclude=./archive" in script
    assert "--exclude=archive " not in script


def test_copy_removes_files_that_vanished_from_the_source():
    """tar 에는 --delete 가 없다.

    지워진 모듈이 /opt 에 남으면 파이썬이 그걸 계속 import 한다.
    """
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "comm -13" in script
    assert 'rm -f "$APP_DIR/$rel"' in script


def test_copy_never_deletes_what_only_the_server_builds():
    """.venv·console-web/dist·.deployed-commit 은 소스에 없다. 지우면 자기 발을 밟는다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    keep = script[script.index("KEEP_IN_DEST="):]

    assert "--exclude=./.deployed-commit" in keep
    assert "TREE_EXCLUDES[@]" in keep, ".venv·dist 는 공통 목록에서 물려받는다"


def test_console_build_failure_fails_the_deployment():
    """An old dist must not be reported as a successful new deployment."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    build_section = script[script.index("# --- 콘솔 화면 빌드 ---"):script.index("== 5/6 권한")]

    assert "이전 화면으로 성공 처리하지 않습니다" in build_section
    assert "exit 1" in build_section
    assert "CONSOLE_BUILD_FAILED=1" not in script


def test_console_log_helper_outputs_newest_records_first():
    script = (ROOT / "deploy" / "tybot-console-logs").read_text(encoding="utf-8")

    assert "slot = ((saved - i - 1) % limit) + 1" in script


def test_install_says_why_it_cannot_set_permissions():
    """권한을 못 바꾸는 환경이면 find 가 수천 줄 오류를 쏟고서야 멈춘다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'chmod u+rwx "$APP_DIR" 2>/dev/null' in script
    for hint in ("getenforce", "root_squash", "lsattr"):
        assert hint in script, hint


def test_install_creates_the_workspace_secret_key():
    """문서에만 있던 수동 절차라 실제로 빠져 있었다.

    키가 없으면 콘솔의 워크스페이스 관리가 아무것도 저장하지 못한다.
    """
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "Fernet.generate_key()" in script
    assert "WORKSPACE_SECRET_KEY_FILE=$WS_KEY_FILE" in script
    # 봇만 읽을 수 있어야 한다. 넓으면 workspace_store 가 거부한다(0400 요구).
    assert 'install -o tybot -g tybot -m 0400 /dev/stdin "$WS_KEY_FILE"' in script


def test_install_never_overwrites_an_existing_workspace_key():
    """키가 바뀌면 저장된 Slack 토큰을 영영 못 푼다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert '! -f "$WS_KEY_FILE"' in script


def test_index_timer_is_installed_and_switchable():
    """색인이 멈추면 검색이 조용히 옛 자료만 본다. 켜고 끌 자리가 있어야 한다."""
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "deploy" / "tybot-console-timers").read_text(encoding="utf-8")
    unit = (ROOT / "deploy" / "tybot-index.service").read_text(encoding="utf-8")

    assert "tybot-index" in install
    assert "tybot-index.timer" in wrapper
    assert "-m tybot.search_index" in unit


def test_docs_never_call_psql_without_the_port():
    """우리 PostgreSQL 은 55432 다. 포트를 빼면 5432 소켓을 찾아 실패한다.

    그 오류 문구가 「DB 가 안 돌고 있다」 로 읽혀서, 실제로 여러 번 헤맸다.
    """

    bad: list[str] = []
    for folder in ("docs", "deploy"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix not in {".md", ".sql", ".sh"} or not path.is_file():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "postgres psql" in line and "-p " not in line:
                    bad.append(f"{path.relative_to(ROOT)}:{i}")

    assert not bad, f"psql 호출에 포트가 없다: {bad}"


# 운영 절차가 적히는 곳. 여기 적힌 명령은 **서버에 그대로 붙여 넣는다.**
# 설계 문서(`docs/design/`)는 개발 PC 기준이라 제외한다.
OPERATIONAL_DOCS = ("docs/deploy", "docs/verification")


def test_operational_docs_call_python_through_the_venv():
    """서버에는 `python` 이 없다. Rocky 8 은 `python3` 뿐이고 의존성도 없다.

    `bash: python: command not found` 가 나오면 사람은 **파이썬이 안 깔렸다**고
    읽는다. 실제로는 깔려 있고 이름이 다를 뿐인데, 그 오해가 설치 절차를 처음부터
    다시 밟게 만든다(2026-09-14 실제로 그랬다).
    """
    bad: list[str] = []
    for folder in OPERATIONAL_DOCS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.strip()
                # 붙여 넣는 명령만 본다. 산문 속 언급은 대상이 아니다.
                if not code.startswith(("python ", "python3 ", "$ python ")):
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{i}")
    assert not bad, (
        f"venv 를 거치지 않는 python 호출: {bad}. "
        "서버에서는 `.venv/bin/python` 으로 부른다."
    )


def test_scripts_that_need_the_database_load_the_env_file():
    """설정을 안 읽으면 **systemd 에서는 되고 사람 손으로는 안 된다.**

    unit 은 `TYBOT_ENV_FILE` 을 박아 두니 그쪽에서만 돌고, 같은 명령을 사람이
    치면 `DATABASE_URL 이 없습니다` 가 나온다. 가장 헷갈리는 모양이다 —
    "서비스는 멀쩡한데 왜 나만 안 되지" 로 시간을 버린다(2026-09-14 실제로 그랬다).
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        code = path.read_text(encoding="utf-8")
        needs_db = "DATABASE_URL" in code or "conversion_queue" in code
        if not needs_db or "def main(" not in code:
            continue
        if "load_env_file()" not in code:
            offenders.append(path.name)
    assert not offenders, (
        f"DB 를 쓰면서 설정 파일을 안 읽는 스크립트: {offenders}. "
        "`load_env_file()` 을 main 에서 부른다."
    )


def test_docs_never_hand_a_locked_path_to_psql():
    """`psql -f` 는 **psql 프로세스가** 읽는다. 그 프로세스는 postgres 계정이다.

    `/opt/tybot` 은 설치가 `root:tybot`·`o-rwx` 로 잠그므로 postgres 는 읽지 못한다.
    권한을 풀면 봇이 자기 코드를 고칠 수 있게 되므로, root 로 읽어 stdin 으로 넘긴다.
    """
    bad: list[str] = []
    for folder in ("docs", "deploy"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix not in {".md", ".sql", ".sh"} or not path.is_file():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "psql" not in line or "-f /opt/tybot" not in line:
                    continue
                if line.lstrip().startswith("#") or line.lstrip().startswith("###"):
                    continue  # 설명 문구는 예외
                bad.append(f"{path.relative_to(ROOT)}:{i}")

    assert not bad, f"postgres 계정이 읽을 수 없는 경로를 psql 에 넘긴다: {bad}"


# --- 타이머를 안 걸면 아무 일도 안 일어난다 ----------------------------------
def test_every_timer_unit_is_actually_enabled():
    """`deploy/*.timer` 를 만들고 `install.sh` 의 `TIMERS` 에 안 넣은 적이 있다.

    파일은 설치되고 유닛도 유효한데 **아무도 켜지 않는다.** 오류가 없어서
    「돌고 있다」 고 믿게 되고, 몇 주 뒤에 결과물이 비어 있는 것으로 알게 된다.
    (실제로 아카이브 내보내기가 그렇게 몇 주를 놀았다.)
    """
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    line = next(row for row in install.splitlines() if row.startswith("TIMERS="))

    for timer in sorted((ROOT / "deploy").glob("*.timer")):
        # `tybot-deploy.path` 처럼 path 유닛은 따로 켠다. 타이머만 본다.
        assert timer.stem in line, f"{timer.name} 을 켜는 코드가 없다"


def test_every_timer_can_be_switched_from_the_console():
    """콘솔에서 못 끄면 SSH 로만 만질 수 있다. 사고 날 때 그 시간이 없다."""
    wrapper = (ROOT / "deploy" / "tybot-console-timers").read_text(encoding="utf-8")

    for timer in sorted((ROOT / "deploy").glob("*.timer")):
        assert timer.name in wrapper, f"{timer.name} 을 콘솔에서 켜고 끌 수 없다"


def test_the_deploy_verifies_specialist_contracts():
    """계약이 안 옮겨지면 `available_keys()` 가 그 전문가를 빼고, 콘솔은
    「미배포」 로 라우터는 「후보 없음」 으로 보인다 — 어디에도 오류가 없어서
    원인을 찾는 데 가장 오래 걸린다."""
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "subbots/*/contract/prompt.md" in install
    assert "배치 누락" in install


def test_subbots_is_not_excluded_from_the_copy():
    """제외하면 서버에 계약이 아예 없다."""
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    excludes = next(
        block for block in install.split("\n\n") if "TREE_EXCLUDES=(" in block
    )

    assert "subbots" not in excludes


# --- 스키마 적용 (2026-09-14) --------------------------------------------------
#
# 배포 문서가 적용할 스키마 파일을 나열했는데 그 목록이 드리프트했다. 스키마 파일은
# 18개인데 문서에는 7개만 있었고, `console_schema.sql` 과
# `specialist_runtime_schema.sql` 은 **한 번도 적용되지 않았다.**
#
# 그래서 콘솔 「봇 분류 상태」 가 `column "qa_record_id" does not exist` 로 죽었다.
# 빠진 것은 컬럼 9개와 표 4개였다 — 나머지는 그 화면을 아직 안 열어서 안 터졌을 뿐이다.
#
# 사람이 목록을 옮겨 적는 단계는 한 번은 빠진다. 그래서 여기서 막는다.
APPLY_SCRIPT = ROOT / "deploy" / "apply-schema.sh"
SQL_DIR = ROOT / "deploy" / "sql"


def _listed_files() -> list[str]:
    """`FILES=(...)` 안의 파일 이름들.

    닫는 괄호를 문자열에서 찾지 않는다 — 주석에 `(멱등)` 처럼 괄호가 들어 있어서
    거기서 잘린다. 줄 단위로 읽고 주석을 먼저 떼어 낸다.
    """
    out: list[str] = []
    inside = False
    for raw in APPLY_SCRIPT.read_text(encoding="utf-8").splitlines():
        if not inside:
            inside = raw.strip().startswith("FILES=(")
            continue
        code = raw.split("#", 1)[0].strip()
        if code == ")":
            break
        if code:
            out.append(code)
    return out


def _postgres_schemas() -> list[str]:
    """Oracle 쪽(`oracle_*`·`export_*`)은 그룹웨어 DB 에서 DBA 가 돌린다."""
    return sorted(
        p.name for p in SQL_DIR.glob("*.sql")
        if not p.name.startswith(("oracle_", "export_"))
    )


def test_every_postgres_schema_file_is_applied():
    """새 스키마를 만들고 목록에 안 넣으면, 그 표는 영영 안 생긴다."""
    missing = sorted(set(_postgres_schemas()) - set(_listed_files()))
    assert not missing, f"apply-schema.sh 에 없는 스키마 파일: {missing}"


def test_the_script_does_not_list_files_that_are_gone():
    """없는 파일을 돌리면 스크립트가 통째로 멈춘다."""
    ghosts = sorted(set(_listed_files()) - set(_postgres_schemas()))
    assert not ghosts, f"파일이 없는데 목록에 있다: {ghosts}"


def test_structure_comes_before_features():
    """뒤 파일이 앞 파일의 표를 외래키로 참조한다. 알파벳 순으로 돌리면 깨진다."""
    files = _listed_files()
    assert files.index("index_schema.sql") < files.index("console_schema.sql")
    for later in ("schedule_dm_schema.sql", "reviewer_schema.sql",
                  "specialist_runtime_schema.sql"):
        assert files.index("console_schema.sql") < files.index(later), later


def test_data_migrations_run_last():
    """구조가 다 선 뒤에 값을 고친다."""
    files = _listed_files()
    last_structure = max(
        files.index(n) for n in files if n.endswith("_schema.sql")
    )
    for data in ("schedule_folder_acl_default.sql", "schedule_dm_fixed_ten.sql"):
        assert files.index(data) > last_structure, data


def test_the_script_stops_on_the_first_error():
    """계속 돌리면 뒤 파일 오류가 줄줄이 나고 진짜 원인이 스크롤 위로 사라진다."""
    body = APPLY_SCRIPT.read_text(encoding="utf-8")
    assert "ON_ERROR_STOP=1" in body
    assert "set -euo pipefail" in body


def test_oracle_files_are_not_applied_to_postgres():
    """섞으면 psql 이 문법 오류로 죽는다."""
    listed = _listed_files()
    assert not [n for n in listed if n.startswith(("oracle_", "export_"))]


def test_the_deploy_doc_points_at_the_script():
    """문서가 다시 파일을 나열하기 시작하면 같은 드리프트가 돌아온다."""
    doc = (ROOT / "docs" / "deploy" / "rocky8.md").read_text(encoding="utf-8")
    assert "apply-schema.sh" in doc
    assert "check_schema_drift.py" in doc


def test_drift_check_reads_every_schema_file():
    """대조 도구가 일부 파일만 읽으면, 안 읽은 파일의 누락은 영영 안 보인다."""
    body = (ROOT / "scripts" / "check_schema_drift.py").read_text(encoding="utf-8")
    assert 'glob("*.sql")' in body
    # 주석 처리된 예시를 요구사항으로 잡으면 없는 고장을 매번 보고한다.
    assert "_uncommented" in body


def test_declared_alter_statements_are_idempotent():
    """`IF NOT EXISTS` 없이 쓰면 두 번째 적용에서 스크립트가 멈춘다."""
    bad: list[str] = []
    pattern = re.compile(r"^\s*ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS)",
                         re.IGNORECASE | re.MULTILINE)
    for name in _listed_files():
        text = (SQL_DIR / name).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("--")
        )
        if pattern.search(body):
            bad.append(name)
    assert not bad, f"IF NOT EXISTS 없는 ADD COLUMN: {bad}"


# --- 권한 (2026-09-14) ---------------------------------------------------------
#
# `apply-schema.sh` 가 표를 다 만들었는데 콘솔은 여전히 「표가 없다」 고 했다.
# 표는 있었다 — `postgres` 가 만들어 소유자가 postgres 였고 GRANT 가 없었다.
#
# `information_schema` 는 **권한 필터가 걸린 뷰**라서, 권한이 없는 표는 아예 안 보인다.
# 그래서 「권한 없음」 이 「없음」 으로 보고됐고, 조치까지 틀리게 안내했다 —
# 스키마를 다시 적용해도 아무것도 달라지지 않는다.
#
# `review_digest_sent` 에서 같은 일을 겪었다. 이번이 두 번째다.
def _sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """주석을 뺀 SQL. 문서의 GRANT 예시가 실제 GRANT 로 잡히면 안 된다."""
    return "\n".join(
        ln for ln in _sql(name).splitlines() if not ln.lstrip().startswith("--")
    )


def _created_tables(body: str) -> set[str]:
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("--")]
    return set(
        re.findall(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
            "\n".join(lines), re.IGNORECASE,
        )
    )


def test_schemas_that_create_tables_also_grant_them():
    """표만 만들고 GRANT 를 안 주면, 봇에게는 그 표가 없는 것과 같다."""
    offenders: list[str] = []
    for name in _listed_files():
        if not _created_tables(_sql(name)):
            continue
        if "GRANT" not in _code(name).upper():
            offenders.append(name)
    assert not offenders, (
        f"표를 만들면서 GRANT 가 없는 스키마: {offenders}. "
        "소유자가 다르면 권한은 자동으로 따라오지 않는다."
    )


def test_the_runtime_schema_grants_its_tables():
    granted = _code("specialist_runtime_schema.sql").upper().split("GRANT", 1)[1]
    for table in ("specialist_source", "specialist_build",
                  "specialist_deployment", "specialist_runtime_secret"):
        assert table.upper() in granted, table


def test_every_created_table_is_named_in_a_grant():
    """**GRANT 라는 낱말이 있는지만 보면 부족하다.**

    시퀀스에만 GRANT 를 주고 표는 빼먹어도 낱말 검사는 통과한다. 그 상태로
    배포하면 표는 있는데 봇에게는 안 보이고, 그 사실은 그 화면을 누가 열 때까지
    아무도 모른다 — `specialist_call` 에서 실제로 그랬다.
    """
    offenders: list[str] = []
    for name in _listed_files():
        tables = _created_tables(_sql(name))
        if not tables:
            continue
        code = _code(name).upper()
        granted = code.split("ON TABLE", 1)[1] if "ON TABLE" in code else ""
        for table in sorted(tables):
            if table.upper() not in granted:
                offenders.append(f"{name}:{table}")
    assert not offenders, (
        f"만들었지만 GRANT 에 이름이 없는 표: {offenders}. "
        "표가 있어도 봇에게는 없는 것과 같다."
    )


def test_grants_are_guarded_by_role_existence():
    """개발 DB 에 그 역할이 없으면 스키마 적용이 통째로 실패한다."""
    for name in _listed_files():
        code = _code(name)
        if "GRANT" not in code.upper():
            continue
        assert "pg_roles" in code, f"{name}: 역할 확인 없이 GRANT 한다"


def test_drift_check_does_not_use_information_schema():
    """권한 필터가 걸린 뷰다. 「권한 없음」 이 「없음」 으로 보고된다."""
    body = (ROOT / "scripts" / "check_schema_drift.py").read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "information_schema" not in code
    assert "pg_class" in code
    assert "has_table_privilege" in code


def test_drift_check_separates_denied_from_missing():
    """조치가 다르다 — 없으면 스키마 적용, 권한이면 GRANT 다."""
    body = (ROOT / "scripts" / "check_schema_drift.py").read_text(encoding="utf-8")
    assert "denied" in body
    assert "GRANT" in body
