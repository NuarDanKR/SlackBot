"""단일 supervisor — **한 프로세스, 여러 워크스페이스, Socket 은 앱 단위.**

결정: 2026-09-29 4단계 지시 1·5.

여기서 막는 것.

1. **같은 앱에 Socket 을 두 개 여는 것.** Slack 이 이벤트를 아무 쪽에나 보내고,
   한쪽 워크스페이스는 조용히 빈다
2. **모르는 team 의 이벤트를 저장하는 것.** 남의 대화가 우리 아카이브에 들어간다
3. **한 워크스페이스 장애가 전체를 멈추는 것.** 다른 본부가 자기와 무관한 이유로
   자료를 잃는다
4. **같은 Bot Token 으로 뜨는 것.** 한쪽 토큰이 다른 연결에 들어가 있으면 남의
   워크스페이스에 수집한다 — 오류가 아니라 유출이다
5. **오래된 세대 설정을 적용하는 것.** 콘솔에서 끈 워크스페이스가 되살아난다

Slack 연결을 열지 않는다. 규칙만 본다.
"""

from __future__ import annotations

import pytest

from tybot.archive import supervisor as sv
from tybot.archive.supervisor_state import DesiredMode, ObservedState, WorkspaceRuntime
from tybot.archive.token_topology import Connection

APP_A = "xapp-" + "a" * 30
APP_B = "xapp-" + "b" * 30
BOT_1 = "xoxb-" + "1" * 30
BOT_2 = "xoxb-" + "2" * 30
BOT_3 = "xoxb-" + "3" * 30


def _conn(workspace: str, team: str, *, app: str = APP_A, bot: str = BOT_1) -> Connection:
    return Connection(
        workspace=workspace, bot_key="archiver", bot_token=bot, app_token=app,
        state="enabled", team_id=team,
    )


def _supervisor(*connections: Connection) -> sv.Supervisor:
    return sv.Supervisor(workers=sv.plan_workers(list(connections)))


# --- Socket 묶음 -----------------------------------------------------------------

def test_one_app_token_means_one_socket():
    """**이 시험이 supervisor 의 이유다.** 둘을 열면 Slack 이 아무 쪽에나 보낸다."""
    workers = sv.plan_workers([
        _conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2),
    ])

    assert len(workers) == 1
    assert sorted(b.workspace for b in workers[0].bindings.values()) == ["mgmt", "tyit"]


def test_two_app_tokens_mean_two_workers_in_one_process():
    """프로세스를 나누지 않는다. 워크스페이스 추가가 곧 서버 작업이 되면 안 된다."""
    workers = sv.plan_workers([
        _conn("tyit", "T1", app=APP_A, bot=BOT_1),
        _conn("pf", "T2", app=APP_B, bot=BOT_2),
    ])

    assert len(workers) == 2
    assert {len(w.bindings) for w in workers} == {1}


def test_a_connection_without_tokens_refuses_startup():
    """토큰 없는 연결로 뜨면 그 워크스페이스만 조용히 빈다."""
    with pytest.raises(sv.StartupRefused, match="토큰 쌍"):
        sv.plan_workers([Connection("tyit", "archiver", "", "", team_id="T1")])


def test_a_connection_without_identity_refuses_startup():
    """team 을 모르면 이벤트를 배분할 수 없다. 뜨고 나서 알면 이미 늦다."""
    with pytest.raises(sv.StartupRefused, match="team"):
        sv.plan_workers([_conn("tyit", "")])


def test_the_same_bot_token_in_two_workspaces_refuses_startup():
    """오류가 아니라 유출이다. 뜨기 전에 막는다."""
    with pytest.raises(sv.StartupRefused, match="남의 대화"):
        sv.plan_workers([_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_1)])


def test_the_same_team_twice_on_one_app_refuses_startup():
    """이벤트를 어디로 보낼지 정할 수 없다. 추측하면 그 추측이 조용히 틀린다."""
    with pytest.raises(sv.StartupRefused, match="team T1"):
        sv.plan_workers([_conn("tyit", "T1", bot=BOT_1), _conn("copy", "T1", bot=BOT_2)])


