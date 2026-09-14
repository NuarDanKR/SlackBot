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


# --- 자기 요청 반려 (2026-09-14) -----------------------------------------------
#
# 자기 제안을 **거두는 것**은 변경을 적용하는 것이 아니라 없애는 것이다. 2인 검토는
# 변경이 들어가는 것을 막으려는 규칙이라, 반려에는 적용할 이유가 없다.
#
# 막아 두니 요청자가 자기 요청을 취소하지 못해 큐에 영원히 남았고, 고쳐서 다시 올리는
# 길까지 함께 막혔다. 관리자가 세 명이어도 자기 것을 못 거둔다.
def _own_request(**over):
    row = {
        "id": 17,
        "workspace": "tyit",
        "requester": "dan@taeyoung.com",
        "state": "awaiting_approval",
        "commit_sha": "abc",
    }
    row.update(over)
    return FakeCursor(row)


def test_requester_can_reject_own_deployment(monkeypatch):
    cursor = _own_request()
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    got = store.decide_request(
        request_id=17, approver="DAN@taeyoung.com", decision="reject", note="다시 올림",
    )
    assert got["approved"] is False
    assert any("state = 'rejected'" in sql for sql in cursor.statements)


def test_self_approval_is_still_blocked_by_default(monkeypatch):
    """반려를 연 것이 승인까지 연 것은 아니다."""
    cursor = _own_request()
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    with pytest.raises(store.DeployApprovalError, match="직접 승인"):
        store.decide_request(
            request_id=17, approver="dan@taeyoung.com", decision="approve", note="",
        )
    assert not any("UPDATE deploy_request" in sql for sql in cursor.statements)


def test_self_approval_is_possible_when_explicitly_allowed(monkeypatch):
    """관리자 예외. 막는 대신 남긴다 — 서버에 들어가면 콘솔을 우회할 수 있다."""
    cursor = _own_request()
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    got = store.decide_request(
        request_id=17, approver="dan@taeyoung.com", decision="approve",
        note="", allow_self=True,
    )
    assert got["approved"] is True


def test_self_decision_is_logged(monkeypatch, caplog):
    """막지 않는 대신 누가 자기 것을 처리했는지 남는다."""
    cursor = _own_request()
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    with caplog.at_level("WARNING", logger="tybot.console.deploy_approval_store"):
        store.decide_request(
            request_id=17, approver="dan@taeyoung.com", decision="reject", note="",
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "자기 배포 요청 처리" in text
    assert "request=17" in text


def test_another_persons_request_is_not_logged_as_self(monkeypatch, caplog):
    cursor = _own_request(requester="other@taeyoung.com")
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))

    with caplog.at_level("WARNING", logger="tybot.console.deploy_approval_store"):
        store.decide_request(
            request_id=17, approver="dan@taeyoung.com", decision="reject", note="",
        )
    assert "자기 배포 요청 처리" not in " ".join(
        r.getMessage() for r in caplog.records
    )
