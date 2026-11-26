"""B-99 — 설치된 유닛이 저장소 판과 다르면 드러난다."""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_unit_drift", ROOT / "scripts" / "check_unit_drift.py"
)
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

UNIT = "tybot-archiver@.service"
OLD = "tybot-archiving-shadow@.service"


def _dirs(tmp_path):
    deploy = tmp_path / "deploy"
    systemd = tmp_path / "systemd"
    deploy.mkdir()
    systemd.mkdir()
    (deploy / UNIT).write_text(
        "[Service]\nReadWritePaths=/a /var/lib/tybot/image-description-notify\n",
        encoding="utf-8",
    )
    (deploy / "tybot-image-description@.service").write_text("[Service]\n", encoding="utf-8")
    (systemd / "tybot-image-description@.service").write_text("[Service]\n", encoding="utf-8")
    return deploy, systemd


def test_the_2026_11_20_unit_without_the_notice_outbox_is_reported(tmp_path, capsys):
    deploy, systemd = _dirs(tmp_path)
    (systemd / UNIT).write_text("[Service]\nReadWritePaths=/a\n", encoding="utf-8")

    code = drift.main(["--deploy-dir", str(deploy), "--systemd-dir", str(systemd)])

    assert code == drift.EXIT_DRIFT
    assert f"differs       {UNIT}" in capsys.readouterr().out


def test_comment_only_differences_are_not_drift(tmp_path):
    deploy, systemd = _dirs(tmp_path)
    (systemd / UNIT).write_text(
        "# installed copy\n[Service]\nReadWritePaths=/a /var/lib/tybot/image-description-notify\n",
        encoding="utf-8",
    )
    assert drift.main(["--deploy-dir", str(deploy), "--systemd-dir", str(systemd)]) == 0


def test_a_required_unit_that_was_never_installed_is_reported(tmp_path):
    deploy, systemd = _dirs(tmp_path)
    results = dict(drift.compare(deploy, systemd))
    assert results[UNIT] == "missing"


def test_the_installer_installs_the_archiver_unit_and_the_notice_outbox():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert f'"$APP_DIR/deploy/{UNIT}" /etc/systemd/system/{UNIT}' in text
    assert '"$DATA_DIR/image-description-notify"' in text
    assert '"$DATA_DIR/archiver"' in text


def test_the_retired_shadow_unit_left_installed_is_reported(tmp_path, capsys):
    deploy, systemd = _dirs(tmp_path)
    (systemd / UNIT).write_text((deploy / UNIT).read_text(encoding="utf-8"), encoding="utf-8")
    (systemd / OLD).write_text("[Service]\n", encoding="utf-8")

    code = drift.main(["--deploy-dir", str(deploy), "--systemd-dir", str(systemd)])

    assert code == drift.EXIT_DRIFT
    assert f"retired-present {OLD}" in capsys.readouterr().out


def test_the_archiver_unit_conflicts_with_the_old_one_and_drops_the_shadow_root():
    text = (ROOT / "deploy" / UNIT).read_text(encoding="utf-8")
    assert "Conflicts=tybot-archiving-shadow@%i.service" in text
    assert "LOCK_DIR=/var/lib/tybot/archiver/%i/.locks" in text
    rw = next(line for line in text.splitlines() if line.startswith("ReadWritePaths="))
    assert "archiver-shadow" not in rw and "dm-handoff" not in rw
    assert "/var/lib/tybot/image-description-notify" in rw
    assert not (ROOT / "deploy" / OLD).exists()


def _switch_block() -> str:
    text = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    start = text.index("ARCHIVER_SWITCH_FAILED=()")
    end = text.index("폐기된 tybot-archiving-shadow@ 유닛 파일을 제거했습니다")
    return text[start:end]


def test_a_console_deploy_switches_running_archivers_to_the_new_unit():
    """SSH 없이 콘솔 배포 한 번으로 전환한다(운영 기능은 콘솔에)."""
    block = _switch_block()
    assert "'tybot-archiving-shadow@*.service'" in block
    assert 'systemctl start "tybot-archiver@$ws.service"' in block
    # 새 유닛이 안 뜨면 옛 유닛으로 되돌려 수집을 잇는다.
    assert 'systemctl start "tybot-archiving-shadow@$ws.service"' in block


def test_the_old_unit_file_is_removed_only_after_every_instance_moved():
    block = _switch_block()
    assert '((${#ARCHIVER_SWITCH_FAILED[@]} == 0)) && [[ -z "$OLD_LEFT" ]]' in block
    # pipefail 아래 `| grep -q` 는 SIGPIPE 로 판정이 뒤집힐 수 있다.
    assert "'tybot-archiving-shadow@*.service' | grep" not in block


def test_a_failed_switch_fails_the_deploy_after_services_are_up():
    text = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    tail = text[text.index('systemctl is-active --quiet tybot; then'):]
    assert "ARCHIVER_SWITCH_FAILED[*]" in tail and "exit 5" in tail


def test_a_bot_deploy_restarts_archivers_that_already_run_the_new_unit():
    """B-100 후속: 이미 tybot-archiver@ 로 돌던 Archiver 가 옛 코드로 남지 않는다."""
    text = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    # 전환 전에 목록을 받아야 방금 전환한 인스턴스를 한 번 더 띄우지 않는다.
    before = text.index("RUNNING_ARCHIVERS=$(")
    assert before < text.index("OLD_ARCHIVERS=$(")
    assert "'tybot-archiver@*.service'" in text[before:before + 200]
    assert 'systemctl restart "$unit"' in text
    # 콘솔 배포(console 범위)는 이 앞에서 끝난다 — 봇 배포에서만 Archiver 를 건드린다.
    assert text.index('if [[ "$SCOPE" == "console" ]]; then') < before
    tail = text[text.index('systemctl is-active --quiet tybot; then'):]
    assert "ARCHIVER_RESTART_FAILED[*]" in tail
