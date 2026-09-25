"""Archiving Bot 상태 전이 — **조용한 실패를 막는 자리들.**

여기서 막는 것은 전부 「오류가 안 나는 사고」 다.

- 두 writer 가 같은 채널에 쓰면 줄이 섞이는데 아무도 예외를 안 받는다
- 첨부가 안 끝났는데 「검색됩니다」 라고 하면 사람이 찾으러 갔다가 없다고 본다
- 그림자를 건너뛰면 누락을 확인할 기회 자체가 사라진다
- 보존 기간을 안 정하면 기본값이 「영구 보관」 이 된다

결정: 2026-09-25 오너 확정. 스키마: `deploy/sql/archiving_schema.sql`.
"""

from __future__ import annotations

import pytest

from tybot.archive.archiving_state import (
    ChannelMode,
    ChannelState,
    IngestProgress,
    IngestState,
    Revision,
    RevisionKind,
    TransitionRefused,
    WriterOwner,
    attachment_revision,
    current_revision,
    may_write_live,
    next_revision,
    owns_write,
    plan_ingest_change,
    plan_mode_change,
    production_blockers,
    searchable_claim,
)


def _channel(mode=ChannelMode.OFF, owner=WriterOwner.MASTER, cutover="") -> ChannelState:
    return ChannelState("tyit", "C0FUND", mode, owner, cutover)


# --- 채널 모드 ---------------------------------------------------------------

def test_shadow_cannot_be_skipped():
    """`off → active` 는 없다.

    그림자를 건너뛰면 「누락 0 · 권한 유출 0 · 첨부 손실 0」 을 확인할 기회가
    없다(분리 설계 §4-C). 급할 때 건너뛰고 싶어지는 단계라 코드로 막는다.
    """
    with pytest.raises(TransitionRefused) as caught:
        plan_mode_change(_channel(), ChannelMode.ACTIVE, cutover_ts="1700000000.0001")

    assert "off → active" in str(caught.value)
    assert "shadow" in str(caught.value)


def test_going_active_requires_a_cutover_coordinate():
    """인수 좌표가 없으면 인수 전후를 대조할 수 없다."""
    shadow = _channel(ChannelMode.SHADOW)

    with pytest.raises(TransitionRefused) as caught:
        plan_mode_change(shadow, ChannelMode.ACTIVE)

    assert "cutover_ts" in str(caught.value)


def test_going_active_moves_the_owner_together():
    """모드와 주인은 **같이** 움직인다.

    따로 움직이면 「active 인데 주인은 master」 가 만들어지고, 그 상태에서는
    두 writer 가 같은 파일에 쓴다. 줄이 섞이고 `doc_count` 갱신이 유실된다.
    """
    after = plan_mode_change(
        _channel(ChannelMode.SHADOW), ChannelMode.ACTIVE, cutover_ts="1700000000.0001"
    )

    assert after.mode == ChannelMode.ACTIVE
    assert after.writer_owner == WriterOwner.ARCHIVER
    assert after.cutover_ts == "1700000000.0001"


def test_cutover_cannot_move_backwards():
    """되돌리면 이미 넘긴 구간을 **두 writer 가 다 썼다고** 생각한다."""
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000500.0000")
    paused = plan_mode_change(active, ChannelMode.PAUSED)

    with pytest.raises(TransitionRefused) as caught:
        plan_mode_change(paused, ChannelMode.ACTIVE, cutover_ts="1700000100.0000")

    assert "뒤로 갈 수 없습니다" in str(caught.value)


def test_turning_off_returns_the_channel_to_master():
    """archiver 가 주인인 채로 꺼지면 **아무도 안 쓰는 구간**이 생긴다.

    그건 누락인데 오류가 안 난다.

    출발점이 중요하다. 이미 master 인 채널에서 끄면 주인을 유지해도 결과가 같아
    시험이 아무것도 안 본다. 그래서 **실제로 갈 수 있는 경로**로 active 에서
    역인수한 뒤 끈다(active → paused → shadow → off).
    """
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")
    paused = plan_mode_change(active, ChannelMode.PAUSED)
    shadow = plan_mode_change(
        paused, ChannelMode.SHADOW, cutover_ts="1700001000.0001"
    )
    assert shadow.writer_owner == WriterOwner.MASTER

    after = plan_mode_change(shadow, ChannelMode.OFF)

    assert after.writer_owner == WriterOwner.MASTER
    assert after.cutover_ts == ""


