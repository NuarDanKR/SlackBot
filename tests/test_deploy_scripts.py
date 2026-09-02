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


def test_install_rsyncs_from_inside_source_directory():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'cd "$SRC_DIR"' in script
    assert './ "$APP_DIR"/' in script
    assert '"$SRC_DIR"/ "$APP_DIR"/' not in script


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


def test_rsync_does_not_set_destination_attributes():
    """`-a` 가 디렉터리 권한·시각까지 맞추려다 Permission denied 로 죽었다.

    5단계에서 소유권과 권한을 직접 설정하므로 보존할 이유가 없다.
    파일 mtime(-t)만 남긴다 — 없으면 매번 전부를 다시 보낸다.
    """
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "--no-perms" in script
    assert "--omit-dir-times" in script
    assert "rsync -a " not in script


def test_install_says_why_it_cannot_set_permissions():
    """권한을 못 바꾸는 환경이면 find 가 수천 줄 오류를 쏟고서야 멈춘다."""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'chmod u+rwx "$APP_DIR" 2>/dev/null' in script
    for hint in ("getenforce", "root_squash", "lsattr"):
        assert hint in script, hint
