"""`/피드백` — 대상 메시지를 고를 수 없을 때의 신고 입구.

리액션(:+1:/:-1:)·`정정:` 답글과 **같은 로그**에 쌓아야 한다. 입구마다 저장소가
갈라지면 품질 검토에서 한쪽을 놓친다.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

from tybot.audit import QALog, QARecord
from tybot.feedback import FeedbackLog
from tybot.slack.pilot import WorkspaceBot


def _bot(tmp_path) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "pilot"
    bot.bot_name = "tybot"
    bot.qa_log = QALog(tmp_path, write_md=False)
    bot.feedback_log = FeedbackLog(tmp_path)
    return bot


def _answered(bot, *, user="U1", channel_id="C1", question="김해외동 기성금 얼마야"):
    rec = QARecord.build(
        workspace="pilot", channel="#현장-김해외동_180182-채팅방", channel_id=channel_id,
        user=user, user_name="단라운", question=question, intent_kind="search",
        intent_source="llm", reason="answered", hits=2, scope="채널 1개",
        answer="15억입니다.", response_ts="1724740000.0001",
    )
    bot.qa_log.write(rec)
    return rec


def _events(tmp_path) -> list[dict]:
    out = []
    for path in tmp_path.glob("feedback-*.jsonl"):
        out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return out


def test_empty_text_never_records_an_empty_report(tmp_path):
    """모달을 못 열었을 때의 안전망 - 빈 신고를 남기지 않는다."""
    bot = _bot(tmp_path)
    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="")
    assert "선택 화면" in reply
    assert _events(tmp_path) == []


def test_usage_leads_with_the_modal_then_the_other_entry_points(tmp_path):
    """모달이 주 입구다. 다른 입구는 뒤에 안내한다."""
    bot = _bot(tmp_path)
    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="")
    assert reply.index("선택 화면") < reply.index("정정:")
    assert "@tybot" in reply  # 봇 이름이 채워진다


def test_report_is_linked_to_the_users_last_answer(tmp_path):
    bot = _bot(tmp_path)
    rec = _answered(bot)
    reply = bot._record_slash_feedback(
        user_id="U1", channel_id="C1", text="다른 현장 내용이 나왔어요"
    )

    (event,) = _events(tmp_path)
    assert event["qa_record_id"] == rec.record_id
    assert event["kind"] == "correction"
    assert event["text"] == "다른 현장 내용이 나왔어요"
    assert "김해외동" in reply  # 어떤 질문에 연결했는지 보여준다


def test_links_only_to_the_reporters_own_answer(tmp_path):
    """남의 질문에 신고가 붙으면 그 사람의 질문 내용이 신고자에게 노출된다."""
    bot = _bot(tmp_path)
    _answered(bot, user="U2", question="다른 사람 질문")

    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="이상해요")

    (event,) = _events(tmp_path)
    assert event["qa_record_id"] == ""
    assert "다른 사람 질문" not in reply
    assert "찾지 못해" in reply


def test_records_even_without_a_linked_answer(tmp_path):
    """연결 못 해도 신고 자체는 남긴다 - 버리면 사용자는 말했는데 사라진다."""
    bot = _bot(tmp_path)
    bot._record_slash_feedback(user_id="U1", channel_id="C1", text="봇이 느려요")

    (event,) = _events(tmp_path)
    assert event["text"] == "봇이 느려요"
    assert event["actor"] == "U1"


def test_reply_states_the_report_is_not_used_as_evidence(tmp_path):
    """신고 내용이 답변 근거가 되면 요약 재귀다 - 사용자에게도 명시한다."""
    bot = _bot(tmp_path)
    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="틀렸어요")
    assert "아카이브에 저장되지 않으며" in reply


def test_last_answer_lookup_ignores_other_channels(tmp_path):
    bot = _bot(tmp_path)
    _answered(bot, channel_id="C-OTHER")
    assert bot.qa_log.last_answer_for_user("pilot", "C1", "U1") is None


def test_last_answer_lookup_returns_the_most_recent(tmp_path):
    bot = _bot(tmp_path)
    _answered(bot, question="첫 질문")
    second = _answered(bot, question="둘째 질문")
    row = bot.qa_log.last_answer_for_user("pilot", "C1", "U1")
    assert row["record_id"] == second.record_id


def test_collection_status_uses_channel_info(tmp_path):
    bot = _bot(tmp_path)
    bot.store = Mock(docs=lambda: [])
    bot.autojoin = True
    bot.realtime = True
    bot.path_problems = {}
    bot._chan_cache = {"C1": "#팀-전산_ABB110-회의"}
    bot._channel_name = lambda client, cid: bot._chan_cache.get(cid, cid)

    client = Mock()
    client.conversations_info.return_value = {"channel": {"is_private": True, "is_member": False}}

    text = bot._collection_status(client, "C1")
    assert "스스로 들어갈 수 없습니다" in text


def test_collection_status_survives_api_failure(tmp_path):
    """조회에 실패해도 이름만으로 답할 수 있어야 한다."""
    bot = _bot(tmp_path)
    bot.store = Mock(docs=lambda: [])
    bot.autojoin = True
    bot.realtime = True
    bot.path_problems = {}
    bot._channel_name = lambda client, cid: "#점심메뉴"

    client = Mock()
    client.conversations_info.side_effect = RuntimeError("missing_scope")

    text = bot._collection_status(client, "C1")
    assert "이름이 표준 규칙과 다릅니다" in text


# --- 모달(주 입구) ---------------------------------------------------------
# 리액션은 답변 메시지에 마우스를 올려 이모지를 찾아야 해서 번거롭고, 모바일에서는 더
# 그렇다. `/피드백` 만 입력하면 선택 화면이 뜨는 쪽이 실제로 더 많이 쓰인다.
def test_modal_offers_three_outcomes():
    from tybot.feedback import KIND_CHOICES, feedback_modal

    view = feedback_modal("{}", target="`김해외동 기성금`")
    values = [o["value"] for o in view["blocks"][1]["element"]["options"]]
    assert values == [v for v, _ in KIND_CHOICES]
    assert view["callback_id"] == "tybot_feedback"


def test_modal_shows_which_answer_it_targets():
    from tybot.feedback import feedback_modal

    view = feedback_modal("{}", target="`김해외동 기성금`")
    assert "김해외동" in view["blocks"][0]["elements"][0]["text"]


def test_modal_says_when_no_answer_is_linked():
    """무엇에 붙는지 모르면 사용자가 오해한다."""
    from tybot.feedback import feedback_modal

    view = feedback_modal("{}")
    assert "찾지 못했습니다" in view["blocks"][0]["elements"][0]["text"]


def test_modal_detail_is_optional():
    """선택만 하고 닫을 수 있어야 리액션보다 빠르다."""
    from tybot.feedback import feedback_modal

    detail = next(b for b in feedback_modal("{}")["blocks"] if b.get("block_id") == "detail")
    assert detail["optional"] is True


def test_modal_states_the_report_is_not_evidence():
    from tybot.feedback import feedback_modal

    texts = [
        e["text"]
        for b in feedback_modal("{}")["blocks"]
        for e in b.get("elements", [])
    ]
    assert any("답변 근거로도 쓰이지 않습니다" in x for x in texts)


def test_unknown_selection_is_not_counted_as_praise():
    """모르는 값을 positive 로 두면 문제 신고가 칭찬으로 집계된다."""
    from tybot.feedback import from_view

    kind, _ = from_view({"state": {"values": {"kind": {"kind": {"selected_option": {"value": "??"}}}}}})
    assert kind == "negative"


def test_missing_is_distinct_from_negative():
    """근거를 못 찾은 것은 수집·검색 문제, 틀린 답은 생성 문제 - 조치하는 사람이 다르다."""
    from tybot.feedback import KINDS

    assert {"missing", "negative"} <= KINDS


def test_feedback_log_accepts_missing_kind(tmp_path):
    bot = _bot(tmp_path)
    bot.feedback_log.write(
        workspace="pilot", channel_id="C1", qa_record_id="r1", answer_ts="1.0",
        actor="U1", kind="missing", action="submitted", text="0건이라고 나왔어요",
    )
    (event,) = _events(tmp_path)
    assert event["kind"] == "missing"


def test_feedback_log_still_rejects_unknown_kind(tmp_path):
    import pytest

    bot = _bot(tmp_path)
    with pytest.raises(ValueError, match="지원하지 않는"):
        bot.feedback_log.write(
            workspace="pilot", channel_id="C1", qa_record_id="r1", answer_ts="1.0",
            actor="U1", kind="아무거나", action="submitted",
        )


def test_thanks_names_what_was_recorded():
    from tybot.feedback import thanks

    assert "근거를 못 찾은 사례로 접수" in thanks("missing", linked="연결됨")
    assert "정확했다는 의견으로 접수" in thanks("positive", linked="연결됨")
