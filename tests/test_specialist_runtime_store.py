"""전문 봇 2단계 — 빌드·배포 상태와 helper 계약 (2026-09-10).

설계: `docs/design/specialist-runtime-v2.md` 구현 순서 5·6단계.

DB 를 부르는 부분은 여기서 돌리지 않는다(운영 DB 를 만지게 된다). 고정하는 것은
**상태 전이 규칙**과 **helper 가 무엇을 거부하는가** 다 — 둘 다 순수하고, 둘 다
틀리면 조용히 나쁜 조합이 만들어진다.

실제 Podman 격리·빌드 파이프라인은 Rocky staging 통합 테스트 몫이다
(`docs/verification/specialist-runtime-v2-checklist.md`).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tybot.console import specialist_runtime_store as store

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "deploy" / "tybot-specialist-build"
DEPLOY = ROOT / "deploy" / "tybot-specialist-deploy"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash 없음")


def _run(script: Path, *args: str, env: dict[str, str] | None = None):
    # 인코딩을 명시한다 — 기본 로캘로 읽으면 한글 오류 문구가 None 이 된다.
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True, timeout=60, check=False,
        encoding="utf-8", errors="replace", env=env,
    )


# --- 상태 전이 ----------------------------------------------------------------
def test_a_failed_build_cannot_become_ready():
    """「빌드 실패인데 배포 가능」 같은 조합이 어디선가 만들어지는 것을 막는다."""
    with pytest.raises(store.RuntimeStoreError):
        store.check_build_transition("build_failed", "image_ready")


def test_a_ready_build_is_terminal():
    """다시 building 으로 돌리면 같은 빌드 기록이 두 번 쓰인다."""
    for nxt in store.BUILD_STATES:
        with pytest.raises(store.RuntimeStoreError):
            store.check_build_transition("image_ready", nxt)


def test_a_retired_deployment_cannot_come_back():
    """되돌리기는 **새 배포 기록**으로 한다. 옛 행을 되살리면 언제 무엇이
    돌았는지가 기록에서 사라진다."""
    with pytest.raises(store.RuntimeStoreError):
        store.check_deploy_transition("retired", "active")


def test_standby_can_go_active_or_fail():
    store.check_deploy_transition("standby", "active")
    store.check_deploy_transition("standby", "failed")


def test_active_can_only_retire_or_fail():
    store.check_deploy_transition("active", "retired")
    with pytest.raises(store.RuntimeStoreError):
        store.check_deploy_transition("active", "standby")


# --- 요청자 ≠ 승인자 ----------------------------------------------------------
def test_self_approval_is_refused():
    """한 사람이 둘 다 하면 승인이 형식이 되고, 승인 기록은 있는데 아무도 안
    본 상태가 된다."""
    with pytest.raises(store.RuntimeStoreError, match="스스로 승인"):
        store.check_separation("dan@taeyoung.com", "dan@taeyoung.com")


def test_case_does_not_bypass_separation():
    with pytest.raises(store.RuntimeStoreError):
        store.check_separation("Dan@Taeyoung.com", "dan@taeyoung.com")


def test_two_people_pass():
    store.check_separation("dev@taeyoung.com", "admin@taeyoung.com")


def test_an_empty_actor_is_refused():
    with pytest.raises(store.RuntimeStoreError):
        store.check_separation("", "admin@taeyoung.com")


# --- 시크릿은 꺼내지 않는다 ---------------------------------------------------
def test_the_mask_hides_the_secret():
    got = store.mask("s3cr3t-value-abcd")

    assert "s3cr3t" not in got
    assert got.endswith("abcd")


def test_a_short_secret_shows_nothing():
    """짧은 값에서 뒤 4자를 보이면 그것이 곧 상당 부분이다."""
    assert store.mask("abc") == "••••"


def test_there_is_no_decrypt_api():
    """화면으로 꺼낼 수 있으면 그 순간 브라우저·로그·스크린샷이 전부 보관
    장소가 된다."""
    import inspect

    source = inspect.getsource(store)

    for leaked in ("def decrypt", "def reveal", "def get_secret", "plaintext"):
        assert leaked not in source, f"복호화 경로가 있다: {leaked}"


def test_secret_status_returns_no_ciphertext():
    import inspect

    source = inspect.getsource(store.secret_status)

    assert "ciphertext" not in source


# --- 빌드 helper --------------------------------------------------------------
@needs_bash
@pytest.mark.parametrize("key", ["../x", "Hermes", "a b", "", "-x"])
def test_build_refuses_a_bad_key(key, tmp_path):
    got = _run(BUILD, key, str(tmp_path))

    assert got.returncode != 0
    assert "invalid-key" in got.stdout


@needs_bash
def test_build_refuses_extra_arguments(tmp_path):
    got = _run(BUILD, "hermes", str(tmp_path), "--privileged")

    assert got.returncode != 0


@needs_bash
def test_build_refuses_a_missing_lockfile(tmp_path):
    """없으면 빌드마다 다른 것을 설치하고 digest 가 흔들린다."""
    (tmp_path / "tybot-specialist.toml").write_text('runtime = "nodejs22"\n')

    got = _run(BUILD, "hermes", str(tmp_path))

    assert "lockfile-missing" in got.stdout


@needs_bash
def test_build_refuses_an_unknown_runtime(tmp_path):
    """매니페스트가 이미지를 고르게 하면 임의 이미지 실행이 된다."""
    (tmp_path / "tybot-specialist.toml").write_text('runtime = "python311"\n')
    (tmp_path / "package-lock.json").write_text("{}")

    got = _run(BUILD, "hermes", str(tmp_path))

    assert "runtime-denied" in got.stdout


@needs_bash
def test_build_fails_closed_without_a_registry_network(tmp_path):
    """인터넷 전체를 여는 것보다 실패하는 쪽이 맞다."""
    (tmp_path / "tybot-specialist.toml").write_text('runtime = "nodejs22"\n')
    (tmp_path / "package-lock.json").write_text("{}")

    got = _run(BUILD, "hermes", str(tmp_path), env={"PATH": "/usr/bin:/bin"})

    # podman 이 없는 개발 PC 에서도 여기까지는 온다 — 둘 중 하나로 닫힌다.
    assert "dependency-denied" in got.stdout or "podman-missing" in got.stdout


def test_the_builder_mounts_no_credentials():
    """빌드는 임의 코드 실행이다. /etc/tybot·아카이브·DB·Podman 소켓이 붙으면
    업로드한 사람이 우리 프로세스의 권한을 그대로 갖는다."""
    # **주석은 빼고 본다.** 주석에는 "무엇을 마운트하지 않는지" 가 적혀 있고,
    # 그것까지 금지어로 세면 이유를 적을수록 테스트가 깨진다.
    code = chr(10).join(
        line for line in BUILD.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    for forbidden in ("/etc/tybot", "/var/lib/tybot/archive", "podman.sock",
                      "/var/run/postgresql", ".ssh"):
        assert forbidden not in code, forbidden


def test_the_builder_disables_lifecycle_scripts():
    """`npm ci` 한 번이 곧 임의 코드 실행이 되는 자리다."""
    assert "--ignore-scripts" in BUILD.read_text(encoding="utf-8")


def test_build_and_test_run_without_network():
    """테스트가 인터넷에 나가면 재현되지 않고, 그 통로로 우리 서버 정보가 나간다."""
    script = BUILD.read_text(encoding="utf-8")
    at_build = script.index("run_stage build")
    closed = script.rindex("NET=(--network=none)", 0, at_build)

    assert closed < at_build, "빌드·테스트 전에 네트워크를 닫지 않는다"


def test_the_builder_never_accepts_a_dockerfile():
    """사용자 Dockerfile 은 「이번 단계에서 하지 않는 것」 목록에 있다."""
    script = BUILD.read_text(encoding="utf-8")

    assert "Containerfile" in script, "우리가 쓴 것을 써야 한다"
    assert "$SRC/Dockerfile" not in script
    assert "--file \"$SRC" not in script


def test_the_candidate_tag_is_removed():
    """태그가 남으면 누군가 그것으로 실행하고, 그 순간 승인한 것과 도는 것이
    갈린다."""
    assert "untag" in BUILD.read_text(encoding="utf-8")


# --- 배포 helper --------------------------------------------------------------
@needs_bash
@pytest.mark.parametrize("action", ["run", "exec", "", "restart"])
def test_deploy_refuses_unknown_actions(action):
    got = _run(DEPLOY, action, "hermes")

    assert got.returncode != 0
    assert "허용된 동작" in got.stderr


@needs_bash
@pytest.mark.parametrize("digest", [
    "latest",
    "sha256:short",
    "sha256:" + "g" * 64,
    "; rm -rf /",
])
def test_deploy_refuses_a_bad_digest(digest):
    """태그는 움직인다. 움직이는 것을 배포하면 승인한 것과 도는 것이 갈린다."""
    got = _run(DEPLOY, "stage", "hermes", digest)

    assert got.returncode != 0


@needs_bash
def test_deploy_refuses_a_bad_key():
    got = _run(DEPLOY, "stage", "../etc", "sha256:" + "a" * 64)

    assert got.returncode != 0
    assert "key 형식" in got.stderr


def test_staging_never_touches_the_running_deployment():
    """후보 실패가 현재 active 를 내리면, 새 버전을 시험할 때마다 서비스가 끊긴다."""
    script = DEPLOY.read_text(encoding="utf-8")
    stage = script[script.index("stage)"):script.index("activate)")]

    assert "기존 배포를 유지" in stage
    assert "smoke" in stage


def test_disable_says_the_db_already_closed_routing():
    """컨테이너 정리를 먼저 하면 그것이 실패했을 때 라우팅이 열린 채 남는다."""
    script = DEPLOY.read_text(encoding="utf-8")
    disable = script[script.index("disable)"):script.index("rollback)")]

    assert "DB 는 이미" in disable


def test_the_image_only_enters_through_a_dropin():
    """unit 본문을 만들지 않는 이유와 같다 — 만들면 그 내용이 곧 systemd
    설정이 된다."""
    script = DEPLOY.read_text(encoding="utf-8")

    assert "image.conf" in script
    assert "systemctl daemon-reload" in script
    # unit 파일 자체를 쓰지 않는다.
    assert "tybot-specialist@.service <<" not in script


def test_the_dropin_is_written_atomically():
    """반쯤 쓰인 drop-in 을 systemd 가 읽으면 기동이 실패한다."""
    script = DEPLOY.read_text(encoding="utf-8")

    assert ".image.conf.new" in script
    assert "mv -f" in script


def test_both_helpers_are_installed_as_root():
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    for name in ("tybot-specialist-build", "tybot-specialist-deploy"):
        assert f"-o root -g root -m 0755 \"$APP_DIR/deploy/{name}\"" in install, name


# --- 코드가 아는 값을 DB 가 거부하면 안 된다 (2026-09-11) --------------------
def test_the_schema_allows_every_execution_mode_the_code_knows():
    """`UPDATE ... execution_mode='tools'` 가 제약 위반으로 막혔다.

    스키마를 안 올린 설치에서 나는 일이고, 오류 문구는 「제약 위반」 이라
    **무엇을 적용해야 하는지 말해 주지 않는다.** 목록이 갈리는 것을 여기서 막는다.
    """
    sql = (ROOT / "deploy" / "sql" / "specialist_runtime_schema.sql").read_text(
        encoding="utf-8"
    )
    line = next(
        row for row in sql.splitlines() if "execution_mode IN" in row
    )

    for mode in store.EXECUTION_MODES:
        assert f"'{mode}'" in line, f"스키마가 {mode} 를 거부한다"


def test_the_adapter_factory_knows_the_same_modes():
    """DB 가 받아 주는데 코드가 모르면, 등록은 되고 동작은 프롬프트로 내려간다."""
    import inspect

    from tybot import specialist_adapters

    source = inspect.getsource(specialist_adapters.build)

    # `prompt` 는 기본값이라 분기가 없어도 된다. 나머지는 이름이 보여야 한다.
    assert '"tools"' in source


def test_the_constraint_is_replaced_not_added():
    """`ADD CONSTRAINT` 만 있으면 이미 있는 설치에서 적용이 실패하고, 그러면
    아무도 다시 적용하지 않는다."""
    sql = (ROOT / "deploy" / "sql" / "specialist_runtime_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "DROP CONSTRAINT IF EXISTS specialist_bot_execution_mode" in sql
