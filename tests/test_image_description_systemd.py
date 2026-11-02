from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_worker_unit_uses_dedicated_credential_and_hardening():
    unit = (ROOT / "deploy/tybot-image-description@.service").read_text(encoding="utf-8")

    assert "LoadCredential=tybot-vision-database-url:" in unit
    assert "EnvironmentFile=/etc/tybot/archiver.env" in unit
    assert "--workspace %i" in unit
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ReadWritePaths=/var/lib/tybot/image-description" in unit
    assert "DATABASE_URL=" not in unit


def test_worker_template_is_installed_but_never_enabled_by_common_installer():
    install = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")

    assert 'deploy/tybot-image-description@.service"' in install
    assert 'deploy/tybot-image-description@.timer"' in install
    assert "systemctl enable --now tybot-image-description" not in install
    timer_line = next(line for line in install.splitlines() if line.startswith("TIMERS="))
    assert "image-description" not in timer_line


def test_timer_runs_the_matching_workspace_instance():
    timer = (ROOT / "deploy/tybot-image-description@.timer").read_text(encoding="utf-8")

    assert "Unit=tybot-image-description@%i.service" in timer
    assert "Persistent=true" in timer
