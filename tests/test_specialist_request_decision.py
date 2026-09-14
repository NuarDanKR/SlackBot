"""전문 봇 변경 요청의 승인·반려 — 자기 것을 거둘 수 있어야 한다.

2026-09-14: 관리자가 자기가 올린 요청을 **반려조차 못 해서** 고쳐 올리지 못했다.
자기 제안을 거두는 것은 변경을 적용하는 것이 아니라 없애는 것이다. 2인 검토는 변경이
들어가는 것을 막으려는 규칙이라, 반려에는 적용할 이유가 없다.

승인은 그대로 막는다 — 그건 변경이 실제로 들어가는 동작이다.
"""
from __future__ import annotations

import pytest

from tybot.console import specialist_store as store


class FakeCursor:
    def __init__(self, row: dict | None):
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


def _own(**over) -> FakeCursor:
    row = {
        "id": 5,
        "requester": "dan@taeyoung.com",
        "state": "awaiting_approval",
        # 승인 경로가 실제로 읽는 키를 다 채운다. 비면 「자기 승인 금지」 가 아니라
        # KeyError 로 떨어져서, 무엇을 시험했는지 흐려진다.
        "proposal": {
            "key": "hermes", "name": "헤르메스", "domain": "내부 기록",
            "adapter": "hermes", "state": "disabled", "version": "1.0",
            "contractVersion": "v1", "workspaces": ["tyit"],
        },
    }
    row.update(over)
    return FakeCursor(row)


def _decide(monkeypatch, cursor, **kw):
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(cursor))
    return store.decide_request(request_id=5, note="", **kw)


def test_requester_can_reject_own_request(monkeypatch):
    cursor = _own()
    _decide(monkeypatch, cursor, actor="DAN@taeyoung.com", decision="reject")
    assert any("UPDATE specialist_change_request" in s for s in cursor.statements)


def test_requester_still_cannot_approve_own_request(monkeypatch):
    """반려를 연 것이 승인까지 연 것은 아니다."""
    cursor = _own()
    with pytest.raises(store.SpecialistStoreError, match="직접 승인"):
        _decide(monkeypatch, cursor, actor="dan@taeyoung.com", decision="approve")
    assert not any("INSERT INTO specialist_bot" in s for s in cursor.statements)


def test_self_approval_is_possible_when_explicitly_allowed(monkeypatch):
    """관리자 예외. 서버에 들어가면 콘솔을 우회할 수 있으니 막는 대신 남긴다."""
    cursor = _own()
    _decide(
        monkeypatch, cursor, actor="dan@taeyoung.com",
        decision="approve", allow_self=True,
    )
    assert any("INSERT INTO specialist_bot" in s for s in cursor.statements)


def test_self_decision_is_logged(monkeypatch, caplog):
    cursor = _own()
    with caplog.at_level("WARNING", logger="tybot.console.specialist_store"):
        _decide(monkeypatch, cursor, actor="dan@taeyoung.com", decision="reject")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "자기 전문 봇 요청 처리" in text


def test_someone_elses_request_needs_no_exception(monkeypatch):
    cursor = _own(requester="other@taeyoung.com")
    _decide(monkeypatch, cursor, actor="dan@taeyoung.com", decision="approve")
    assert any("INSERT INTO specialist_bot" in s for s in cursor.statements)


def test_only_waiting_requests_can_be_decided(monkeypatch):
    cursor = _own(state="approved")
    with pytest.raises(store.SpecialistStoreError):
        _decide(monkeypatch, cursor, actor="other@taeyoung.com", decision="reject")


@pytest.mark.parametrize("decision", ["cancel", "", "APPROVE "])
def test_unknown_decisions_are_refused(monkeypatch, decision):
    cursor = _own()
    with pytest.raises(store.SpecialistStoreError, match="승인 또는 반려"):
        _decide(monkeypatch, cursor, actor="other@taeyoung.com", decision=decision)