def test_the_same_team_on_different_apps_is_fine():
    """다른 Slack 앱이면 Socket 이 다르므로 섞이지 않는다."""
    workers = sv.plan_workers([
        _conn("tyit", "T1", app=APP_A, bot=BOT_1),
        _conn("tyit-2", "T1", app=APP_B, bot=BOT_2),
    ])

    assert len(workers) == 2


def test_a_worker_does_not_print_its_tokens():
    """로그 한 줄이 토큰을 남기면, 로그는 감사보다 오래 남는다."""
    workers = sv.plan_workers([_conn("tyit", "T1")])

    assert APP_A not in repr(workers[0]) and BOT_1 not in repr(workers[0])
    assert BOT_1 not in repr(workers[0].bindings["T1"])


# --- 이벤트 배분 -----------------------------------------------------------------

def test_an_event_goes_to_the_workspace_that_owns_the_team():
    supervisor = _supervisor(_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2))
    seen: list[str] = []

    result = supervisor.dispatch(
        supervisor.workers[0].socket_key, {"team_id": "T2", "ts": "1.1"},
        handler=lambda binding, _event: seen.append(binding.workspace) or "written",
    )

    assert result == "written" and seen == ["mgmt"]


def test_an_unknown_team_is_not_stored():
    """추측해서 보내면 그 대화가 남의 아카이브에 들어가고, 되돌릴 수 없다."""
    supervisor = _supervisor(_conn("tyit", "T1"))
    called: list[str] = []

    result = supervisor.dispatch(
        supervisor.workers[0].socket_key, {"team_id": "T-UNKNOWN"},
        handler=lambda *_: called.append("x") or "written",
        unknown_team=called.append,
    )

    assert result == "skipped-unknown-team"
    assert called == ["T-UNKNOWN"], "핸들러는 안 불린다"


def test_an_event_without_a_team_is_not_stored():
    supervisor = _supervisor(_conn("tyit", "T1"))

    result = supervisor.dispatch(
        supervisor.workers[0].socket_key, {"ts": "1.1"},
        handler=lambda *_: "written",
    )

    assert result == "skipped-unknown-team"


def test_an_event_on_an_unknown_socket_is_not_stored():
    supervisor = _supervisor(_conn("tyit", "T1"))

    result = supervisor.dispatch(
        "some-other-socket", {"team_id": "T1"}, handler=lambda *_: "written",
    )

    assert result == "skipped-unknown-team"


def test_a_workspace_switched_off_drops_the_event():
    """사람이 껐다. worker 가 멈추는 중일 수 있으므로 상태를 오류로 만들지 않는다."""
    supervisor = _supervisor(_conn("tyit", "T1"))
    supervisor.runtimes["tyit"] = WorkspaceRuntime("tyit", desired_mode=DesiredMode.OFF)

    result = supervisor.dispatch(
        supervisor.workers[0].socket_key, {"team_id": "T1"},
        handler=lambda *_: "written",
    )

    assert result == "skipped-desired-off"
    assert supervisor.runtimes["tyit"].observed_state is ObservedState.STOPPED


# --- 장애 격리 -------------------------------------------------------------------

def test_one_workspace_failure_does_not_stop_the_others():
    """다른 본부가 자기와 무관한 이유로 자료를 잃으면 안 된다."""
    supervisor = _supervisor(_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2))
    socket = supervisor.workers[0].socket_key

    def handler(binding, _event):
        if binding.workspace == "tyit":
            raise RuntimeError("disk full")
        return "written"

    failed = supervisor.dispatch(socket, {"team_id": "T1"}, handler=handler)
    healthy = supervisor.dispatch(socket, {"team_id": "T2"}, handler=handler)

    assert failed == "failed" and healthy == "written"
    assert supervisor.runtimes["tyit"].observed_state is ObservedState.ERROR
    assert supervisor.runtimes["tyit"].error_code == "RuntimeError"
    assert "mgmt" not in supervisor.runtimes, "멀쩡한 쪽 상태는 안 건드린다"