def test_pause_keeps_the_owner_so_resume_continues():
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")

    paused = plan_mode_change(active, ChannelMode.PAUSED)

    assert paused.writer_owner == WriterOwner.ARCHIVER
    assert paused.cutover_ts == "1700000000.0001"


def test_active_cannot_go_straight_back_to_shadow():
    """운영에 쓰던 채널을 그림자로 되돌리면 그 사이 원문을 아무도 안 쓴다."""
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")

    with pytest.raises(TransitionRefused):
        plan_mode_change(active, ChannelMode.SHADOW)


def test_reverse_cutover_returns_live_writes_to_master():
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")
    paused = plan_mode_change(active, ChannelMode.PAUSED)

    shadow = plan_mode_change(
        paused, ChannelMode.SHADOW, cutover_ts="1700001000.0001"
    )

    assert shadow.writer_owner == WriterOwner.MASTER
    assert shadow.cutover_ts == "1700001000.0001"
    assert owns_write(shadow, WriterOwner.MASTER)
    assert not owns_write(shadow, WriterOwner.ARCHIVER)


def test_reverse_cutover_needs_a_new_coordinate():
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")
    paused = plan_mode_change(active, ChannelMode.PAUSED)

    with pytest.raises(TransitionRefused, match="역인수 좌표"):
        plan_mode_change(paused, ChannelMode.SHADOW)


# --- 동시 쓰기 ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("mode", "owner"),
    [
        (ChannelMode.OFF, WriterOwner.MASTER),
        (ChannelMode.SHADOW, WriterOwner.MASTER),
        (ChannelMode.ACTIVE, WriterOwner.ARCHIVER),
    ],
)
def test_exactly_one_writer_owns_a_channel(mode, owner):
    """**둘이 동시에 참이 되지 않는다.** 이게 이 모델의 전부다."""
    state = _channel(mode, owner, "1700000000.0001" if owner == WriterOwner.ARCHIVER else "")

    owners = [who for who in WriterOwner if owns_write(state, who)]

    assert len(owners) == 1, f"{mode}/{owner} 에서 주인이 {owners}"


def test_paused_channel_has_no_live_writer():
    paused = _channel(
        ChannelMode.PAUSED, WriterOwner.ARCHIVER, "1700000000.0001"
    )

    assert [who for who in WriterOwner if owns_write(paused, who)] == []


def test_shadow_never_writes_live():
    """그림자는 보기만 한다. 켜져 있어도 운영에 안 쓴다."""
    shadow = _channel(ChannelMode.SHADOW)

    assert may_write_live(shadow, archiver_flag=True) is False


def test_global_switch_can_stop_every_channel_at_once():
    """사고 때 한 번에 내릴 차단기가 있어야 한다."""
    active = _channel(ChannelMode.ACTIVE, WriterOwner.ARCHIVER, "1700000000.0001")

    assert may_write_live(active, archiver_flag=True) is True
    assert may_write_live(active, archiver_flag=False) is False


# --- 수집 상태(ACK) ----------------------------------------------------------

def test_ready_is_refused_while_attachments_are_pending():
    """오너 결정 §6 — attachment ready 전에는 검색 가능하다고 말하지 않는다."""
    progress = IngestProgress(IngestState.ATTACHMENT_PENDING, attachment_total=3, attachment_ready=1)

    with pytest.raises(TransitionRefused) as caught:
        plan_ingest_change(progress, IngestState.READY)

    assert "1/3" in str(caught.value)


def test_ready_is_allowed_once_attachments_finish():
    progress = IngestProgress(IngestState.ATTACHMENT_PENDING, attachment_total=3, attachment_ready=3)

    after = plan_ingest_change(progress, IngestState.READY)

    assert after.state == IngestState.READY


