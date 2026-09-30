"""채널 하나를 **비우고 넘기는** 순서.

결정: 2026-09-30 오너 — 겹침 대신 빈 구간으로 간다.

바로 `shadow → active` 로 가면 Master 의 판정 캐시가 만료되기 전까지 두 봇이 같은
메시지를 각자 쓴다. 그 겹침은 경합이라 **범위를 모른다.**

가운데 `paused` 를 넣으면 겹침 대신 빔이 생긴다. 빔은 사람이 누른 두 시각 사이라
**범위를 알고**, 소급 하나가 메우므로 화자 문자열도 저절로 같다.

여기서 보는 것은 셋이다.

1. 순서를 **건너뛰지 못한다** — 비우기 없이 올릴 수 없다
2. 비운 시간을 **모르면 기다린다** — 「모르니까 됐다고 치자」 가 곧 겹침이다
3. 빈 구간을 안 메우면 **끝났다고 말하지 않는다**
"""

from __future__ import annotations

import pytest

from tybot.archive.archiving_state import ChannelMode, ChannelState, WriterOwner
from tybot.archive.handover import (
    BACKFILL_OVERLAP_SECONDS,
    DRAIN_SETTLE_SECONDS,
    Preconditions,
    Stage,
    backfill_window,
    plan,
    stage_of,
)
from tybot.archive.write_owner import CACHE_SECONDS, decide

WS = "tyit"
CH = "C0FUND"
PAUSED_AT = "1759100000.000100"

READY = Preconditions(
    shadow_compared=True, speaker_parity=True, live_switch=True,
    archiver_joined=True, schema_gate_open=True,
)


def _state(mode: ChannelMode, owner: WriterOwner = WriterOwner.MASTER,
           cutover: str = "") -> ChannelState:
    return ChannelState(WS, CH, mode, owner, cutover)


SHADOW = _state(ChannelMode.SHADOW)
DRAINING = _state(ChannelMode.PAUSED)
ACTIVE = _state(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, PAUSED_AT)


# --- 단계는 모드 하나에서 읽는다 ---------------------------------------------------

def test_the_stage_comes_from_the_mode():
    """단계를 따로 저장하면 모드와 어긋나는 상태가 생기고, 화면이 둘 중 못 고른다."""
    assert stage_of(SHADOW) == Stage.BEFORE
    assert stage_of(DRAINING) == Stage.DRAINING
    assert stage_of(ACTIVE) == Stage.HANDED_OVER
    assert stage_of(_state(ChannelMode.OFF)) == Stage.NOT_IN_SCOPE
    assert stage_of(None) == Stage.BEFORE


# --- 순서를 건너뛰지 못한다 --------------------------------------------------------

def test_the_first_step_is_to_drain_not_to_activate():
    """이 시험이 이 모듈의 이유다. 바로 올리면 겹친다."""
    step = plan(SHADOW, ready=READY)

    assert step.action == "pause"
    assert not step.blocked


def test_an_unchecked_channel_cannot_start():
    step = plan(SHADOW, ready=Preconditions())

    assert step.blocked
    assert step.action == "none"


@pytest.mark.parametrize("missing", [
    "shadow_compared", "speaker_parity", "live_switch",
    "archiver_joined", "schema_gate_open",
])
def test_each_precondition_alone_blocks_the_handover(missing):
    """하나라도 빠지면 막는다. 빠진 이름을 말해 주지 않으면 사람이 뭘 할지 모른다."""
    ready = Preconditions(**{
        **{check.name: True for check in READY.checks()},
        missing: False,
    })

    step = plan(SHADOW, ready=ready)

    assert step.blocked
    assert missing in step.reason


def test_speaker_parity_is_a_precondition_not_an_afterthought():
    """화자 이름이 다르면 소급이 메운 구간에서 한 사람이 두 이름으로 남는다.

    오류가 아니라 **사람이 둘로 보이는** 모양이라 눈에 잘 안 띈다.
    """
    names = {check.name for check in Preconditions().checks()}

    assert "speaker_parity" in names


