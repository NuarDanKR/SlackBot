"""supervisor 상태 규칙 — **희망과 관측은 다른 값이다.**

결정: 2026-09-29 supervisor 작업지시서 §3.2·§4.2.

여기서 막는 것.

1. **세대를 안 올리고 희망만 바꾸는 것.** 콘솔은 새 값을 보여 주고 프로세스는 옛
   값으로 돈다. 조용하다
2. **「꺼져 있는데 수집 중」 을 저장하는 것.** 둘 중 하나가 거짓인데 화면은 둘 다
   보여 준다
3. **0건과 실패를 같은 결과로 닫는 것.** 아무것도 없었던 것과 못 읽은 것은 사람이
   할 일이 다르다
4. **쓰기 전에 cursor 를 옮기는 것.** 그 구간은 영영 안 메워지고, 다음 실행은 이미
   지났다고 본다

DB 도 Slack 도 없다. 값만 넣고 값만 본다.
"""

from __future__ import annotations

import pytest

from tybot.archive.supervisor_state import (
    DesiredMode,
    JobCounts,
    JobState,
    ObservedState,
    TransitionRefused,
    WorkspaceRuntime,
    advance_cursor,
    finish_state,
    plan_desired_change,
    plan_job_state,
    plan_observed_change,
    stale,
)


def _runtime(**over) -> WorkspaceRuntime:
    base = {"workspace": "tyit"}
    return WorkspaceRuntime(**(base | over))


# --- 희망 상태 -------------------------------------------------------------------

def test_changing_the_desired_mode_bumps_the_generation():
    """세대가 안 오르면 supervisor 가 「바뀐 것 없다」 고 보고 옛 설정으로 돈다."""
    after = plan_desired_change(_runtime(), DesiredMode.SHADOW)

    assert after.desired_mode is DesiredMode.SHADOW
    assert after.generation == 2


def test_setting_the_same_mode_does_not_restart_the_worker():
    """같은 값을 다시 눌렀다고 재시작하면, 누를 때마다 수집이 끊긴다."""
    current = _runtime(desired_mode=DesiredMode.SHADOW, generation=7)

    assert plan_desired_change(current, "shadow").generation == 7


def test_live_is_refused_in_the_pilot():
    """운영 원문의 주인을 바꾸는 일이다. 고를 수 있게 두면 언젠가 눌린다."""
    with pytest.raises(TransitionRefused, match="파일럿 범위 밖"):
        plan_desired_change(_runtime(), DesiredMode.LIVE)


def test_live_is_possible_only_with_an_explicit_opt_in():
    """나중에 여는 문은 남겨 두되, **기본값이 되지 않게** 한다."""
    after = plan_desired_change(_runtime(), DesiredMode.LIVE, allow_live=True)

    assert after.desired_mode is DesiredMode.LIVE


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        plan_desired_change(_runtime(), "sorta-on")


# --- 관측 상태 -------------------------------------------------------------------

def test_switching_off_preserves_running_until_the_worker_stops():
    """중지 요청과 실제 정지 사이의 차이를 저장해야 supervisor가 이를 해소할 수 있다."""
    current = _runtime(
        desired_mode=DesiredMode.SHADOW,
        observed_state=ObservedState.RUNNING,
    )

    after = plan_desired_change(current, DesiredMode.OFF)

    assert after.desired_mode is DesiredMode.OFF
    assert after.observed_state is ObservedState.RUNNING
    assert after.generation == current.generation + 1


def test_running_observation_is_allowed_while_stop_is_pending():
    current = _runtime(
        desired_mode=DesiredMode.OFF,
        observed_state=ObservedState.RUNNING,
    )

    after = plan_observed_change(current, ObservedState.RUNNING)

    assert after.observed_state is ObservedState.RUNNING


def test_a_switched_off_workspace_may_report_stopped():
    after = plan_observed_change(_runtime(), ObservedState.STOPPED)

    assert after.observed_state is ObservedState.STOPPED


def test_an_error_needs_a_reason_code():
    """사유 없는 오류는 화면에서 「빨간 글씨」 로만 보이고, 사람은 서버로 간다."""
    current = _runtime(desired_mode=DesiredMode.SHADOW)

    with pytest.raises(TransitionRefused, match="사유 코드"):
        plan_observed_change(current, ObservedState.ERROR)


def test_a_healthy_state_cannot_carry_an_error_code():
    """정상인데 빨간 글씨가 남으면 무엇을 믿어야 할지 모른다."""
    current = _runtime(desired_mode=DesiredMode.SHADOW)

    with pytest.raises(TransitionRefused, match="오류가 아닌 상태"):
        plan_observed_change(current, ObservedState.RUNNING, error_code="slack-429")


