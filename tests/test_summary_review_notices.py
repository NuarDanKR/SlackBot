from datetime import datetime
from types import SimpleNamespace

import pytest

from tybot import summary_review as sr
from tybot.summary_review_notices import NoticeStore, send_status


class Ledger:
    def __init__(self):
        self.rows = {}

    def claim(self, workspace, channel_id, recipient, on, category, reason):
        key = (workspace, channel_id, recipient, on, category)
        if key in self.rows:
            return False
        self.rows[key] = "sending"
        return True

    def finish(self, workspace, channel_id, recipient, on, category, state):
        self.rows[workspace, channel_id, recipient, on, category] = state


class Client:
    def __init__(self, error=None):
        self.posts = []
        self.error = error

    def conversations_open(self, **kwargs):
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        if self.error:
            raise self.error
        return {"ts": "1.0"}


def send(ledger, client, *, reason="model-empty", workspace="tyit", recipients=None):
    return send_status(
        SimpleNamespace(rollback=lambda: None), client,
        workspace=workspace, channel_id="C1", label="channel",
        recipients=["U1"] if recipients is None else recipients,
        reason=reason, now=datetime(2026, 10, 15, 13, tzinfo=sr.KST), store=ledger,
    )


def test_empty_status_is_plain_text_without_approval_buttons():
    client = Client()
    assert send(Ledger(), client) == (1, 0)
    assert client.posts[0]["blocks"][0]["text"]["type"] == "plain_text"
    assert "검토할 요약 후보가 없습니다" in client.posts[0]["blocks"][0]["text"]["text"]
    assert "button" not in repr(client.posts)


def test_empty_status_once_per_channel_reviewer_day_even_after_restart():
    ledger = Ledger()
    assert send(ledger, Client()) == (1, 0)
    assert send(ledger, Client()) == (0, 0)
    assert send(ledger, Client(), reason="no-new-source") == (0, 0)


def test_error_after_empty_is_not_hidden_by_empty_notification():
    ledger, client = Ledger(), Client()
    send(ledger, client)
    assert send(ledger, client, reason="generate-failed") == (1, 0)
    assert send(ledger, client, reason="no-accepted-candidate") == (0, 0)
    assert "실패" in client.posts[-1]["blocks"][0]["text"]["text"]


def test_workspace_and_recipient_ledgers_are_separate():
    ledger = Ledger()
    assert send(ledger, Client()) == (1, 0)
    assert send(ledger, Client(), workspace="mgmt") == (1, 0)
    assert send(ledger, Client(), recipients=["U2", "U2"]) == (1, 0)


def test_unknown_skip_reason_does_not_claim_or_notify():
    ledger = Ledger()
    assert send(ledger, Client(), reason="source-already-processed") == (0, 0)
    assert not ledger.rows


def test_missing_sender_does_not_record_success():
    ledger = Ledger()
    assert send(ledger, None) == (0, 1)
    assert not ledger.rows


def test_timeout_after_post_is_ambiguous_and_not_retried():
    ledger, client = Ledger(), Client(TimeoutError())
    assert send(ledger, client) == (0, 1)
    assert list(ledger.rows.values()) == ["ambiguous"]
    assert send(ledger, client) == (0, 0)
    assert len(client.posts) == 1


def test_crash_with_durable_sending_record_does_not_duplicate():
    ledger = Ledger()
    ledger.claim("tyit", "C1", "U1", datetime(2026, 10, 15).date(), "empty", "model-empty")
    assert send(ledger, Client()) == (0, 0)


def test_open_failure_is_safe_to_retry_later():
    class Broken(Client):
        def conversations_open(self, **kwargs):
            raise TimeoutError()

    ledger = Ledger()
    assert send(ledger, Broken()) == (0, 1)
    assert list(ledger.rows.values()) == ["failed"]


def test_claim_is_atomic_and_separate_from_approval_tables():
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, args):
            calls.append(sql)

        def fetchone(self):
            return {"workspace": "tyit"}

    conn = SimpleNamespace(cursor=Cursor, commit=lambda: None)
    assert NoticeStore(conn).claim("tyit", "C1", "U1", "2026-10-15", "empty", "model-empty")
    assert len(calls) == 1
    assert "ON CONFLICT" in calls[0] and "RETURNING" in calls[0]
    assert "interval '1 hour'" in calls[0]
    assert "review_digest_sent" not in calls[0]
    assert "summary_review_delivery" not in calls[0]


@pytest.mark.parametrize("minutes, expected", [(5, False), (15, True), (60, True)])
def test_generation_cooldown(minutes, expected):
    from datetime import timedelta

    now = datetime(2026, 10, 15, 13, tzinfo=sr.KST)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            pass

        def fetchone(self):
            return {"checked_at": now - timedelta(minutes=minutes)}

    assert sr.Store(SimpleNamespace(cursor=Cursor)).generation_due("tyit", "C1", now) is expected
