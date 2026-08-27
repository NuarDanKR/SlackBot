"""투표 — 옵션이 실제로 지켜지는지 검증.

특히 확인하는 것:
- 중복 선택 허용/금지가 눌렀을 때 다르게 동작하는가
- **익명 투표에서 저장 파일에 사용자 ID 가 남지 않는가** (익명이라고만 적어두면 익명이 아니다)
- 결과 공개 시점이 채널 메시지와 개인 안내 양쪽에서 지켜지는가
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tybot import poll_view, polls
from tybot.polls import (
    SHOW_AFTER_CLOSE,
    SHOW_AFTER_VOTE,
    SHOW_ALWAYS,
    PollError,
    apply_vote,
    close_poll,
    create_poll,
    parse_options,
)

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def make(**kw):
    base = dict(
        workspace="fin",
        channel_id="C1",
        creator="U-CREATOR",
        question="회식 언제 할까요?",
        options_text="월요일\n화요일\n수요일",
        now=NOW,
    )
    base.update(kw)
    return create_poll(**base)


# --- 입력 검증 -------------------------------------------------------------

def test_options_are_cleaned():
    """사람들이 번호를 직접 붙여 적는다. 그대로 두면 화면에서 두 번 매겨진다."""
    assert parse_options("1. 월요일\n2) 화요일\n- 수요일\n\n  ") == ["월요일", "화요일", "수요일"]


def test_duplicate_options_are_refused():
    with pytest.raises(PollError) as e:
        parse_options("월요일\n월요일")
    assert "두 번" in str(e.value)
    assert e.value.block_id == "options"


def test_needs_at_least_two_options():
    with pytest.raises(PollError):
        parse_options("월요일")


def test_too_many_options_are_refused():
    with pytest.raises(PollError):
        parse_options("\n".join(f"항목{i}" for i in range(polls.MAX_OPTIONS + 1)))


def test_empty_question_is_refused():
    with pytest.raises(PollError) as e:
        make(question="   ")
    assert e.value.block_id == "question"


# --- 하나만 선택 -----------------------------------------------------------

def test_single_choice_switches_vote():
    poll = make()
    apply_vote(poll, "U1", 0, now=NOW)
    assert poll.selection("U1") == [0]
    apply_vote(poll, "U1", 2, now=NOW)
    assert poll.selection("U1") == [2]  # 갈아탄다
    assert poll.voter_count == 1


def test_pressing_same_option_cancels():
    poll = make()
    apply_vote(poll, "U1", 1, now=NOW)
    msg = apply_vote(poll, "U1", 1, now=NOW)
    assert "취소" in msg
    assert poll.selection("U1") == []
    # 아무것도 고르지 않았으면 참여자 수가 부풀지 않아야 한다
    assert poll.voter_count == 0


# --- 중복 선택 -------------------------------------------------------------

def test_multi_choice_accumulates():
    poll = make(multi=True)
    apply_vote(poll, "U1", 0, now=NOW)
    apply_vote(poll, "U1", 2, now=NOW)
    assert poll.selection("U1") == [0, 2]
    assert poll.counts() == [1, 0, 1]


def test_multi_choice_toggles_off():
    poll = make(multi=True)
    apply_vote(poll, "U1", 0, now=NOW)
    apply_vote(poll, "U1", 0, now=NOW)
    assert poll.selection("U1") == []


# --- 변경 금지 -------------------------------------------------------------

def test_locked_poll_refuses_change():
    poll = make(allow_change=False)
    apply_vote(poll, "U1", 0, now=NOW)
    with pytest.raises(PollError) as e:
        apply_vote(poll, "U1", 1, now=NOW)
    assert "바꿀 수 없" in str(e.value)
    assert poll.selection("U1") == [0]


def test_locked_poll_still_lets_others_vote():
    poll = make(allow_change=False)
    apply_vote(poll, "U1", 0, now=NOW)
    apply_vote(poll, "U2", 1, now=NOW)
    assert poll.counts() == [1, 1, 0]


# --- 익명 -----------------------------------------------------------------

def test_anonymous_poll_still_blocks_double_voting():
    """익명이어도 같은 사람이 두 번 세어지면 안 된다."""
    poll = make(anonymous=True)
    apply_vote(poll, "U1", 0, now=NOW)
    apply_vote(poll, "U1", 1, now=NOW)
    assert poll.voter_count == 1
    assert poll.counts() == [0, 1, 0]


def test_anonymous_poll_does_not_store_user_id(tmp_path, monkeypatch):
    """저장 파일에 사용자 ID 가 남으면 익명이 아니다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    poll = make(anonymous=True)
    apply_vote(poll, "U-SUKHYUN", 0, now=NOW)
    polls.save(poll)

    raw = polls.poll_path(poll.workspace, poll.id).read_text(encoding="utf-8")
    assert "U-SUKHYUN" not in raw
    # 만든 사람은 화면에 표시되므로 남는다(그건 익명 대상이 아니다)
    assert poll.creator in raw


