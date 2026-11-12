from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_worker_unit_uses_dedicated_credential_and_hardening():
    unit = (ROOT / "deploy/tybot-image-description@.service").read_text(encoding="utf-8")

    assert not any(line.startswith("LoadCredential=") for line in unit.splitlines())
    assert "TYBOT_VISION_DATABASE_URL_FILE=/etc/tybot/tybot-vision-database-url" in unit
    # 2026-11-12 보안 리뷰: worker 는 Vision 전용 키만 받는다. 워크스페이스 토큰 키는 없다.
    assert "VISION_PROVIDER_SECRET_KEY_FILE=/etc/tybot/vision-provider-secret.key" in unit
    assert "WORKSPACE_SECRET_KEY" not in "\n".join(
        line for line in unit.splitlines() if not line.lstrip().startswith("#")
    )
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


def test_console_unit_gets_the_vision_dsn_file_path():
    """콘솔 이미지 설명 화면도 Vision 전용 DSN 만 쓴다. 경로가 빠지면 화면이 실패한다."""
    unit = (ROOT / "deploy/tybot-console.service").read_text(encoding="utf-8")
    assert "Environment=TYBOT_VISION_DATABASE_URL_FILE=/etc/tybot/tybot-vision-database-url" in unit
    assert not any(line.startswith("LoadCredential=") for line in unit.splitlines())


def test_setup_script_guides_a_tcp_dsn_not_the_peer_socket():
    """소켓 DSN 은 peer 인증으로 거절된다(2026-11-12). 안내는 TCP 루프백이어야 한다."""
    text = (ROOT / "deploy/setup-tybot-vision-runtime.sh").read_text(encoding="utf-8")
    assert "@127.0.0.1:$PORT/$DB" in text
    assert "<비밀번호>@/$DB?host=/run/postgresql" not in text
