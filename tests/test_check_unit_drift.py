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
