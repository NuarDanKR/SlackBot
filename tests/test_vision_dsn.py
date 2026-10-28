"""Vision 전용 DSN 해석. **폴백이 없다는 것**과 **아무것도 새지 않는다는 것**을 건다."""
from __future__ import annotations

import logging
import os
import traceback

import pytest

from tybot.console import vision_dsn

#: 시험에서만 쓰는 표식. 실제 시크릿이 아니고, 샜는지 세는 용도다.
MARKER = "PASSWORD-PLACEHOLDER-NOT-A-SECRET"
VISION_URL = f"postgresql://tybot_vision_runtime:{MARKER}@localhost:55432/tyslackai"
MASTER_URL = "postgresql://tyslackai@localhost:55432/tyslackai"

ENV = (
    "TYBOT_VISION_DATABASE_URL",
    "TYBOT_VISION_DATABASE_URL_FILE",
    "CREDENTIALS_DIRECTORY",
    "DATABASE_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """개발 PC 의 환경이 판정을 바꾸면 안 된다."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def private(path, text: str = VISION_URL):
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def code_of(call) -> str:
    with pytest.raises(vision_dsn.VisionDsnError) as error:
        call()
    return error.value.code


# --- 어디서 읽나 -------------------------------------------------------------


def test_the_direct_value_is_used(monkeypatch):
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", VISION_URL)
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


def test_surrounding_space_is_trimmed(monkeypatch):
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", f"  {VISION_URL}\n")
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


def test_an_explicit_file_is_read(monkeypatch, tmp_path):
    path = private(tmp_path / "dsn")
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


def test_the_systemd_credential_directory_is_read(monkeypatch, tmp_path):
    private(tmp_path / vision_dsn.CREDENTIAL_NAME)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


def test_the_credential_name_matches_the_deploy_script():
    """철자가 갈라지면 운영자가 넣은 파일을 코드가 못 찾는다."""
    script = (
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        + "/deploy/setup-tybot-vision-runtime.sh"
    )
    with open(script, encoding="utf-8") as handle:
        assert vision_dsn.CREDENTIAL_NAME in handle.read()


def test_the_direct_value_wins_over_a_file(monkeypatch, tmp_path):
    path = private(tmp_path / "dsn", "postgresql://other@localhost/db")
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", VISION_URL)
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


def test_an_explicit_file_wins_over_the_credential_directory(monkeypatch, tmp_path):
    chosen = private(tmp_path / "chosen", VISION_URL)
    private(tmp_path / vision_dsn.CREDENTIAL_NAME, "postgresql://other@localhost/db")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(chosen))
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


# --- 폴백이 없다 -------------------------------------------------------------


def test_nothing_configured_stays_unconfigured():
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNCONFIGURED


def test_a_master_database_url_is_never_a_fallback(monkeypatch):
    """일반 `DATABASE_URL` 이 있어도 Vision 설정이 없으면 미설정이다."""
    monkeypatch.setenv("DATABASE_URL", MASTER_URL)
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNCONFIGURED


def test_copying_the_master_dsn_is_refused(monkeypatch):
    """접속은 성공한다. 그래서 화면은 정상으로 보이고 분리는 이미 사라졌다."""
    monkeypatch.setenv("DATABASE_URL", MASTER_URL)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", MASTER_URL)
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.SHARED


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://postgres@example/db",
        "postgresql://tyslackai@example/db",
        "postgresql://tybot_vision@example/db",
        "dbname=tyslackai user=tybot_vision_runtime",
        "not-a-dsn",
    ],
)
def test_only_the_dedicated_runtime_login_is_accepted(monkeypatch, value):
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", value)
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.WRONG_ROLE


def test_a_percent_encoded_runtime_login_is_parsed_structurally(monkeypatch):
    monkeypatch.setenv(
        "TYBOT_VISION_DATABASE_URL",
        "postgresql://tybot%5Fvision%5Fruntime:secret@example/db",
    )
    assert vision_dsn.resolve_vision_database_url().startswith("postgresql://")


def test_a_query_string_user_that_overrides_the_userinfo_is_refused(monkeypatch):
    """libpq 는 URI 를 키워드로 펼칠 때 질의 문자열로 userinfo 를 **덮어쓴다.**

    userinfo 만 보면 이 DSN 이 통과한다. 실제 접속은 `postgres` 로 간다
    (2026-10-28 실측: 통과했다).
    """
    monkeypatch.setenv(
        "TYBOT_VISION_DATABASE_URL",
        f"postgresql://{vision_dsn.RUNTIME_ROLE}@example/db?user=postgres",
    )
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.WRONG_ROLE


