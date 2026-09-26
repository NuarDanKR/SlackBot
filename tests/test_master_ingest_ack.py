"""Master 가 답하기 전에 **수집 ACK 를 본다** — 실제 요청 처리 경로로.

결정: 2026-09-25 오너 §6, 2026-09-26 보완 지시 2.

`ingest_ack.claim` 을 직접 부르는 시험은 함수가 맞는지만 본다. 그런데 정작 문제는
**Master 가 그 함수를 안 부르는 것**이다. 그래서 여기서는 `_handle` 을 통과시키고
사람이 실제로 받는 문장을 본다.

## 왜 첨부에만 붙나

사람은 파일을 올리면서 그 파일에 대해 묻는다. 그때 아무 말도 안 하면 답이 그
파일을 읽고 나온 것처럼 읽힌다. 실제로는 변환이 큐를 지나므로 대개 아직이고,
그 답은 **올리기 전 자료로만** 만든 것이다.

첨부 없는 질문에는 안 붙인다. 매 답변에 수집 상태를 달면 그 줄을 아무도 안 읽게
되고, 정작 필요할 때도 안 읽는다.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

# 같은 harness 를 쓴다 — 여기서 새로 만들면 진짜 경로와 달라진다.
from test_handle_compound import _bot

from tybot.answer import Answer
from tybot.archive import ingest_ack
from tybot.archive.archiving_state import IngestProgress, IngestState
from tybot.intent import Intent

ANSWER = Answer(
    "지난주 기성 청구는 15억입니다.",
    ["#팀_자금(ABB540)_주간보고, 📄2026-09.md(2026-09-20)"],
    "fake", 0.01, 3, "answered",
)


def _ask(bot, *, files=None) -> str:
    """실제 요청 핸들러를 통과시킨다. 돌려주는 것은 **사람이 받는 문장**이다."""
    sent: list[str] = []
    event = {"text": "이 파일 기준으로 기성 얼마야?", "user": "U1", "channel": "C1", "ts": "1.0"}
    if files is not None:
        event["files"] = files
    bot._handle(event, Mock(), lambda **kw: sent.append(kw["text"]), in_channel=True)
    return "\n".join(sent)


@pytest.fixture
def bot():
    return _bot([Intent("search", question="기성 얼마야", terms=["기성"])], [ANSWER])


@pytest.fixture
def ack(monkeypatch):
    """ACK 조회 결과를 시험이 정한다. DB 를 요구하지 않는다."""

    def given(status):
        monkeypatch.setattr(ingest_ack, "read", lambda *_: status)

    return given


def _status(state: IngestState, *, written_to="live", total=0, ready=0):
    return ingest_ack.AckStatus(IngestProgress(state, total, ready), written_to)


FILES = [{"id": "F1", "name": "기성내역.xlsx"}]


# --- 행 없음 ------------------------------------------------------------------

def test_no_ack_row_means_master_does_not_claim_anything(bot, ack):
    """없는 것을 「아직 안 됐다」 로도 말하지 않는다 — 모르면서 아는 척이다."""
    ack(None)

    reply = _ask(bot, files=FILES)

    assert ingest_ack.UNKNOWN_CLAIM in reply
    assert "검색할 수 있습니다" not in reply


# --- DB 장애 ------------------------------------------------------------------

def test_a_database_failure_does_not_block_the_answer(bot, monkeypatch):
    """상태를 못 봤다고 질문에 못 답하면 사람은 봇이 죽은 줄 안다."""
    def boom(*_):
        raise RuntimeError("DB 다운")

    monkeypatch.setattr(ingest_ack, "read", boom)

    reply = _ask(bot, files=FILES)

    assert "15억" in reply, "답변 자체는 나간다"
    assert "검색할 수 있습니다" not in reply


def test_a_database_failure_never_claims_searchable(bot, monkeypatch):
    monkeypatch.setattr(ingest_ack, "_connect", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

    reply = _ask(bot, files=FILES)

    assert "검색할 수 있습니다" not in reply


# --- shadow ------------------------------------------------------------------

def test_shadow_ready_is_reported_as_not_yet_in_operational_search(bot, ack):
    """**이번 보완의 핵심.**

    그림자 수집도 `ready` 가 된다. 그런데 그 파일은 운영 아카이브에 없어서 운영
    검색이 못 찾는다. 「검색할 수 있습니다」 라고 하면 사람이 찾으러 갔다가
    못 찾고, 그때 봇은 거짓말한 것이 된다.
    """
    ack(_status(IngestState.READY, written_to="shadow"))

    reply = _ask(bot, files=FILES)

    assert ingest_ack.SHADOW_CLAIM in reply
    assert "검색할 수 있습니다" not in reply


def test_a_ready_row_without_a_destination_is_treated_as_not_live(bot, ack):
    ack(_status(IngestState.READY, written_to=""))

    reply = _ask(bot, files=FILES)

    assert ingest_ack.SHADOW_CLAIM in reply


# --- partial ------------------------------------------------------------------

def test_partial_tells_how_many_attachments_are_left(bot, ack):
    """숫자를 주면 사람이 기다릴지 말지 정할 수 있다."""
    ack(_status(IngestState.ATTACHMENT_PENDING, total=3, ready=1))

    reply = _ask(bot, files=FILES)

    assert "1/3" in reply
    assert "검색할 수 있습니다" not in reply


def test_partial_state_is_not_searchable(bot, ack):
    ack(_status(IngestState.PARTIAL, written_to="live", total=2, ready=2))

    reply = _ask(bot, files=FILES)

    assert "검색할 수 있습니다" not in reply


# --- ready + live -------------------------------------------------------------

def test_ready_and_live_is_the_only_case_that_claims_searchable(bot, ack):
    ack(_status(IngestState.READY, written_to="live"))

    reply = _ask(bot, files=FILES)

    assert "검색할 수 있습니다" in reply


def test_the_answer_itself_is_untouched_by_the_notice(bot, ack):
    """상태 한 줄이 답변을 밀어내지 않는다. 출처도 그대로 남는다(원칙 2)."""
    ack(_status(IngestState.READY, written_to="live"))

    reply = _ask(bot, files=FILES)

    assert "15억" in reply
    assert "출처:" in reply


# --- 첨부가 없으면 안 붙는다 ---------------------------------------------------

def test_a_question_without_files_gets_no_ingest_notice(bot, ack):
    """매 답변에 달면 그 줄을 아무도 안 읽게 되고, 정작 필요할 때도 안 읽는다."""
    ack(_status(IngestState.READY, written_to="live"))

    reply = _ask(bot)

    assert "첨부 상태" not in reply


def test_an_empty_file_list_gets_no_notice(bot, ack):
    ack(_status(IngestState.READY, written_to="live"))

    assert "첨부 상태" not in _ask(bot, files=[])


# --- 판정이 한 곳에만 있다 -----------------------------------------------------

def test_the_handler_does_not_reimplement_the_rule():
    """「ready 면 검색 가능」 을 여기서 다시 쓰면 규칙이 두 곳에 생긴다.

    특히 그림자도 `ready` 가 되므로, 그 규칙을 두 번 쓰면 한쪽이 틀린다.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent
    text = (source / "src" / "tybot" / "slack" / "pilot.py").read_text(encoding="utf-8")
    notice = text[text.index("def _ingest_notice"):text.index("def _handle_request")]

    assert "IngestState" not in notice
    assert "written_to" not in notice
    assert "ingest_ack.claim" in notice