def test_a_message_with_no_attachments_can_go_straight_to_ready():
    progress = IngestProgress(IngestState.RAW_WRITTEN)

    assert plan_ingest_change(progress, IngestState.READY).state == IngestState.READY


def test_received_cannot_jump_to_ready():
    """원문을 쓰지도 않고 「됐다」 고 하는 경로를 막는다."""
    with pytest.raises(TransitionRefused):
        plan_ingest_change(IngestProgress(IngestState.RECEIVED), IngestState.READY)


@pytest.mark.parametrize(
    "terminal", [IngestState.READY, IngestState.REFUSED, IngestState.FAILED]
)
def test_terminal_states_do_not_move(terminal):
    """끝난 것은 안 되돌린다. 새 사실은 새 메시지이거나 새 revision 이다."""
    with pytest.raises(TransitionRefused) as caught:
        plan_ingest_change(IngestProgress(terminal), IngestState.RAW_WRITTEN)

    assert "끝난 상태" in str(caught.value)


def test_partial_can_still_finish():
    progress = IngestProgress(IngestState.PARTIAL, attachment_total=2, attachment_ready=2)

    assert plan_ingest_change(progress, IngestState.READY).state == IngestState.READY


# --- 사람에게 뭐라고 말하나 ---------------------------------------------------

def test_pending_attachments_are_never_called_searchable():
    """문구를 한 자리에서 만드는 이유 — 호출부마다 쓰면 한 군데가 거짓말한다."""
    claim = searchable_claim(
        IngestProgress(IngestState.ATTACHMENT_PENDING, 3, 1), require_ack=True
    )

    assert "검색" in claim
    assert "안 잡힙니다" in claim
    assert "1/3" in claim


def test_ready_is_the_only_state_that_claims_searchable():
    for state in IngestState:
        total = 0 if state == IngestState.READY else 2
        claim = searchable_claim(IngestProgress(state, total, 0), require_ack=True)
        says_yes = "검색할 수 있습니다" in claim
        assert says_yes == (state == IngestState.READY), f"{state}: {claim}"


def test_claim_without_ack_still_does_not_invent_facts():
    """스위치가 꺼져 있어도 **없는 사실을 만들지는 않는다.**"""
    claim = searchable_claim(IngestProgress(IngestState.RAW_WRITTEN), require_ack=False)

    assert "검색" not in claim


# --- 메시지 revision ---------------------------------------------------------

def test_first_revision_must_be_create():
    with pytest.raises(TransitionRefused) as caught:
        next_revision([], RevisionKind.CHANGE, body_sha256="a" * 64)

    assert "create" in str(caught.value)


def test_edits_stack_instead_of_overwriting():
    """원문은 안 고친다. 고친 것은 **새 revision** 으로 쌓는다."""
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]
    history.append(next_revision(history, RevisionKind.CHANGE, body_sha256="b" * 64))

    assert [r.revision_no for r in history] == [1, 2]
    assert history[0].body_sha256 == "a" * 64, "앞의 것이 바뀌면 안 된다"
    assert current_revision(history).body_sha256 == "b" * 64


def test_delete_keeps_the_earlier_body_but_hides_the_message():
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]
    history.append(next_revision(history, RevisionKind.DELETE))

    assert current_revision(history) is None, "검색에는 안 보인다"
    assert history[0].body_sha256 == "a" * 64, "감사에는 남는다"


def test_redact_leaves_no_body_hash():
    """짧은 본문은 사전 대입으로 해시에서 되찾힌다.

    해시를 남기면 「본문을 남기지 않는다」 를 지킨 것이 아니다.
    """
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]
    history.append(
        next_revision(history, RevisionKind.REDACT, body_sha256="b" * 64, reason_code="rrn")
    )

    assert history[-1].body_sha256 == ""
    assert history[-1].reason_code == "rrn"


def test_redact_needs_a_reason():
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]

    with pytest.raises(TransitionRefused):
        next_revision(history, RevisionKind.REDACT)


def test_nothing_stacks_on_a_redacted_message():
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]
    history.append(next_revision(history, RevisionKind.REDACT, reason_code="legal"))

    with pytest.raises(TransitionRefused):
        next_revision(history, RevisionKind.CHANGE, body_sha256="c" * 64)