def test_anonymous_salt_differs_per_poll(tmp_path, monkeypatch):
    """같은 사용자라도 투표가 다르면 다른 키가 되어야 한다 — 투표 간 대조를 막는다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    a, b = make(anonymous=True), make(anonymous=True)
    assert a.voter_key("U1") != b.voter_key("U1")


def test_anonymous_poll_hides_voter_names():
    poll = make(anonymous=True)
    apply_vote(poll, "U1", 0, now=NOW)
    assert poll.voters_by_option() == [[], [], []]
    lines = "\n".join(poll_view.results_lines(poll, now=NOW))
    assert "<@U1>" not in lines


def test_public_poll_shows_voter_names():
    poll = make()
    apply_vote(poll, "U1", 0, now=NOW)
    lines = "\n".join(poll_view.results_lines(poll, now=NOW))
    assert "<@U1>" in lines


# --- 마감 -----------------------------------------------------------------

def test_deadline_closes_poll():
    poll = make(deadline="1h")
    later = NOW + timedelta(hours=2)
    assert poll.is_open(now=NOW)
    assert not poll.is_open(now=later)
    with pytest.raises(PollError) as e:
        apply_vote(poll, "U1", 0, now=later)
    assert "마감" in str(e.value)


def test_only_creator_can_close():
    poll = make()
    with pytest.raises(PollError) as e:
        close_poll(poll, "U-OTHER")
    assert "만든 사람" in str(e.value)
    close_poll(poll, "U-CREATOR")
    assert poll.closed


def test_admin_can_close():
    poll = make()
    close_poll(poll, "U-ADMIN", is_admin=True)
    assert poll.closed


def test_closed_poll_refuses_votes():
    poll = make()
    close_poll(poll, "U-CREATOR")
    with pytest.raises(PollError):
        apply_vote(poll, "U1", 0, now=NOW)


def test_unknown_deadline_is_refused():
    with pytest.raises(PollError):
        make(deadline="언젠가")


# --- 결과 공개 시점 --------------------------------------------------------

def test_always_shows_results_in_channel():
    poll = make(show_results=SHOW_ALWAYS)
    text = json.dumps(poll_view.message_blocks(poll, now=NOW), ensure_ascii=False)
    assert "월요일*" in text or "*월요일*" in text  # 결과 줄에 굵게 표시된다


def test_after_vote_hides_results_until_voted():
    poll = make(show_results=SHOW_AFTER_VOTE)
    assert not poll.may_see_results("U1", now=NOW)
    apply_vote(poll, "U1", 0, now=NOW)
    assert poll.may_see_results("U1", now=NOW)
    # 투표하지 않은 사람에게는 여전히 감춘다
    assert not poll.may_see_results("U2", now=NOW)


def test_after_close_hides_results_from_everyone_until_closed():
    poll = make(show_results=SHOW_AFTER_CLOSE, deadline="1h")
    apply_vote(poll, "U1", 0, now=NOW)
    assert not poll.may_see_results("U1", now=NOW)
    assert not poll.may_see_results("U-CREATOR", now=NOW)
    later = NOW + timedelta(hours=2)
    assert poll.may_see_results("U1", now=later)


def test_channel_message_hides_results_when_not_always():
    """채널 메시지는 한 장을 모두가 본다. '항상 공개'가 아니면 거기서 결과를 감춰야 한다."""
    poll = make(show_results=SHOW_AFTER_VOTE)
    apply_vote(poll, "U1", 0, now=NOW)
    text = json.dumps(poll_view.message_blocks(poll, now=NOW), ensure_ascii=False)
    assert "투표하면 결과가 보입니다" in text
    assert "1표" not in text


def test_private_results_respects_gate():
    poll = make(show_results=SHOW_AFTER_VOTE)
    assert "먼저 투표하면" in poll_view.private_results(poll, "U1", now=NOW)
    apply_vote(poll, "U1", 0, now=NOW)
    shown = poll_view.private_results(poll, "U1", now=NOW)
    assert "1표" in shown
    assert "내가 고른 항목: 월요일" in shown


# --- 화면 블록 -------------------------------------------------------------

def test_buttons_disappear_after_close():
    poll = make()
    before = json.dumps(poll_view.message_blocks(poll, now=NOW), ensure_ascii=False)
    assert poll_view.ACTION_VOTE in before
    close_poll(poll, "U-CREATOR")
    after = json.dumps(poll_view.message_blocks(poll, now=NOW), ensure_ascii=False)
    assert poll_view.ACTION_VOTE not in after
    assert "마감된 투표입니다" in after


def test_options_are_split_into_rows():
    """한 줄에 버튼을 너무 많이 넣으면 모바일에서 읽기 어렵다."""
    poll = make(options_text="\n".join(f"항목{i}" for i in range(7)))
    rows = [
        b
        for b in poll_view.message_blocks(poll, now=NOW)
        if b["type"] == "actions" and b["block_id"].startswith(poll_view.ACTION_VOTE)
    ]
    assert len(rows) == 2
    assert len(rows[0]["elements"]) == poll_view.BUTTONS_PER_ROW


def test_message_has_fallback_text():
    """blocks 만 보내면 알림이 빈 채로 간다."""
    poll = make()
    assert "회식" in poll_view.fallback_text(poll)


def test_badges_state_the_options():
    poll = make(multi=True, anonymous=True, allow_change=False, deadline="3h")
    text = json.dumps(poll_view.message_blocks(poll, now=NOW), ensure_ascii=False)
    for mark in ("여러 개 선택 가능", "익명", "변경 불가", "남음"):
        assert mark in text


# --- 모달 값 읽기 ----------------------------------------------------------

def test_read_modal_maps_settings():
    view = {
        "private_metadata": "C99",
        "state": {
            "values": {
                "question": {"value": {"value": "언제 할까요?"}},
                "options": {"value": {"value": "월\n화"}},
                "settings": {
                    "value": {
                        "selected_options": [{"value": "multi"}, {"value": "anonymous"}]
                    }
                },
                "show_results": {"value": {"selected_option": {"value": SHOW_AFTER_CLOSE}}},
                "deadline": {"value": {"selected_option": {"value": "1d"}}},
            }
        },
    }
    fields = poll_view.read_modal(view)
    assert fields["channel_id"] == "C99"
    assert fields["multi"] is True
    assert fields["anonymous"] is True
    # 화면에서는 "변경 못 하게 하기"로 묻고 내부에서는 허용 여부로 뒤집는다
    assert fields["allow_change"] is True
    assert fields["show_results"] == SHOW_AFTER_CLOSE
    assert fields["deadline"] == "1d"


def test_read_modal_lock_inverts():
    view = {
        "private_metadata": "C1",
        "state": {
            "values": {
                "question": {"value": {"value": "q"}},
                "options": {"value": {"value": "a\nb"}},
                "settings": {"value": {"selected_options": [{"value": "lock"}]}},
            }
        },
    }
    assert poll_view.read_modal(view)["allow_change"] is False


def test_read_modal_defaults_when_optional_blocks_missing():
    view = {"state": {"values": {"question": {"value": {"value": "q"}}}}}
    fields = poll_view.read_modal(view)
    assert fields["show_results"] == SHOW_ALWAYS
    assert fields["deadline"] == "none"
    assert fields["multi"] is False
    assert fields["allow_change"] is True


# --- 저장 -----------------------------------------------------------------

def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    poll = make(multi=True, deadline="1h")
    apply_vote(poll, "U1", 0, now=NOW)
    poll.message_ts = "1755000000.1"
    polls.save(poll)

    loaded = polls.load(poll.workspace, poll.id)
    assert loaded is not None
    assert loaded.question == poll.question
    assert loaded.multi is True
    assert loaded.selection("U1") == [0]
    assert loaded.message_ts == "1755000000.1"


def test_load_missing_poll_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert polls.load("fin", "없는id") is None


def test_polls_are_not_written_into_the_archive(tmp_path, monkeypatch):
    """투표는 봇의 운영 상태다. 아카이브(답변 근거)에 섞이면 원칙 1 위반이다."""
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setenv("ARCHIVE_DIR", str(archive))
    monkeypatch.delenv("STATE_DIR", raising=False)

    poll = make()
    polls.save(poll)
    assert list(archive.rglob("*.json")) == []
    assert polls.poll_path(poll.workspace, poll.id).exists()


def test_poll_id_cannot_escape_state_dir(tmp_path, monkeypatch):
    """투표 id 는 버튼 값에서 오므로 경로로 해석되면 안 된다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    path = polls.poll_path("fin", "../../etc/passwd")
    assert path.parent == tmp_path / "polls" / "fin"
    assert ".." not in path.name
