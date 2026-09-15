"""초대 이전 Slack 원문 소급 수집의 페이지·스레드·체크포인트 경계."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_channel_history as history


class FakeClient:
    def __init__(self):
        self.history_calls = []
        self.reply_calls = []

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        if len(self.history_calls) == 1:
            return {
                "messages": [
                    {"ts": "20.0", "user": "U1", "text": "둘", "reply_count": 1},
                    {"ts": "10.0", "user": "U2", "text": "하나"},
                ],
                "has_more": True,
            }
        return {
            "messages": [{"ts": "5.0", "user": "U3", "text": "과거"}],
            "has_more": False,
        }

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        return {
            "messages": [
                {"ts": "20.0", "user": "U1", "text": "둘"},
                {"ts": "21.0", "user": "U4", "text": "답글"},
                {"ts": "22.0", "bot_id": "B1", "text": "봇 답변"},
            ],
            "response_metadata": {},
        }


def test_history_pages_include_threads_and_resume_boundary(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda **_: client))
    batches = []
    monkeypatch.setattr(
        history,
        "_ingest_events",
        lambda cfg, channel, archive, api, events: (batches.append(events) or len(events), set()),
    )
    monkeypatch.setattr(history, "_sync_canvas", lambda *args: (0, set()))
    monkeypatch.setattr(history, "_sync_files", lambda *args: (0, 0, set()))
    checkpoints = history.Checkpoints(tmp_path / "state.json")
    cfg = SimpleNamespace(key="tyit", bot_token="token")
    channel = {"id": "C1", "name": "팀-전산_abb155-업무"}

    stats = history.backfill_channel(
        cfg, channel, str(tmp_path / "archive"), checkpoints, pace=0
    )

    assert stats.pages == 2
    assert stats.messages == 4
    assert client.history_calls[1]["latest"] == "10.0"
    assert client.history_calls[1]["inclusive"] is False
    assert client.reply_calls[0]["ts"] == "20.0"
    assert all(not event.get("bot_id") for batch in batches for event in batch)
    state = checkpoints.get("tyit", "C1")
    assert state["history_complete"] is True
    assert state["latest"] == "5.0"
    assert state["canvas_complete"] is True
    assert state["files_complete"] is True


def test_thread_failure_is_saved_for_a_later_retry(tmp_path, monkeypatch):
    client = FakeClient()
    client.conversations_replies = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("rate"))
    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda **_: client))
    monkeypatch.setattr(history, "_ingest_events", lambda *args: (0, set()))
    monkeypatch.setattr(history, "_sync_canvas", lambda *args: (0, set()))
    monkeypatch.setattr(history, "_sync_files", lambda *args: (0, 0, set()))
    checkpoints = history.Checkpoints(tmp_path / "state.json")

    stats = history.backfill_channel(
        SimpleNamespace(key="tyit", bot_token="token"),
        {"id": "C1", "name": "팀-전산_abb155-업무"},
        str(tmp_path / "archive"),
        checkpoints,
        pace=0,
        max_pages=1,
    )

    assert stats.failures == 1
    assert checkpoints.get("tyit", "C1")["pending_threads"] == ["20.0"]


def test_bot_and_system_messages_are_never_accepted():
    assert history._accepted({"ts": "1", "user": "U1"}) is True
    assert history._accepted({"ts": "1", "bot_id": "B1"}) is False
    assert history._accepted({"ts": "1", "subtype": "channel_join"}) is False


def test_rate_limit_honours_retry_after(monkeypatch):
    class Limited(Exception):
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "7"})

    calls = []

    def request():
        calls.append(True)
        if len(calls) == 1:
            raise Limited
        return {"ok": True}

    sleeps = []
    monkeypatch.setattr(history.time, "sleep", sleeps.append)

    assert history._paced_call(history.ApiPacer(0), "history", request) == {"ok": True}
    assert sleeps == [7.0]