def test_degraded_keeps_its_reason():
    """`degraded` 는 **돌지만 온전치 않다** 다. `error` 와 합치면 그 구분이 사라진다."""
    current = _runtime(desired_mode=DesiredMode.SHADOW)

    after = plan_observed_change(current, ObservedState.DEGRADED, error_code="channel-denied")

    assert after.observed_state is ObservedState.DEGRADED
    assert after.error_code == "channel-denied"
    assert after.collecting is True, "절반이라도 모으고 있다"


def test_an_error_is_not_collecting():
    current = _runtime(desired_mode=DesiredMode.SHADOW)

    after = plan_observed_change(current, ObservedState.ERROR, error_code="db-down")

    assert after.collecting is False


def test_the_generation_survives_an_observation():
    """관측이 세대를 올리면 worker 가 자기 보고 때문에 계속 재시작한다."""
    current = _runtime(desired_mode=DesiredMode.SHADOW, generation=5)

    assert plan_observed_change(current, ObservedState.RUNNING).generation == 5


# --- heartbeat ------------------------------------------------------------------

def test_a_worker_that_never_reported_is_stale():
    """「아직 판단 못 함」 으로 두면 한 번도 안 뜬 worker 가 영원히 정상으로 보인다."""
    assert stale(None) is True


def test_a_recent_heartbeat_is_not_stale():
    assert stale(30) is False


def test_an_old_heartbeat_is_stale():
    assert stale(600) is True


# --- 작업 상태 -------------------------------------------------------------------

def test_finding_nothing_is_a_success():
    """아무것도 없었던 것과 못 읽은 것은 사람이 할 일이 다르다."""
    assert finish_state(JobCounts(), exhausted=True) is JobState.SUCCEEDED


def test_a_failure_without_any_write_is_a_failure():
    assert finish_state(JobCounts(found=3, failed=3), exhausted=True) is JobState.FAILED


def test_some_failures_make_it_partial():
    counts = JobCounts(found=5, written=4, failed=1)

    assert finish_state(counts, exhausted=True) is JobState.PARTIAL


def test_stopping_early_is_partial_even_when_everything_written():
    """남은 구간이 있다는 사실이 건수보다 중요하다."""
    counts = JobCounts(found=10, written=10)

    assert finish_state(counts, exhausted=False) is JobState.PARTIAL


def test_counts_are_kept_apart():
    """하나로 합치면 「500건 처리」 가 400건 중복이었다는 사실을 가린다."""
    counts = JobCounts(found=10, written=4, duplicate=3, refused=2, failed=1)

    assert counts.accounted == 10


def test_a_finished_job_never_runs_again():
    """소급은 되돌릴 수 없는 종류의 일이다. 두 번 돌면 두 번 쓴다."""
    for done in (JobState.SUCCEEDED, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED):
        with pytest.raises(TransitionRefused, match="이미 끝난"):
            plan_job_state(done, JobState.RUNNING)


def test_a_queued_job_can_start_or_be_cancelled():
    assert plan_job_state(JobState.QUEUED, JobState.RUNNING) is JobState.RUNNING
    assert plan_job_state(JobState.QUEUED, JobState.CANCELLED) is JobState.CANCELLED


def test_a_queued_job_cannot_be_reported_successful():
    """돌지 않은 작업이 성공으로 닫히면, 그 범위는 메워진 적이 없는데 메워졌다고 남는다."""
    with pytest.raises(TransitionRefused):
        plan_job_state(JobState.QUEUED, JobState.SUCCEEDED)


def test_a_running_job_cannot_go_back_to_queued():
    with pytest.raises(TransitionRefused):
        plan_job_state(JobState.RUNNING, JobState.QUEUED)


# --- cursor ---------------------------------------------------------------------

def test_the_cursor_moves_only_after_a_durable_write():
    """읽자마자 옮기면 실패한 구간이 영영 안 메워진다. 다음 실행은 지났다고 본다."""
    assert advance_cursor("100.000000", "200.000000", durable=False) == "100.000000"


def test_the_cursor_moves_forward_after_a_write():
    assert advance_cursor("100.000000", "200.000000", durable=True) == "200.000000"


def test_the_cursor_never_moves_backwards():
    """재실행이 겹치는 구간을 다시 읽어도 제자리여야 한다."""
    assert advance_cursor("200.000000", "100.000000", durable=True) == "200.000000"


def test_an_empty_candidate_leaves_the_cursor_alone():
    assert advance_cursor("200.000000", "", durable=True) == "200.000000"


def test_the_first_write_sets_the_cursor():
    assert advance_cursor("", "100.000000", durable=True) == "100.000000"