def test_a_contradictory_row_is_not_guessed():
    """shadow 인데 주인이 archiver 다. 추측해서 넘기면 그 추측이 조용히 틀린다."""
    step = plan(_state(ChannelMode.SHADOW, WriterOwner.ARCHIVER), ready=READY)

    assert step.blocked
    assert "표를 먼저" in step.reason


def test_an_off_channel_is_not_a_handover_target():
    step = plan(_state(ChannelMode.OFF), ready=READY)

    assert step.blocked
    assert step.action == "none"


# --- 비운 시간을 모르면 기다린다 ---------------------------------------------------

def test_an_unknown_drain_time_blocks_the_activation():
    """「모르니까 됐다고 치자」 가 곧 겹침이다."""
    step = plan(DRAINING, ready=READY, drained_seconds=None)

    assert step.action == "wait"
    assert step.blocked


def test_activating_too_early_is_refused():
    """Master 의 캐시가 남아 있으면 비우려던 단계가 아무 일도 안 한 셈이 된다."""
    step = plan(DRAINING, ready=READY, drained_seconds=CACHE_SECONDS)

    assert step.action == "wait"
    assert "겹침" in step.reason


def test_the_settle_time_outlasts_the_master_cache():
    """이 관계가 깨지면 기다리는 의미가 없다. 시계가 정확히 맞지도 않는다."""
    assert DRAIN_SETTLE_SECONDS > CACHE_SECONDS


def test_after_settling_the_channel_may_be_activated():
    step = plan(DRAINING, ready=READY, drained_seconds=DRAIN_SETTLE_SECONDS)

    assert step.action == "activate"
    assert not step.blocked


def test_a_drained_channel_really_has_no_writer():
    """비우는 단계가 실제로 비는지 문지기에게 다시 묻는다. 말만 비우면 안 된다."""
    writers = [actor for actor in WriterOwner
               if decide(DRAINING, actor, archiver_flag=True).allowed]

    assert writers == []


# --- 빈 구간을 안 메우면 끝난 것이 아니다 -------------------------------------------

def test_a_handed_over_channel_still_owes_a_backfill():
    """비운 구간은 그림자에도 없다. 안 메우면 Slack 보존 기간이 지나는 날 사라진다."""
    step = plan(ACTIVE, ready=READY, paused_at_ts=PAUSED_AT)

    assert step.action == "backfill"
    assert not step.blocked


def test_only_a_succeeded_backfill_ends_the_handover():
    step = plan(ACTIVE, ready=READY, paused_at_ts=PAUSED_AT, backfill_succeeded=True)

    assert step.action == "done"


def test_a_queued_or_failed_backfill_is_not_done():
    assert plan(ACTIVE, ready=READY, paused_at_ts=PAUSED_AT).action == "backfill"


def test_the_backfill_starts_before_the_pause():
    """Master 가 캐시 때문에 그 뒤로도 잠깐 더 썼다. 뒤에서 시작하면 구간이 빈다."""
    start, end = backfill_window(PAUSED_AT)

    assert float(start) == pytest.approx(float(PAUSED_AT) - BACKFILL_OVERLAP_SECONDS)
    assert end == ""


def test_the_overlap_outlasts_the_master_cache():
    """겹침이 캐시보다 짧으면 Master 의 마지막 쓰기 뒤에서 시작할 수 있다."""
    assert BACKFILL_OVERLAP_SECONDS > CACHE_SECONDS


def test_the_backfill_end_is_open():
    """끝을 박으면 Archiver 가 실시간으로 쓰기 시작한 지점과 사이가 벌어진다."""
    assert backfill_window(PAUSED_AT)[1] == ""


def test_an_unreadable_coordinate_widens_the_window_instead_of_narrowing_it():
    """좁은 범위로 「메웠다」 고 말하는 것이 못 메운 것보다 나쁘다."""
    assert backfill_window("어제") == ("", "")
    assert backfill_window("") == ("", "")


def test_the_window_never_goes_negative():
    assert backfill_window("10.000000") == ("0.000000", "")


