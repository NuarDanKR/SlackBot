"""답변 피드백은 QA 기록에 연결되고 아카이브 근거와 분리된다."""
from __future__ import annotations

import json

from tybot.archive.store import ArchiveStore
from tybot.audit import QALog, QARecord
from tybot.feedback import FeedbackLog, correction_text, reaction_kind
from tybot.slack.pilot import WorkspaceBot


def _qa(**overrides) -> QARecord:
    values = dict(
        workspace="mgmt",
        channel="#경영-회의",
        channel_id="C1",
        user="U1",
        user_name="사용자",
        question="전산팀 현황은?",
        intent_kind="summary",
        intent_source="llm",
        reason="answered",
        hits=2,
        scope="채널 3개",
        answer="서버 이관 중입니다.",
        request_ts="100.1",
        response_ts="100.2",
        thread_ts="100.1",
        channel_type="channel",
    )
    values.update(overrides)
    return QARecord.build(**values)


def _rows(root):
    path = next(root.glob("feedback-*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_qa_answer_can_be_found_by_response_or_thread(tmp_path):
    log = QALog(tmp_path, write_md=False)
    record = _qa()
    log.write(record)

    assert log.find_answer("mgmt", "C1", response_ts="100.2")["record_id"] == record.record_id
    assert log.find_answer("mgmt", "C1", thread_ts="100.1")["record_id"] == record.record_id
    assert log.find_answer("pilot", "C1", response_ts="100.2") is None


def test_feedback_log_references_qa_without_copying_answer(tmp_path):
    feedback = FeedbackLog(tmp_path / "qa-log")
    feedback.write(
        workspace="mgmt",
        channel_id="C1",
        qa_record_id="qa-1",
        answer_ts="100.2",
        actor="U2",
        kind="negative",
        action="added",
    )

    body = next((tmp_path / "qa-log").glob("feedback-*.jsonl")).read_text(encoding="utf-8")
    assert "qa-1" in body
    assert "서버 이관" not in body
    assert ArchiveStore(tmp_path / "archive").docs() == []


def test_reaction_and_correction_parsing():
    assert reaction_kind("+1") == "positive"
    assert reaction_kind("-1") == "negative"
    assert reaction_kind("eyes") is None
    assert correction_text("정정: 실제 완료일은 8월 27일") == "실제 완료일은 8월 27일"
    assert correction_text("일반 질문") is None


def test_workspace_bot_records_only_reactions_to_known_answers(tmp_path):
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.qa_log = QALog(tmp_path, write_md=False)
    bot.feedback_log = FeedbackLog(tmp_path)
    record = _qa()
    bot.qa_log.write(record)

    bot._handle_feedback_reaction(
        {
            "reaction": "-1",
            "user": "U2",
            "item": {"type": "message", "channel": "C1", "ts": "100.2"},
        },
        action="added",
    )
    bot._handle_feedback_reaction(
        {
            "reaction": "-1",
            "user": "U2",
            "item": {"type": "message", "channel": "C1", "ts": "unknown"},
        },
        action="added",
    )

    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "negative"
    assert rows[0]["qa_record_id"] == record.record_id


def test_correction_requires_matching_thread_and_records_human_text(tmp_path):
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.qa_log = QALog(tmp_path, write_md=False)
    bot.feedback_log = FeedbackLog(tmp_path)
    bot.qa_log.write(_qa())
    sent = []

    handled = bot._handle_correction(
        {
            "text": "<@B1> 정정: 서버 이관은 아직 시작 전입니다",
            "user": "U2",
            "channel": "C1",
            "thread_ts": "100.1",
        },
        lambda **kwargs: sent.append(kwargs),
    )

    assert handled is True
    assert "기록했습니다" in sent[0]["text"]
    (row,) = _rows(tmp_path)
    assert row["kind"] == "correction"
    assert row["text"] == "서버 이관은 아직 시작 전입니다"
