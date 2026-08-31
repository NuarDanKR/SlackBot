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


def test_empty_text_shows_usage_not_an_empty_record(tmp_path):
    bot = _bot(tmp_path)
    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="")
    assert "사용법" in reply
    assert _events(tmp_path) == []


def test_usage_mentions_the_other_two_entry_points(tmp_path):
    bot = _bot(tmp_path)
    reply = bot._record_slash_feedback(user_id="U1", channel_id="C1", text="")
    assert ":+1:" in reply
    assert "정정:" in reply
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