def test_pausing_also_stops_the_shadow_collection():
    """§5.3 이 이 사실 위에 서 있다. 바뀌면 문서가 조용히 틀린 것이 된다.

    `paused` 동안 그림자까지 멈추기 때문에 비운 구간은 **어디에도 안 남는다.**
    그래서 소급이 선택이 아니라 필수다.
    """
    from tybot.archive.channel_membership import is_collectible

    row = {"membership": "joined", "operator_hold": False}

    assert is_collectible({**row, "mode": "shadow"})
    assert is_collectible({**row, "mode": "active"})
    assert not is_collectible({**row, "mode": "paused"})


def test_the_step_reads_as_a_boolean():
    """호출부가 `if not step:` 으로 막힌 것을 본다."""
    assert plan(SHADOW, ready=READY)
    assert not plan(SHADOW, ready=Preconditions())


# --- 되돌릴 수 있나 ---------------------------------------------------------------
#
# 「인수했는데 문제가 있다」 는 반드시 생긴다. 그때 되돌릴 수 없으면 인수를 못 한다.
#
# 되돌리는 길은 **한 단계가 아니다.** `_MODE_EDGES` 에 `active → shadow` 가 없고
# `active → paused → shadow` 만 있다. 먼저 멈추고, 그다음에 넘긴다.

def test_there_is_no_one_step_way_back():
    """바로 넘기면 멈추지 않은 채 주인이 바뀐다 — 인수할 때와 똑같은 겹침이다."""
    from tybot.archive.archiving_state import TransitionRefused, plan_mode_change

    with pytest.raises(TransitionRefused, match="갈 수 없습니다"):
        plan_mode_change(ACTIVE, ChannelMode.SHADOW, cutover_ts="1759200000.000100")


def test_the_first_step_back_is_to_stop_both():
    """멈추는 데는 좌표가 필요 없다. 급할 때 좌표를 정하느라 못 멈추면 안 된다."""
    from tybot.archive.archiving_state import plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)

    assert [who for who in WriterOwner
            if decide(halted, who, archiver_flag=True).allowed] == []


def test_the_second_step_reopens_the_master_gate():
    """모드만 바뀌고 문지기가 안 열리면 되돌린 것이 아니다."""
    from tybot.archive.archiving_state import plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)
    back = plan_mode_change(halted, ChannelMode.SHADOW, cutover_ts="1759200000.000100")

    assert back.writer_owner == WriterOwner.MASTER
    assert decide(back, WriterOwner.MASTER).allowed
    assert not decide(back, WriterOwner.ARCHIVER, archiver_flag=True).allowed


def test_handing_back_needs_a_new_coordinate():
    """좌표 없이 소유권만 바꾸면 그 경계에서 중복·누락이 생긴다."""
    from tybot.archive.archiving_state import TransitionRefused, plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)

    with pytest.raises(TransitionRefused, match="역인수 좌표"):
        plan_mode_change(halted, ChannelMode.SHADOW)


def test_the_rollback_coordinate_cannot_precede_the_handover():
    """뒤로 가면 이미 넘긴 구간을 다시 넘기게 되고, 그 구간은 양쪽이 다 썼다고 본다."""
    from tybot.archive.archiving_state import TransitionRefused, plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)

    with pytest.raises(TransitionRefused, match="앞설 수 없습니다"):
        plan_mode_change(halted, ChannelMode.SHADOW, cutover_ts="1.000100")


def test_a_rolled_back_channel_starts_over():
    """되돌린 채널은 처음부터 다시 간다. `active` 로 바로 못 돌아간다."""
    from tybot.archive.archiving_state import plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)
    back = plan_mode_change(halted, ChannelMode.SHADOW, cutover_ts="1759200000.000100")

    assert stage_of(back) == Stage.BEFORE
    assert plan(back, ready=READY).action == "pause"


def test_stopping_does_not_erase_who_owned_it():
    """멈춘 동안에도 주인은 기억한다. 잊으면 재개할 때 어느 모드로 갈지 모른다."""
    from tybot.archive.archiving_state import plan_mode_change

    halted = plan_mode_change(ACTIVE, ChannelMode.PAUSED)

    assert halted.writer_owner == WriterOwner.ARCHIVER
    assert halted.cutover_ts == ACTIVE.cutover_ts
