"""운영 원문을 **누가 쓰는가.** 둘이 동시에 쓰지 않는다.

결정: 2026-09-30 오너 — Master 수집 기능 분리 준비.

`owns_write()` 는 예전부터 있었지만 부르는 곳이 시험뿐이었다. 그래서 콘솔에서
채널을 인수해도 Master 는 계속 썼다 — 오류 없이, 두 봇이 같은 채널에.

여기서 보는 것은 셋이다.

1. 어떤 상태에서도 **둘 다 참이 되지 않는다**
2. 모르는 상태의 기본값이 **서로 반대**다 — Master 는 쓰고 Archiver 는 안 쓴다
3. 표를 못 읽어도 **수집이 죽지 않는다**
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager

import pytest

from tybot.archive.archiving_state import ChannelMode, ChannelState, WriterOwner
from tybot.archive.write_owner import (
    Decision,
    OwnerLookup,
    decide,
    state_from_row,
)

WS = "tyit"
CH = "C0FUND"


def _state(mode: ChannelMode, owner: WriterOwner, cutover: str = "") -> ChannelState:
    return ChannelState(WS, CH, mode, owner, cutover)


SHADOW = _state(ChannelMode.SHADOW, WriterOwner.MASTER)
ACTIVE = _state(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1759100000.000100")
PAUSED = _state(ChannelMode.PAUSED, WriterOwner.ARCHIVER, "1759100000.000100")
OFF = _state(ChannelMode.OFF, WriterOwner.MASTER)


# --- 둘이 동시에 쓰지 않는다 -----------------------------------------------------

def test_no_state_lets_both_bots_write():
    """이 시험이 이 모듈의 이유다. 둘이 같이 쓰면 줄이 섞이고 doc_count 가 유실된다."""
    states = [
        _state(mode, owner, cutover)
        for mode, owner, cutover in itertools.product(
            ChannelMode, WriterOwner, ("", "1759100000.000100"),
        )
    ]

    for state in [*states, None]:
        for flag in (True, False):
            writers = [
                actor for actor in WriterOwner
                if decide(state, actor, archiver_flag=flag).allowed
            ]
            assert len(writers) <= 1, (state, flag, writers)


def test_an_active_channel_belongs_to_the_archiver():
    assert decide(ACTIVE, WriterOwner.ARCHIVER, archiver_flag=True).allowed
    assert not decide(ACTIVE, WriterOwner.MASTER).allowed


def test_a_shadow_channel_still_belongs_to_the_master():
    """전환 전에는 Master 가 계속 운영 아카이브를 쓴다."""
    assert decide(SHADOW, WriterOwner.MASTER).allowed
    assert not decide(SHADOW, WriterOwner.ARCHIVER, archiver_flag=True).allowed


def test_a_paused_channel_belongs_to_nobody():
    """멈춘 채널은 양쪽 다 안 쓴다. 그래야 「멈췄다」 가 실제로 멈춘 것이다."""
    assert not decide(PAUSED, WriterOwner.MASTER).allowed
    assert not decide(PAUSED, WriterOwner.ARCHIVER, archiver_flag=True).allowed


def test_the_global_switch_alone_does_not_hand_over_a_channel():
    """전역만 보면 채널 하나 문제가 전체를 멈추게 하고, 채널만 보면 차단기가 없다."""
    assert not decide(SHADOW, WriterOwner.ARCHIVER, archiver_flag=True).allowed
    assert not decide(ACTIVE, WriterOwner.ARCHIVER, archiver_flag=False).allowed
    assert decide(ACTIVE, WriterOwner.ARCHIVER, archiver_flag=True).allowed


def test_the_switch_being_off_is_said_out_loud():
    """거절 사유가 「인수 안 됨」 과 같으면 사람이 무엇을 켤지 모른다."""
    verdict = decide(ACTIVE, WriterOwner.ARCHIVER, archiver_flag=False)

    assert "archiver_writes_live" in verdict.reason


# --- 모르는 상태의 기본값 ---------------------------------------------------------

def test_an_unknown_channel_stays_with_the_master():
    """전환 전 상태가 그것이다. 반대로 두면 표에 없는 채널이 통째로 안 모인다."""
    assert decide(None, WriterOwner.MASTER).allowed
    assert not decide(None, WriterOwner.ARCHIVER, archiver_flag=True).allowed


def test_an_unreadable_row_is_not_guessed():
    """모르는 모드를 추측해 읽으면 「알 수 없어서 아무나 쓴다」 가 된다."""
    assert state_from_row(WS, CH, {"mode": "허용안됨", "writer_owner": "master"}) is None
    assert state_from_row(WS, CH, {"writer_owner": "master"}) is None
    assert state_from_row(WS, CH, None) is None


def test_a_good_row_becomes_a_state():
    state = state_from_row(WS, CH, {
        "mode": "active", "writer_owner": "archiver", "cutover_ts": "1.000100",
    })

    assert state == _state(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1.000100")


def test_a_decision_reads_as_a_boolean():
    """호출부가 `if not verdict:` 로 읽는다. 속성을 잊으면 항상 참이 된다."""
    assert bool(decide(SHADOW, WriterOwner.MASTER))
    assert not bool(decide(ACTIVE, WriterOwner.MASTER))
    assert isinstance(decide(OFF, WriterOwner.MASTER), Decision)


# --- 표를 못 읽어도 수집이 죽지 않는다 ---------------------------------------------

class FakeCursor:
    def __init__(self, row, error=None) -> None:
        self.row = row
        self.error = error
        self.queries = 0
        self.sql = ""

    def execute(self, sql, params=None):
        self.queries += 1
        self.sql = str(sql)
        if self.error:
            raise self.error

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _lookup(row=None, *, conn=True, error=None, clock=None):
    cursor = FakeCursor(row, error)

    @contextmanager
    def connect():
        yield FakeConn(cursor) if conn else None

    lookup = OwnerLookup(WS, connect=connect, clock=clock or (lambda: 0.0))
    return lookup, cursor


def test_without_a_database_the_master_keeps_collecting():
    """DB 장애로 수집이 죽으면 그날 대화가 통째로 빠진다."""
    lookup, _ = _lookup(conn=False)

    verdict = lookup.master_may_write(CH)

    assert verdict.allowed
    assert verdict.degraded is True


def test_operator_stop_survives_database_failure(monkeypatch):
    monkeypatch.setenv("TYBOT_MASTER_CHANNEL_WRITES_ENABLED", "0")
    lookup, cursor = _lookup(conn=False)

    verdict = lookup.master_may_write(CH)

    assert not verdict.allowed
    assert "operator" in verdict.reason
    assert cursor.queries == 0


def test_operator_stop_is_checked_after_a_cached_allow(monkeypatch):
    lookup, _ = _lookup({"mode": "shadow", "writer_owner": "master"})
    assert lookup.master_may_write(CH).allowed

    monkeypatch.setenv("TYBOT_MASTER_CHANNEL_WRITES_ENABLED", "off")
    assert not lookup.master_may_write(CH).allowed


def test_without_a_database_the_archiver_does_not_write_live():
    """둘 다 기본값으로 쓰면 장애 중에 두 봇이 같은 채널에 쓴다."""
    lookup, _ = _lookup(conn=False)

    assert not lookup.archiver_may_write_live(CH, archiver_flag=True).allowed


def test_archiver_live_switch_is_read_without_update_privilege():
    lookup, cursor = _lookup({"enabled": True})

    assert lookup.archiver_live_flag() is True
    assert cursor.queries == 1
    assert "FOR SHARE" not in cursor.sql.upper()
    assert "FOR UPDATE" not in cursor.sql.upper()


def test_archiver_live_switch_fails_closed_on_database_error():
    lookup, _ = _lookup(error=RuntimeError("permission denied"))

    assert lookup.archiver_live_flag() is False


def test_a_query_error_does_not_raise():
    lookup, _ = _lookup(error=RuntimeError("연결 끊김"))

    verdict = lookup.master_may_write(CH)

    assert verdict.allowed
    assert verdict.degraded is True


def test_a_handed_over_channel_is_read_from_the_table():
    lookup, _ = _lookup({
        "mode": "active", "writer_owner": "archiver", "cutover_ts": "1.000100",
    })

    verdict = lookup.master_may_write(CH)

    assert not verdict.allowed
    assert verdict.degraded is False
    assert verdict.owner == WriterOwner.ARCHIVER


def test_the_table_is_not_read_for_every_message():
    """메시지마다 물으면 실시간 수집이 DB 응답 시간에 묶인다."""
    lookup, cursor = _lookup({"mode": "shadow", "writer_owner": "master"})

    for _ in range(5):
        lookup.master_may_write(CH)

    assert cursor.queries == 1


def test_the_cache_expires_so_a_handover_lands():
    """오래 들고 있으면 인수된 뒤에도 옛 주인이 계속 쓴다."""
    now = [0.0]
    lookup, cursor = _lookup({"mode": "shadow", "writer_owner": "master"},
                             clock=lambda: now[0])
    lookup.master_may_write(CH)
    now[0] = 1000.0

    lookup.master_may_write(CH)

    assert cursor.queries == 2


def test_forget_makes_the_next_decision_read_the_table():
    lookup, cursor = _lookup({"mode": "shadow", "writer_owner": "master"})
    lookup.master_may_write(CH)

    lookup.forget(CH)
    lookup.master_may_write(CH)

    assert cursor.queries == 2


def test_each_channel_is_judged_on_its_own():
    """한 채널의 판정이 다른 채널에 새면, 인수 하나가 전부를 멈춘다."""
    lookup, cursor = _lookup({"mode": "shadow", "writer_owner": "master"})

    lookup.master_may_write("C1")
    lookup.master_may_write("C2")

    assert cursor.queries == 2


@pytest.mark.parametrize("mode,owner,expected", [
    ("off", "master", True),
    ("shadow", "master", True),
    ("active", "archiver", False),
    ("paused", "archiver", False),
])
def test_the_master_gate_matches_the_table(mode, owner, expected):
    lookup, _ = _lookup({"mode": mode, "writer_owner": owner,
                         "cutover_ts": "1.000100"})

    assert lookup.master_may_write(CH).allowed is expected