def test_the_last_user_parameter_wins_like_libpq(monkeypatch):
    monkeypatch.setenv(
        "TYBOT_VISION_DATABASE_URL",
        f"postgresql://example/db?user={vision_dsn.RUNTIME_ROLE}&user=postgres",
    )
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.WRONG_ROLE


@pytest.mark.parametrize("value", [
    "postgresql://example/db?user=tybot_vision_runtime",
    "postgresql://postgres@example/db?user=tybot_vision_runtime",
    "postgresql://example/db?user=tybot%5Fvision%5Fruntime",
    "postgresql://tybot_vision_runtime@/tyslackai?host=/run/postgresql&port=55432",
])
def test_a_query_string_user_that_names_the_runtime_is_accepted(monkeypatch, value):
    """덮어쓴 값이 **맞는** 계정이면 접속도 그 계정으로 간다. 거절할 이유가 없다."""
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", value)
    assert vision_dsn.resolve_vision_database_url() == value


@pytest.mark.parametrize("value", [
    "postgresql://user@[oops/db",
    "postgresql://tybot_vision_runtime@[::1:9999/db",
    "://",
    "postgresql://",
])
def test_a_dsn_we_cannot_parse_is_refused_not_crashed(monkeypatch, value):
    """파싱 실패를 예외로 흘리면 콘솔이 500 을 낸다. 거절 코드여야 한다."""
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", value)
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.WRONG_ROLE


def test_the_plan_dsn_example_is_one_the_console_accepts(monkeypatch):
    """설치 안내가 보여 주는 형식이 실제로 통과해야 한다 — 아니면 운영자가
    안내대로 적고 503 을 본다."""
    import pathlib
    import re
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash 를 찾지 못했다 — 설치 안내를 실행해 볼 수 없다")
    script = (
        pathlib.Path(__file__).resolve().parents[1]
        / "deploy" / "setup-tybot-vision-runtime.sh"
    )
    said = subprocess.run(
        [bash, str(script), "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if said.returncode != 0:
        pytest.skip("bash 실행 환경을 사용할 수 없다")
    said = said.stdout
    example = re.search(r"^\s*(postgresql://\S+)", said, re.MULTILINE)
    assert example, said
    monkeypatch.setenv(
        "TYBOT_VISION_DATABASE_URL", example.group(1).replace("<비밀번호>", MARKER)
    )
    assert vision_dsn.resolve_vision_database_url()


def test_the_same_value_from_a_file_is_also_refused(monkeypatch, tmp_path):
    path = private(tmp_path / "dsn", MASTER_URL)
    monkeypatch.setenv("DATABASE_URL", MASTER_URL)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.SHARED


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_a_blank_value_is_not_a_configuration(monkeypatch, value):
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", value)
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNCONFIGURED


@pytest.mark.parametrize("value", ["", "   \n"])
def test_a_blank_file_is_unreadable_not_unconfigured(monkeypatch, tmp_path, value):
    """운영자가 파일을 만들어 두고 비워 둔 것은 **고칠 자리가 있는** 상태다."""
    path = private(tmp_path / "dsn", value)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNREADABLE


def test_an_explicit_file_that_is_missing_is_a_misconfiguration(monkeypatch, tmp_path):
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(tmp_path / "none"))
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNREADABLE


def test_an_absent_default_credential_is_merely_unconfigured(monkeypatch, tmp_path):
    """systemd 는 `CREDENTIALS_DIRECTORY` 를 늘 준다. 그 안에 우리 파일이 없는
    것은 아직 안 넣었다는 뜻이지 설정이 틀렸다는 뜻이 아니다."""
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.UNCONFIGURED


# --- 파일 권한 ---------------------------------------------------------------

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX 권한 비트는 Windows 에서 뜻을 갖지 않는다 — 여기서 재면 개발 PC 에서만 빨개진다",
)


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o644])
def test_a_group_or_other_readable_file_is_refused(monkeypatch, tmp_path, mode):
    path = tmp_path / "dsn"
    path.write_text(VISION_URL, encoding="utf-8")
    path.chmod(mode)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert code_of(vision_dsn.resolve_vision_database_url) == vision_dsn.BAD_MODE


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_an_owner_only_file_is_accepted(monkeypatch, tmp_path, mode):
    path = tmp_path / "dsn"
    path.write_text(VISION_URL, encoding="utf-8")
    path.chmod(mode)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


