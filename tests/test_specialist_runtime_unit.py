"""전문 봇 2단계 런타임 템플릿 (2026-09-10).

설계: `docs/design/specialist-runtime-v2.md` 구현 순서 3단계 —
「고정 systemd/Podman unit과 root helper. arbitrary argument 거부 테스트」

지키는 것 하나. **콘솔 사용자에게 Podman 권한도, root shell 도, 임의 unit 작성
권한도 주지 않는다.** 콘솔은 DB 에 작업을 요청할 뿐이고 고정된 helper 가 허용
목록에 있는 동작만 한다. 그래서 helper 가 받는 것은 동작 이름과 key 뿐이다 —
이미지·마운트·환경변수를 인자로 받는 순간, 그 인자를 넣을 수 있는 사람이 곧
root 로 임의 컨테이너를 띄울 수 있는 사람이 된다.

실제 격리(cgroup·namespace·egress)는 Rocky staging 통합 테스트 몫이다. 여기서
고정하는 것은 **템플릿이 무엇을 선언하고 무엇을 거부하는가** 다.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "tybot-specialist@.service"
HELPER = ROOT / "deploy" / "tybot-specialist-run"
TARGET = ROOT / "deploy" / "tybot-specialists.target"

BASH = shutil.which("bash")


def _unit() -> str:
    return UNIT.read_text(encoding="utf-8")


def _helper() -> str:
    return HELPER.read_text(encoding="utf-8")


def _run(*args: str, env: dict[str, str] | None = None):
    """helper 를 실제로 돌린다.

    **인코딩을 명시한다.** Windows 기본 로캘로 읽으면 한글 오류 문구가 통째로
    `None` 이 되고, 그러면 "거부했는가" 가 아니라 "무슨 이유로 거부했는가" 를
    확인할 수 없다.
    """
    # 저장소 안의 고정 스크립트다 — 인자를 조립하지 않는다.
    return subprocess.run(
        [BASH, str(HELPER), *args],
        capture_output=True, timeout=30, check=False,
        encoding="utf-8", errors="replace", env=env,
    )


# --- 임의 인자를 받지 않는다 --------------------------------------------------
needs_bash = pytest.mark.skipif(BASH is None, reason="bash 없음")


@needs_bash
@pytest.mark.parametrize("action", ["run", "exec", "shell", "", "START", "rm"])
def test_only_three_actions_are_allowed(action):
    """동작을 늘리면 이 helper 가 범용 실행기가 된다."""
    got = _run(action, "hermes")

    assert got.returncode != 0
    assert "허용된 동작" in got.stderr or "인자는" in got.stderr


@needs_bash
@pytest.mark.parametrize("key", [
    "../../etc/passwd",
    "hermes; rm -rf /",
    "hermes hermes",
    "Hermes",
    "-x",
    "",
    "a" * 40,
    "hermes$(id)",
])
def test_a_bad_key_is_refused(key):
    """key 는 컨테이너 이름·소켓 경로·사용자 이름에 모두 들어간다.
    한 곳이라도 새면 나머지가 다 열린다."""
    got = _run("precheck", key)

    assert got.returncode != 0
    assert "key 형식" in got.stderr or "인자는" in got.stderr


@needs_bash
def test_extra_arguments_are_refused():
    """이미지·마운트·환경변수를 인자로 받으면, 그 인자를 넣을 수 있는 사람이
    곧 root 로 임의 컨테이너를 띄울 수 있는 사람이 된다."""
    got = _run("start", "hermes", "--privileged")

    assert got.returncode != 0
    assert "인자는" in got.stderr


@needs_bash
def test_precheck_refuses_a_tag_instead_of_a_digest():
    """태그는 움직인다. 움직이는 것을 승인하면 승인한 것과 도는 것이 갈린다."""
    got = _run("precheck", "hermes", env={
        "TYBOT_SPECIALIST_IMAGE": "localhost/tybot-specialist/hermes:latest"
    })

    assert got.returncode != 0
    assert "digest" in got.stderr


@needs_bash
def test_precheck_refuses_another_specialists_image():
    """digest 만 맞으면 **남의 전문가 이미지**를 이 자리에 띄울 수 있다."""
    got = _run("precheck", "hermes", env={
        "TYBOT_SPECIALIST_IMAGE":
            "localhost/tybot-specialist/legal@sha256:" + "a" * 64
    })

    assert got.returncode != 0


@needs_bash
def test_precheck_refuses_a_missing_image():
    got = _run("precheck", "hermes", env={})

    assert got.returncode != 0
    assert "이미지가 없습니다" in got.stderr


# --- unit 이 선언하는 격리 ----------------------------------------------------
@pytest.mark.parametrize("needed", [
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "PrivateTmp=true",
    "RestrictSUIDSGID=true",
])
def test_the_unit_confines_the_helper(needed):
    assert needed in _unit()


def test_the_container_drops_every_capability():
    for needed in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only"):
        assert needed in _helper(), needed


def test_the_container_has_resource_limits():
    """설계표의 값과 같아야 한다 — 갈리면 문서가 거짓말이 된다."""
    helper = _helper()

    assert "--cpus=1" in helper
    assert "--memory=512m" in helper
    assert "--pids-limit=128" in helper


def test_the_network_is_closed_by_default():
    """allowlist 를 실제로 강제할 수단이 없으면 열지 않는다. 단순 DNS 이름
    확인만으로는 우회 가능하다."""
    helper = _helper()

    assert "--network=none" in helper
    assert "TYBOT_EGRESS_NETWORK" in helper, "강제 수단 없이 프로필을 켠다"


def test_only_the_socket_dir_and_one_secret_are_mounted():
    """호스트 경로 mount 금지. 예외는 그 봇의 소켓 디렉터리와 읽기 전용 시크릿
    파일 하나뿐이다(설계 §런타임 격리)."""
    volumes = [
        line.strip() for line in _helper().splitlines()
        if line.strip().startswith("--volume")
    ]

    assert len(volumes) == 2, volumes
    assert any("/run/specialist" in v for v in volumes)
    assert any("/run/secrets/hmac:ro" in v for v in volumes)


@pytest.mark.parametrize("forbidden", [
    "/etc/tybot",
    "/var/lib/tybot/archive",
    "/var/run/postgresql",
    "podman.sock",
    "/root",
])
def test_the_container_never_mounts_our_data(forbidden):
    """전문 봇이 Slack·DB·아카이브를 직접 조회하는 것은 이번 단계에서 하지
    않는 것 목록에 있다."""
    assert forbidden not in _helper()


def test_secrets_go_in_as_files_not_environment():
    """환경변수는 `/proc/<pid>/environ` 과 크래시 덤프, systemd 상태 출력에
    그대로 보인다."""
    unit = _unit()

    assert "LoadCredential=hmac:" in unit
    for leaked in ("Environment=SLACK_", "Environment=DATABASE_URL",
                   "Environment=ANTHROPIC_API_KEY", "Environment=WORKSPACE_SECRET"):
        assert leaked not in unit, leaked


def test_the_specialist_runs_as_its_own_user():
    """TYBot·콘솔과 같으면 프로세스 격리가 이름만 남는다."""
    unit = _unit()

    assert "User=specialist-%i" in unit
    assert "User=tybot" not in unit


def test_restarts_are_rate_limited():
    """무한 재시작은 죽어 가는 컨테이너를 계속 밀어붙이고, 그 사이 라우팅은
    열린 채로 남는다."""
    unit = _unit()

    assert "StartLimitBurst=" in unit
    assert "RestartSec=" in unit


# --- 마스터를 끌어내리지 않는다 -----------------------------------------------
def test_tybot_does_not_depend_on_specialists():
    """마스터가 전문 봇에 의존하면 남의 코드 하나가 Slack 답변 전체를 세운다."""
    target = TARGET.read_text(encoding="utf-8")

    assert "tybot.service" not in target
    assert "Requires=" not in _unit()


def test_the_unit_is_a_fixed_template():
    """DB 값이나 업로드 내용으로 unit 을 만들지 않는다. 만들면 매니페스트 한
    줄이 곧 systemd 설정이 되고, 그건 root 로 임의 실행하는 것과 같다.

    바깥에서 들어오는 값은 **인스턴스 이름과 이미지 drop-in 뿐**이다.
    """
    unit = _unit()
    env_lines = [
        line for line in unit.splitlines() if line.startswith("Environment=")
    ]

    assert env_lines == ["Environment=TYBOT_SPECIALIST_IMAGE="], env_lines
    # ExecStart 는 고정 helper 하나만 부른다 — 셸을 거치지 않는다.
    for line in unit.splitlines():
        if line.startswith("Exec"):
            assert "/usr/local/libexec/tybot-specialist-run" in line
            assert not any(ch in line for ch in ";|&`$(")


def test_the_helper_is_installed_and_owned_by_root():
    """콘솔이 쓸 수 있는 곳에 있으면 helper 자체를 바꿔치기할 수 있다."""
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "tybot-specialist-run" in install, "install.sh 가 helper 를 배치하지 않는다"
    assert "tybot-specialist@.service" in install
