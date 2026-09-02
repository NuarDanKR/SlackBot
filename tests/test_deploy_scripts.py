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