def test_the_failure_reason_carries_no_message_body():
    """오류 문구에 원문이 섞이면 본문이 로그로 나간다."""
    supervisor = _supervisor(_conn("tyit", "T1"))

    def handler(_binding, _event):
        raise RuntimeError("금액은 1,200만원")

    supervisor.dispatch(
        supervisor.workers[0].socket_key, {"team_id": "T1", "text": "금액은 1,200만원"},
        handler=handler,
    )

    assert supervisor.runtimes["tyit"].error_code == "RuntimeError"
    assert "1,200만원" not in repr(supervisor.runtimes["tyit"])


def test_isolating_one_workspace_leaves_the_rest_alone():
    supervisor = _supervisor(_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2))
    supervisor.observe("mgmt", ObservedState.RUNNING)

    supervisor.isolate("tyit", error_code="slack-auth")

    assert supervisor.runtimes["mgmt"].observed_state is ObservedState.RUNNING
    assert supervisor.runtimes["tyit"].error_code == "slack-auth"


# --- 설정 세대 -------------------------------------------------------------------

def test_a_stale_generation_is_not_applied():
    """느린 worker 가 옛 설정을 적용하면 콘솔에서 끈 워크스페이스가 되살아난다."""
    supervisor = _supervisor(_conn("tyit", "T1"))

    assert supervisor.should_apply("tyit", 5) is True
    assert supervisor.should_apply("tyit", 4) is False


def test_the_same_generation_is_applied_again():
    """재기동 뒤 같은 세대를 다시 읽는 것은 정상이다. 막으면 설정이 안 붙는다."""
    supervisor = _supervisor(_conn("tyit", "T1"))
    supervisor.should_apply("tyit", 5)

    assert supervisor.should_apply("tyit", 5) is True


def test_generations_are_tracked_per_workspace():
    supervisor = _supervisor(_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2))
    supervisor.should_apply("tyit", 9)

    assert supervisor.should_apply("mgmt", 1) is True


# --- 관측 보고 -------------------------------------------------------------------

def test_a_heartbeat_loss_is_observable_as_degraded():
    """heartbeat 가 끊긴 것을 상태로 남기지 않으면 화면이 「정상」 으로 보여 준다."""
    supervisor = _supervisor(_conn("tyit", "T1"))

    runtime = supervisor.observe(
        "tyit", ObservedState.DEGRADED, error_code="heartbeat-lost",
    )

    assert runtime.collecting is True
    assert runtime.error_code == "heartbeat-lost"


def test_a_stopped_worker_reports_stopped():
    supervisor = _supervisor(_conn("tyit", "T1"))
    supervisor.runtimes["tyit"] = WorkspaceRuntime("tyit", desired_mode=DesiredMode.OFF)

    runtime = supervisor.observe("tyit", ObservedState.STOPPED)

    assert runtime.observed_state is ObservedState.STOPPED
    assert runtime.desired_mode is DesiredMode.OFF


def test_bindings_are_listed_by_workspace():
    supervisor = _supervisor(_conn("tyit", "T1", bot=BOT_1), _conn("mgmt", "T2", bot=BOT_2))

    assert sorted(supervisor.bindings()) == ["mgmt", "tyit"]
    assert supervisor.bindings()["tyit"].team_id == "T1"


def test_three_workspaces_on_two_apps_are_routed_correctly():
    """같은 앱 두 워크스페이스 + 다른 앱 하나. 섞이면 남의 아카이브에 들어간다."""
    supervisor = _supervisor(
        _conn("tyit", "T1", app=APP_A, bot=BOT_1),
        _conn("mgmt", "T2", app=APP_A, bot=BOT_2),
        _conn("pf", "T3", app=APP_B, bot=BOT_3),
    )
    shared = next(w for w in supervisor.workers if len(w.bindings) == 2)
    lone = next(w for w in supervisor.workers if len(w.bindings) == 1)

    assert supervisor.route(shared.socket_key, "T2").workspace == "mgmt"
    assert supervisor.route(shared.socket_key, "T3") is None, "다른 앱 team 은 없다"
    assert supervisor.route(lone.socket_key, "T3").workspace == "pf"
