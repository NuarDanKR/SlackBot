"""답변 피드백은 QA 기록에 연결되고 아카이브 근거와 분리된다."""
from __future__ import annotations

import json

from tybot import feedback
from tybot.archive.store import ArchiveStore
from tybot.audit import QALog, QARecord
from tybot.feedback import FeedbackLog, correction_text
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


def test_correction_parsing():
    assert correction_text("정정: 실제 완료일은 8월 27일") == "실제 완료일은 8월 27일"
    assert correction_text("일반 질문") is None


def test_feedback_entry_is_only_the_command():
    """입구가 둘이면 어느 쪽이 접수됐는지 사용자가 모른다. `/피드백` 하나로 합쳤다."""
    assert not hasattr(WorkspaceBot, "_handle_feedback_reaction")
    assert not hasattr(feedback, "reaction_kind")


def test_modal_offers_thumbs_choices():
    """사람들이 이미 아는 기호를 그대로 쓴다."""
    modal = feedback.feedback_modal("{}")
    kind_block = next(b for b in modal["blocks"] if b.get("block_id") == "kind")
    labels = [o["text"]["text"] for o in kind_block["element"]["options"]]
    assert any("👍" in x for x in labels)
    assert any("👎" in x for x in labels)


def test_negative_feedback_requires_a_correction():
    """'틀렸다'만 눌리고 끝나면 고칠 거리가 없다. 숫자만 나빠진다."""
    assert feedback.validation_errors("negative", "") is not None
    assert feedback.validation_errors("missing", "   ") is not None
    assert feedback.validation_errors("negative", "가") is not None, "너무 짧으면 막는다"
    assert feedback.validation_errors("negative", "기성금은 3억 2천만원입니다") is None


def test_praise_does_not_require_typing():
    """👍 에까지 글을 요구하면 아무도 누르지 않는다."""
    assert feedback.validation_errors("positive", "") is None


def test_validation_message_says_what_to_write():
    errors = feedback.validation_errors("negative", "")
    assert "올바른 내용" in errors["detail"]
    # Slack 은 block_id 로 칸을 찾는다. 이름이 틀리면 오류가 화면에 안 뜬다.
    assert set(errors) == {"detail"}


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
