from __future__ import annotations

import json
from types import SimpleNamespace

from tybot import specialist_v3_broker as broker
from tybot import specialist_wire_v3 as wire


class FakeToolBox:
    def __init__(self):
        self.touched = SimpleNamespace(
            evidence_hits=[], documents=[], live_permalinks=[]
        )
        self.budget = SimpleNamespace(
            max_calls=4,
            calls=0,
            max_chars=1000,
            chars=0,
            max_seconds=30,
            elapsed=0,
        )

    def run(self, name, args):
        self.budget.calls += 1
        if name == "search":
            self.touched.evidence_hits.append(object())
            return "authorized line"
        return "no result"


def _call(session, **over):
    payload = {
        "schema": wire.SCHEMA_TOOL_CALL,
        "request_id": session.request_id,
        "token": session.token,
        "call_id": "t1",
        "tool": "search",
        "args": {"query": "status"},
    }
    payload.update(over)
    return json.dumps(payload).encode()


def test_session_binds_a_toolbox_and_issues_opaque_evidence():
    registry = broker.ToolSessionRegistry(clock=lambda: 10.0)
    session = registry.open(
        "req-1", FakeToolBox(), credential_id="hermes:tyit", lifetime_s=20
    )

    result = json.loads(registry.handle(_call(session)))

    assert result["ok"] is True
    assert result["text"] == "authorized line"
    assert result["evidence"] == [{"id": "e1", "kind": "archive_line"}]
    assert registry.issued(session.token) == frozenset({"e1"})


def test_a_token_cannot_be_reused_for_another_request():
    registry = broker.ToolSessionRegistry(clock=lambda: 10.0)
    session = registry.open(
        "req-1", FakeToolBox(), credential_id="hermes:tyit", lifetime_s=20
    )

    result = json.loads(registry.handle(_call(session, request_id="req-2")))

    assert result["ok"] is False
    assert result["reason"] == "request-mismatch"


def test_expired_and_revoked_tokens_fail_closed():
    now = [10.0]
    registry = broker.ToolSessionRegistry(clock=lambda: now[0])
    session = registry.open(
        "req-1", FakeToolBox(), credential_id="hermes:tyit", lifetime_s=1
    )
    now[0] = 12.0

    assert json.loads(registry.handle(_call(session)))["reason"] == "expired-token"

    live = registry.open(
        "req-2", FakeToolBox(), credential_id="hermes:tyit", lifetime_s=10
    )
    registry.revoke(live.token)
    assert json.loads(registry.handle(_call(live)))["reason"] == "expired-token"


def test_tools_outside_the_session_allowlist_are_refused():
    registry = broker.ToolSessionRegistry(clock=lambda: 10.0)
    session = registry.open(
        "req-1",
        FakeToolBox(),
        credential_id="hermes:tyit",
        allow=("search",),
        lifetime_s=20,
    )

    result = json.loads(registry.handle(_call(session, tool="read_document")))

    assert result["ok"] is False
    assert result["reason"] == "tool-not-allowed"