def test_create_cannot_be_stacked_twice():
    history = [next_revision([], RevisionKind.CREATE, body_sha256="a" * 64)]

    with pytest.raises(TransitionRefused):
        next_revision(history, RevisionKind.CREATE, body_sha256="b" * 64)


def test_a_deleted_message_can_still_be_redacted():
    """지운 뒤에도 법적 삭제가 들어올 수 있다. 그때 본문 해시가 사라져야 한다."""
    history = [
        Revision(RevisionKind.CREATE, 1, body_sha256="a" * 64),
        Revision(RevisionKind.DELETE, 2),
    ]

    after = next_revision(history, RevisionKind.REDACT, reason_code="legal")

    assert after.revision_no == 3
    assert after.body_sha256 == ""


# --- 첨부 revision -----------------------------------------------------------

BASE = {
    "source_sha256": "a" * 64,
    "converter_name": "kordoc",
    "converter_version": "2.1",
    "config": {"ocr": True, "schema": 3},
}


def test_same_input_gives_the_same_revision():
    """재변환이 멱등해야 큐를 다시 돌려도 안전하다."""
    assert attachment_revision(**BASE) == attachment_revision(**BASE)


@pytest.mark.parametrize(
    "change",
    [
        {"source_sha256": "b" * 64},
        {"converter_name": "pandoc"},
        {"converter_version": "2.2"},
        {"config": {"ocr": False, "schema": 3}},
        {"config": {"ocr": True, "schema": 4}},
    ],
    ids=["source", "converter", "version", "config-value", "schema"],
)
def test_any_of_the_four_moves_the_revision(change):
    """넷 중 하나만 바뀌어도 다른 변환본이다. 그러면 **덮지 않고 새로 쌓는다.**"""
    assert attachment_revision(**{**BASE, **change}) != attachment_revision(**BASE)


def test_config_key_order_does_not_move_the_revision():
    """dict 순서가 결과를 바꾸면 같은 설정이 매번 새 revision 을 만든다."""
    a = attachment_revision(**{**BASE, "config": {"ocr": True, "schema": 3}})
    b = attachment_revision(**{**BASE, "config": {"schema": 3, "ocr": True}})

    assert a == b


# --- production 게이트 -------------------------------------------------------

def test_missing_retention_blocks_production():
    """값이 없으면 기본값이 「영구 보관」 이 된다.

    개인 대화를 영구 보관하는 것은 아무도 결정한 적이 없는데 그냥 그렇게 된다.
    """
    blockers = production_blockers({"bot_conversation_audit": 90, "bot_dm_attachment": None})

    retention = [item for item in blockers if "보존 기간" in item]
    assert len(retention) == 1
    assert "bot_dm_attachment" in retention[0]


def test_zero_is_not_a_valid_retention_period():
    """DB와 마찬가지로 0일은 운영 정책으로 인정하지 않는다."""
    blockers = production_blockers(
        {"bot_conversation_audit": 0, "bot_dm_attachment": 0}
    )

    retention = [item for item in blockers if "보존 기간" in item]
    assert len(retention) == 2
    assert all("1일 이상" in blocker for blocker in retention)


def test_all_set_clears_the_gate():
    assert production_blockers(
        {"bot_conversation_audit": 90, "bot_dm_attachment": 30},
        flags={
            "archiver_writes_live": True,
            "preserve_edit_delete": True,
            "require_attachment_ack": True,
            "revision_reader_ready": True,
        },
    ) == []


def test_attachment_separation_without_a_reader_is_blocked():
    """읽는 쪽이 없는데 분리를 켜면 그 본문이 조용히 답변에서 빠진다."""
    blockers = production_blockers(
        {"bot_conversation_audit": 90, "bot_dm_attachment": 30},
        flags={"separate_attachments": True, "attachment_reader_ready": False},
    )

    assert any("읽는 쪽" in item for item in blockers)


def test_missing_reader_flag_is_fail_closed():
    blockers = production_blockers(
        {"bot_conversation_audit": 90, "bot_dm_attachment": 30},
        flags={"separate_attachments": True},
    )

    assert any("읽는 쪽" in item for item in blockers)
