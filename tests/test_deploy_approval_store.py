from __future__ import annotations

import pytest

from tybot.console import deploy_approval_store as store


class FakeCursor:
    def __init__(self, row: dict):
        self.row = row
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, _params=()):
        self.statements.append(sql)

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.fake_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.fake_cursor


def test_failed_deployment_does_not_record_false_rollback(monkeypatch):
    cursor = FakeCursor({})
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    store.mark_result(17, "failed", "system", "tests failed")

    assert any("UPDATE deploy_request" in sql for sql in cursor.statements)
    assert not any("INSERT INTO deploy_event" in sql for sql in cursor.statements)


def test_requester_cannot_approve_own_deployment(monkeypatch):
    cursor = FakeCursor(
        {
            "id": 17,
            "workspace": "tyit",
            "requester": "dan@taeyoung.com",
            "state": "awaiting_approval",
            "commit_sha": "abc",
        }
    )
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    with pytest.raises(store.DeployApprovalError, match="직접 승인"):
        store.decide_request(
            request_id=17,
            approver="DAN@taeyoung.com",
            decision="approve",
            note="",
        )

    assert not any("UPDATE deploy_request" in sql for sql in cursor.statements)


def test_only_waiting_request_can_be_decided(monkeypatch):
    cursor = FakeCursor(
        {
            "id": 17,
            "workspace": "tyit",
            "requester": "developer@taeyoung.com",
            "state": "live",
            "commit_sha": "abc",
        }
    )
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    with pytest.raises(store.DeployApprovalError, match="이미 처리"):
        store.decide_request(
            request_id=17,
            approver="admin@taeyoung.com",
            decision="approve",
            note="",
        )
