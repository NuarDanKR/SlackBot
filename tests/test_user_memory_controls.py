from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot.intent import classify_by_rule
from tybot.slack.pilot import WorkspaceBot
from tybot.user_memory import UserMemory
from tybot.user_memory_service import MemoryListResult, RevokeResult


def _bot(service) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot._user_memory_service_obj = service
    bot.engine = SimpleNamespace(router=None)
    bot.qa_log = Mock(recent_for_user=Mock(return_value=[]))
    return bot


@pytest.mark.parametrize(
    "text",
    ["내 기억 보여줘", "저장된 기억 목록", "기억 삭제 17", "기억 철회 #17"],
)
def test_personal_memory_controls_use_the_memory_intent(text):
    assert classify_by_rule(text).kind == "memory"


def test_user_can_list_only_memories_returned_by_the_exact_scope():
    service = Mock()
    service.list_active.return_value = MemoryListResult(
        memories=(
            UserMemory(
                id=17,
                workspace="tyit",
                user_id="U1",
                kind="preference",
                body="format.length=short",
                normalized="format.length=short",
                expires_at=datetime(2027, 1, 2, tzinfo=UTC),
            ),
        )
    )

    reply = _bot(service)._self_reply(
        "memory", "내 기억 보여줘", client=Mock(), user_id="U1"
    )

    assert "#17" in reply
    assert "짧은 답변" in reply
    service.list_active.assert_called_once_with(workspace="tyit", user_id="U1")


def test_user_can_revoke_only_through_the_exact_scope():
    service = Mock()
    service.revoke_result.return_value = RevokeResult(revoked=True)

    reply = _bot(service)._self_reply(
        "memory", "기억 삭제 17", client=Mock(), user_id="U1"
    )

    assert "철회했습니다" in reply
    service.revoke_result.assert_called_once_with(
        17, workspace="tyit", user_id="U1", actor="U1"
    )


def test_storage_failure_is_not_reported_as_an_empty_memory_list():
    service = Mock()
    service.list_active.return_value = MemoryListResult(unavailable=True)

    reply = _bot(service)._self_reply(
        "memory", "내 기억 보여줘", client=Mock(), user_id="U1"
    )

    assert "확인할 수 없습니다" in reply
    assert "없다는 뜻은 아닙니다" in reply


def test_generic_memory_answer_keeps_bot_prose_out_of_persistent_evidence():
    reply = _bot(Mock())._self_reply(
        "memory", "이전 답변 기억나?", client=Mock(), user_id="U1"
    )

    assert "이전 봇 답변 문장" in reply
    assert "사실 근거로 기억하지 않습니다" in reply
    assert "구조화된 개인 기억" in reply
