"""VIPBot `main()` 의 **조립 순서**와 중복 기동 잠금 (B-83). 네트워크 0회.

조각마다 시험이 있어도 `main()` 은 따로 봐야 한다. 조각은 전부 초록인데
**부르는 순서만** 틀릴 수 있고, 그 틀림은 조각 시험에 걸리지 않는다.

제일 비싼 어긋남은 신원 확인이 손잡이 뒤로 가는 것이다 — 틀린 토큰으로 **남의
워크스페이스에 붙은 다음** 알아차린다. 그래서 순서를 목록으로 박는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tybot import vipbot_runtime as runtime

WS = "pilot"
TEAM = "T0VIPTEAM"
BOT = "U0BOT0001"
KEY = "sk" + "-ant-" + "synthetic-vipbot-boot-000000000000"

#: `main()` 이 불러야 하는 순서. 바뀌면 이 목록을 **먼저** 고쳐야 한다.
ORDER = [
    "settings",
    "credential",
    "database",
    "claims",
    "engine",
    "app",
    "identity",
    "register",
    "handler",
]


def settings(tmp_path):
    """`load_settings` 를 **부르지 않고** 만든다 — 대역이 그 이름을 차지한다."""
    from tybot.vip_profile import load_profile

    return runtime.RuntimeSettings(
        workspace=WS,
        archive_dir=Path(tmp_path),
        profile=load_profile({"VIPBOT_DAILY_LIMIT_USD": "12.50"}),
    )


def connection() -> dict:
    return {
        "team_id": TEAM,
        "bot_user_id": BOT,
        "bot_token": "synthetic-bot-token",
        "app_token": "synthetic-app-token",
    }


def code(call) -> str:
    with pytest.raises(runtime.VipbotRuntimeError) as caught:
        call()
    return caught.value.code


class App:
    client = object()


class Handler:
    def __init__(self, start=None):
        self.started = 0
        self.closed = 0
        self._start = start

    def start(self):
        self.started += 1
        if self._start is not None:
            raise self._start

    def close(self):
        self.closed += 1


class Recorder:
    """`main()` 이 무엇을 어떤 순서로 불렀는지 적는 대역 묶음."""

    def __init__(self, tmp_path, *, fail_at=None, error=None, start=None):
        self.steps: list[str] = []
        self.tmp_path = tmp_path
        self.fail_at = fail_at
        self.error = error or runtime.VipbotRuntimeError(runtime.IDENTITY_MISMATCH)
        self.handler = Handler(start=start)

    def _step(self, name, value=None):
        self.steps.append(name)
        if self.fail_at == name:
            raise self.error
        return value

    def load_settings(self):
        return self._step("settings", settings(self.tmp_path))

    def resolve_anthropic_api_key(self):
        return self._step("credential", KEY)

    def load_connection(self, _workspace):
        return self._step("database", connection())

    def claims(self, _path):
        return self._step("claims", object())

    def build_engine(self, **_kwargs):
        return self._step("engine", object())

    def build_app(self, _config):
        return self._step("app", App())

    def verify_identity(self, _client, _config):
        return self._step("identity", {"team_id": TEAM, "bot_user_id": BOT})

    def register(self, _app, **_kwargs):
        return self._step("register")

    def socket_handler(self, _app, _token):
        self._step("handler")
        return self.handler


def boot(monkeypatch, tmp_path, recorder: Recorder) -> Recorder:
    """`main()` 의 바깥 세계를 전부 대역으로 바꾼다 — Slack·DB·LLM 0회."""
    monkeypatch.setattr(runtime, "load_settings", recorder.load_settings)
    monkeypatch.setattr(
        runtime, "resolve_anthropic_api_key", recorder.resolve_anthropic_api_key
    )
    monkeypatch.setattr(runtime, "load_connection", recorder.load_connection)
    monkeypatch.setattr(
        runtime, "state_path", lambda: tmp_path / "slack-events.sqlite3"
    )
    monkeypatch.setattr(runtime, "EventClaims", recorder.claims)
    monkeypatch.setattr(runtime, "build_engine", recorder.build_engine)
    monkeypatch.setattr(runtime, "build_app", recorder.build_app)
    monkeypatch.setattr(runtime, "verify_identity", recorder.verify_identity)
    monkeypatch.setattr(runtime, "register", recorder.register)
    monkeypatch.setattr(runtime, "_socket_handler", recorder.socket_handler)
    return recorder


# --- 1. 정상 기동 순서 ---------------------------------------------------------


def test_main_assembles_in_the_order_the_security_depends_on(monkeypatch, tmp_path):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path))

    assert runtime.main() == 0

    assert recorder.steps == ORDER
    assert recorder.handler.started == 1
    assert recorder.handler.closed == 1


def test_identity_is_checked_before_the_socket_and_settings_before_everything(
    monkeypatch, tmp_path
):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path))
    runtime.main()

    place = recorder.steps.index
    assert place("settings") == 0
    assert place("identity") < place("handler")
    assert place("database") < place("engine")
    assert place("app") < place("identity")


# --- 2. 설정·신원 실패에서는 손잡이가 안 선다 -----------------------------------


@pytest.mark.parametrize(
    "fail_at",
    ["settings", "credential", "database", "claims", "engine", "app", "identity", "register"],
)
def test_a_failure_anywhere_before_the_handler_never_starts_it(
    monkeypatch, tmp_path, fail_at
):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path, fail_at=fail_at))

    with pytest.raises(runtime.VipbotRuntimeError):
        runtime.main()

    assert recorder.steps == [*ORDER[: ORDER.index(fail_at)], fail_at]
    assert "handler" not in recorder.steps
    assert recorder.handler.started == 0
    assert recorder.handler.closed == 0


def test_an_identity_mismatch_stops_before_a_single_event_can_arrive(
    monkeypatch, tmp_path
):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path, fail_at="identity"))

    assert code(runtime.main) == runtime.IDENTITY_MISMATCH
    # 핸들러 등록조차 안 한다 — 붙었다면 이벤트가 들어왔을 자리다.
    assert "register" not in recorder.steps


def test_a_settings_failure_leaves_the_lock_free(monkeypatch, tmp_path):
    boot(monkeypatch, tmp_path, Recorder(tmp_path, fail_at="settings"))
    with pytest.raises(runtime.VipbotRuntimeError):
        runtime.main()
    with runtime.single_instance(WS, directory=tmp_path):
        pass


# --- 3. 손잡이 예외에서 자원 해제 ----------------------------------------------


def test_a_handler_failure_closes_the_socket_and_releases_the_lock(
    monkeypatch, tmp_path
):
    recorder = boot(
        monkeypatch, tmp_path, Recorder(tmp_path, start=RuntimeError("socket died"))
    )

    with pytest.raises(RuntimeError, match="socket died"):
        runtime.main()

    assert recorder.handler.started == 1
    assert recorder.handler.closed == 1
    with runtime.single_instance(WS, directory=tmp_path):
        pass  # 잠금이 풀려 있다


def test_a_close_failure_does_not_hide_the_real_error(monkeypatch, tmp_path):
    """닫다가 난 오류가 **원래 오류를 가리면** 원인을 영영 못 본다."""
    recorder = boot(
        monkeypatch, tmp_path, Recorder(tmp_path, start=RuntimeError("socket died"))
    )

    def broken_close():
        raise OSError("close blew up")

    recorder.handler.close = broken_close

    with pytest.raises(RuntimeError, match="socket died"):
        runtime.main()
    with runtime.single_instance(WS, directory=tmp_path):
        pass


def test_a_handler_without_close_is_not_a_crash(monkeypatch, tmp_path):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path))
    recorder.handler.close = None

    assert runtime.main() == 0


def test_a_clean_stop_also_closes_and_releases(monkeypatch, tmp_path):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path))
    assert runtime.main() == 0
    assert recorder.handler.closed == 1
    with runtime.single_instance(WS, directory=tmp_path):
        pass


def test_main_never_reaches_slack_when_the_handler_is_doubled(monkeypatch, tmp_path):
    """`_socket_handler` 가 유일한 Slack 접점이다 — 대역이면 import 도 안 난다."""
    boot(monkeypatch, tmp_path, Recorder(tmp_path))
    monkeypatch.setitem(sys.modules, "slack_bolt", None)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", None)

    assert runtime.main() == 0


# --- 4. 같은 워크스페이스 중복 기동 --------------------------------------------


def test_a_second_runtime_for_the_same_workspace_is_refused(tmp_path):
    """봇이 둘이면 같은 멘션에 **두 번 답하고** 모델 비용도 두 배다."""
    with runtime.single_instance(WS, directory=tmp_path):
        with pytest.raises(runtime.VipbotRuntimeError) as caught:
            with runtime.single_instance(WS, directory=tmp_path):
                raise AssertionError("두 번째 기동이 통과했다")
    assert caught.value.code == runtime.ALREADY_RUNNING


def test_main_takes_the_lock_before_touching_the_database(monkeypatch, tmp_path):
    recorder = boot(monkeypatch, tmp_path, Recorder(tmp_path))

    with runtime.single_instance(WS, directory=tmp_path):
        assert code(runtime.main) == runtime.ALREADY_RUNNING

    # 설정만 읽고 끝났다 — DB 도 Slack 도 자격증명도 안 건드렸다.
    assert recorder.steps == ["settings"]


def test_the_lock_is_released_when_the_first_runtime_exits(tmp_path):
    with runtime.single_instance(WS, directory=tmp_path):
        pass
    with runtime.single_instance(WS, directory=tmp_path):
        pass


def test_the_lock_is_released_even_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError, match="boom"):
        with runtime.single_instance(WS, directory=tmp_path):
            raise RuntimeError("boom")
    with runtime.single_instance(WS, directory=tmp_path):
        pass


def test_another_workspace_is_not_blocked(tmp_path):
    with runtime.single_instance(WS, directory=tmp_path):
        with runtime.single_instance("mgmt", directory=tmp_path):
            pass


def test_the_lock_lives_under_the_state_directory_and_names_the_workspace(tmp_path):
    with runtime.single_instance(WS, directory=tmp_path) as path:
        assert path == tmp_path / f"vipbot-{WS}.lock"
        assert path.exists()
    # 잠긴 동안에는 읽지 않는다 — Windows 바이트 잠금은 읽기도 막는다.
    assert path.read_text(encoding="utf-8").strip().isdigit()


def test_the_lock_directory_comes_from_the_validated_state_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime, "state_path", lambda: tmp_path / "slack-events.sqlite3"
    )
    assert runtime._lock_file(WS) == tmp_path / f"vipbot-{WS}.lock"


def test_an_unusable_lock_directory_is_a_fixed_code(tmp_path):
    missing = tmp_path / "gone"
    with pytest.raises(runtime.VipbotRuntimeError) as caught:
        with runtime.single_instance(WS, directory=missing):
            pass
    assert caught.value.code == runtime.LOCK_UNAVAILABLE


def test_the_lock_is_not_the_event_claim_database(tmp_path):
    """같은 디렉터리를 쓰지만 **다른 파일**이다. 잠금이 DB 를 잠그면 안 된다."""
    with runtime.single_instance(WS, directory=tmp_path) as path:
        assert path.name.endswith(".lock")
        assert "sqlite" not in path.name
        assert not (tmp_path / "slack-events.sqlite3").exists()


def test_the_lock_never_carries_a_token_or_a_question(tmp_path):
    with runtime.single_instance(WS, directory=tmp_path) as path:
        pass
    body = path.read_text(encoding="utf-8")
    assert "synthetic-bot-token" not in body
    assert "synthetic-app-token" not in body
    assert len(body.strip().splitlines()) == 1


def test_the_lock_helpers_live_in_the_runtime_not_in_a_new_dependency():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "import fcntl" in source and "import msvcrt" in source
    # 플랫폼 모듈은 **함수 안에서만** 들여온다 — 반대쪽 OS 에서 import 가 터진다.
    module_level = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    assert not any("fcntl" in line or "msvcrt" in line for line in module_level)