@pytest.mark.skipif(os.name == "posix", reason="Windows 쪽 분기를 본다")
def test_windows_skips_the_mode_check_instead_of_failing(monkeypatch, tmp_path):
    """개발 PC 에서 권한 검사가 돌면 **여기서만** 빨개진다. 분리해 둔다."""
    path = tmp_path / "dsn"
    path.write_text(VISION_URL, encoding="utf-8")
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    assert vision_dsn.resolve_vision_database_url() == VISION_URL


# --- 아무것도 새지 않는다 ----------------------------------------------------


def _leaky(tmp_path):
    """거절되는 경로를 만든다. **경로 이름에 표식을 넣는다** — 경로가 새면
    값이 새는 것과 똑같이 잡히고, 플랫폼 권한 비트에 기대지 않는다."""
    path = tmp_path / f"{MARKER}-dsn"
    if os.name == "posix":
        path.write_text(VISION_URL, encoding="utf-8")
        path.chmod(0o666)  # 거절되는 권한
    # Windows 에서는 파일을 두지 않는다 — 명시 경로가 없으면 UNREADABLE 이다.
    return path


def test_the_error_carries_only_a_code(monkeypatch, tmp_path):
    path = _leaky(tmp_path)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    with pytest.raises(vision_dsn.VisionDsnError) as error:
        vision_dsn.resolve_vision_database_url()
    said = str(error.value) + repr(error.value) + repr(error.value.args)
    assert MARKER not in said
    assert error.value.code.isascii()


def test_the_traceback_does_not_carry_the_path(monkeypatch, tmp_path):
    """`OSError` 의 문자열에 경로가 들어 있다. `from None` 으로 끊는 이유다."""
    path = _leaky(tmp_path)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    with pytest.raises(vision_dsn.VisionDsnError) as error:
        vision_dsn.resolve_vision_database_url()
    text = "".join(
        traceback.format_exception(
            type(error.value), error.value, error.value.__traceback__
        )
    )
    assert MARKER not in text
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_nothing_is_logged(monkeypatch, tmp_path, caplog):
    path = _leaky(tmp_path)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(vision_dsn.VisionDsnError):
            vision_dsn.resolve_vision_database_url()
        monkeypatch.setenv("TYBOT_VISION_DATABASE_URL", VISION_URL)
        vision_dsn.resolve_vision_database_url()
    assert caplog.records == []


def test_the_module_has_no_logger_to_leak_into():
    """남길 만한 것이 값과 경로뿐이라 로거를 두지 않는다."""
    import inspect

    source = inspect.getsource(vision_dsn)
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in code if "logging" in line or "print(" in line]


def test_nothing_prints(monkeypatch, tmp_path, capsys):
    path = _leaky(tmp_path)
    monkeypatch.setenv("TYBOT_VISION_DATABASE_URL_FILE", str(path))
    with pytest.raises(vision_dsn.VisionDsnError):
        vision_dsn.resolve_vision_database_url()
    said = capsys.readouterr()
    assert (said.out, said.err) == ("", "")


# --- 콘솔 경계 ---------------------------------------------------------------


def test_the_console_refuses_instead_of_falling_back(monkeypatch):
    """`image_description_admin` 이 일반 `DATABASE_URL` 로 내려가면 안 된다."""
    from tybot.console import image_description_admin as admin

    monkeypatch.setenv("DATABASE_URL", MASTER_URL)
    with pytest.raises(admin.ImageDescriptionAdminError) as error:
        admin.snapshot("tyit")
    assert str(error.value) == vision_dsn.UNCONFIGURED


@pytest.mark.parametrize(
    "code", [vision_dsn.BAD_MODE, vision_dsn.UNREADABLE, vision_dsn.SHARED]
)
def test_the_console_passes_the_code_through_without_the_cause(monkeypatch, code):
    from tybot.console import image_description_admin as admin

    def refuse():
        raise vision_dsn.VisionDsnError(code)

    monkeypatch.setattr(admin, "resolve_vision_database_url", refuse)
    with pytest.raises(admin.ImageDescriptionAdminError) as error:
        admin.snapshot("tyit")
    assert str(error.value) == code
    # 콘솔이 돌려주는 코드는 `_safe_code` 를 지나므로 ASCII 소문자여야 한다.
    assert code == code.lower() and code.replace("_", "").isalnum()
