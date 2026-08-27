"""채널 이름 규칙이 자동 참여뿐 아니라 실제 저장 경계에도 적용되는지 검증."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from tybot.archive import writer
from tybot.slack.pilot import WorkspaceBot


def _bot(tmp_path, channel: str) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.archive_dir = str(tmp_path)
    bot.workspace = "pilot"
    bot._ingested = 0
    bot._last_ingest_at = None
    bot._channel_name = Mock(return_value=channel)
    bot._messages_from = Mock(
        return_value=[
            writer.IncomingMessage(
                ts=datetime.now(UTC),
                speaker="사용자",
                text="원문",
            )
        ]
    )
    return bot


def test_realtime_ingest_rejects_non_rule_channel(tmp_path):
    bot = _bot(tmp_path, "#점심메뉴")
    with patch("tybot.slack.pilot.writer.ingest") as ingest:
        bot._ingest_live(Mock(), {"channel": "C1"})
    ingest.assert_not_called()


def test_manual_ingest_rejects_non_rule_channel_before_slack_history(tmp_path):
    bot = _bot(tmp_path, "#팀_자금(ABB540)_주간보고")
    client = Mock()
    result = bot._ingest_channel(client, "C1")
    assert "수집 규칙과 달라" in result
    client.conversations_history.assert_not_called()


def test_ingest_all_does_not_join_when_autojoin_is_disabled(tmp_path):
    bot = _bot(tmp_path, "#팀-전산_ABB110-회의")
    bot.autojoin = False
    bot._chan_cache = {}
    client = Mock()
    client.conversations_list.return_value = {
        "channels": [
            {
                "id": "C1",
                "name": "팀-전산_ABB110-회의",
                "is_member": False,
                "is_private": False,
            }
        ],
        "response_metadata": {},
    }
    result = bot._ingest_all(client)
    client.conversations_join.assert_not_called()
    assert "자동 참여가 꺼져" in result
