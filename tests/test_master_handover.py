"""인수한 채널에서 **Master 가 손을 뗀다.**

결정: 2026-09-30 오너 — Master 수집 기능 분리 준비.

판정 규칙은 `test_write_owner.py` 가 본다. 여기서 보는 것은 **수집 경로가 그
판정을 지나는가** 다. 규칙만 맞고 부르는 곳이 없으면 아무것도 바뀌지 않는다 —
실제로 `owns_write()` 가 그 상태로 오래 있었다.

Master 의 운영 원문 쓰기 자리는 셋이다.

| 자리 | 언제 |
|---|---|
| `WorkspaceBot._ingest_live` | 실시간 이벤트 |
| `WorkspaceBot._ingest_channel` | 사람이 시킨 취합 |
| `collect.collect_workspace` | 정시 잡 |

답변·실시간 조회는 건드리지 않는다. 막는 것은 **쓰기뿐**이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from tybot import collect
from tybot.archive import writer
from tybot.archive.archiving_state import ChannelMode, ChannelState, WriterOwner
from tybot.archive.write_owner import Decision, decide
from tybot.slack.pilot import WorkspaceBot

CHANNEL = "#팀-전산_ABB110-회의"


class FakeLookup:
    """표 대신 정해진 상태를 돌려준다. 판정 자체는 진짜 `decide` 가 한다."""

    def __init__(self, state: ChannelState | None) -> None:
        self.state = state
        self.asked: list[str] = []

    def master_may_write(self, channel_id: str) -> Decision:
        self.asked.append(channel_id)
        return decide(self.state, WriterOwner.MASTER)


def _handed_over() -> ChannelState:
    return ChannelState("pilot", "C1", ChannelMode.ACTIVE, WriterOwner.ARCHIVER,
                        "1759100000.000100")


def _still_ours() -> ChannelState:
    return ChannelState("pilot", "C1", ChannelMode.SHADOW, WriterOwner.MASTER)


def _result(tmp_path) -> writer.IngestResult:
    return writer.IngestResult(
        path=tmp_path / "raw.md", written=1, skipped_bot=0, refused=[],
    )


def _bot(tmp_path, state: ChannelState | None) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.archive_dir = str(tmp_path)
    bot.workspace = "pilot"
    bot.bot_name = "tybot"
    bot._ingested = 0
    bot._last_ingest_at = None
    bot._owner_lookup = FakeLookup(state)
    bot._channel_name = Mock(return_value=CHANNEL)
    bot._messages_from = Mock(return_value=[
        writer.IncomingMessage(ts=datetime.now(UTC), speaker="사용자", text="원문"),
    ])
    bot._confirm_attachments = Mock()
    bot.cfg = Mock(bot_token="xoxb-테스트")
    bot.store = Mock()
    return bot


# --- 실시간 ----------------------------------------------------------------------

def test_a_handed_over_channel_is_not_written_in_realtime(tmp_path):
    """인수 뒤에도 계속 쓰면 같은 대화가 두 봇의 손으로 두 번 들어간다."""
    bot = _bot(tmp_path, _handed_over())

    with patch("tybot.slack.pilot.writer.ingest") as ingest:
        bot._ingest_live(Mock(), {"channel": "C1", "ts": "1759200000.000100"})

    ingest.assert_not_called()
    assert bot._owner_lookup.asked == ["C1"]


def test_a_channel_we_still_own_is_written_in_realtime(tmp_path):
    """전환 전에는 Master 가 계속 쓴다. 문지기가 전부를 막으면 그게 사고다."""
    bot = _bot(tmp_path, _still_ours())

    with patch("tybot.slack.pilot.writer.ingest") as ingest:
        ingest.return_value = _result(tmp_path)
        bot._ingest_live(Mock(), {"channel": "C1", "ts": "1759200000.000100"})

    ingest.assert_called_once()


def test_an_unknown_channel_is_still_written_in_realtime(tmp_path):
    """표에 없는 채널까지 막으면 아직 아무것도 안 옮겼는데 수집이 멈춘다."""
    bot = _bot(tmp_path, None)

    with patch("tybot.slack.pilot.writer.ingest") as ingest:
        ingest.return_value = _result(tmp_path)
        bot._ingest_live(Mock(), {"channel": "C1", "ts": "1759200000.000100"})

    ingest.assert_called_once()


def test_the_gate_runs_before_slack_is_read(tmp_path):
    """막을 채널의 본문을 먼저 읽으면 rate limit 만 쓰고 버린다."""
    bot = _bot(tmp_path, _handed_over())

    bot._ingest_live(Mock(), {"channel": "C1", "ts": "1759200000.000100"})

    bot._messages_from.assert_not_called()


# --- 사람이 시킨 취합 --------------------------------------------------------------

def test_a_handed_over_channel_refuses_the_manual_backfill(tmp_path):
    """조용히 넘어가면 사람은 봇이 고장난 줄 안다."""
    bot = _bot(tmp_path, _handed_over())
    client = Mock()

    message = bot._ingest_channel(client, "C1")

    assert "Archiving Bot" in message
    client.conversations_history.assert_not_called()


def test_a_channel_we_still_own_runs_the_manual_backfill(tmp_path, monkeypatch):
    bot = _bot(tmp_path, _still_ours())
    client = Mock()
    client.conversations_history.return_value = {"messages": []}
    monkeypatch.setattr("tybot.slack.pilot.canvas_lines",
                        lambda *a, **k: Mock(lines=[], file_ids=[], warnings=[]))

    bot._ingest_channel(client, "C1")

    client.conversations_history.assert_called_once()


# --- 정시 잡 ----------------------------------------------------------------------

class FakeSlack:
    def __init__(self) -> None:
        self.history_calls: list[str] = []

    def conversations_list(self, **_kw):
        return {
            "channels": [
                {"id": "C1", "name": "팀-전산_ABB110-회의", "is_member": True},
            ],
            "response_metadata": {},
        }

    def conversations_history(self, channel, **_kw):
        self.history_calls.append(channel)
        return {"messages": []}


@pytest.fixture
def batch(monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr("slack_sdk.WebClient", lambda token: slack)
    return slack


def _run_batch(tmp_path, state, monkeypatch, batch):
    monkeypatch.setattr(collect, "OwnerLookup", lambda workspace: FakeLookup(state))
    cfg = Mock(key="pilot", bot_token="xoxb-테스트")
    return collect.collect_workspace(cfg, str(tmp_path), pace=0), batch


def test_the_hourly_job_skips_a_handed_over_channel(tmp_path, monkeypatch, batch):
    """정시 잡이 계속 메우면 인수 이후 구간을 두 봇이 쓴다."""
    stats, slack = _run_batch(tmp_path, _handed_over(), monkeypatch, batch)

    assert slack.history_calls == []
    assert stats["skipped_owner"] == 1


def test_the_hourly_job_counts_handover_apart_from_the_name_rule(tmp_path,
                                                                 monkeypatch, batch):
    """합쳐 세면 「왜 이 채널이 안 들어오나」 를 로그로 구분할 수 없다."""
    stats, _ = _run_batch(tmp_path, _handed_over(), monkeypatch, batch)

    assert stats["skipped_rule"] == 0


def test_the_hourly_job_still_runs_on_our_channels(tmp_path, monkeypatch, batch):
    stats, slack = _run_batch(tmp_path, _still_ours(), monkeypatch, batch)

    assert slack.history_calls == ["C1"]
    assert stats["skipped_owner"] == 0


# --- 답변·조회는 그대로 -----------------------------------------------------------

def test_reading_paths_do_not_ask_who_owns_the_writer():
    """막는 것은 쓰기뿐이다. 조회까지 막으면 인수 중에 답변이 사라진다.

    인수는 **누가 쓰나**를 정할 뿐, 누가 읽나를 정하지 않는다. 읽기 권한은
    `access.RequestContext` 가 본다(절대 원칙 3).
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "tybot"
    for name in ("answer.py", "specialist_tools.py", "orgsearch.py",
                 "thread_followup.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "write_owner" not in source, name
        assert "master_may_write" not in source, name
