"""Request-scoped tool broker for external v3 specialists.

The transport server is deliberately separate.  This module owns the security
boundary that can be tested without a Unix socket: a token is bound to one
request, one ``ToolBox`` (and therefore one ``RequestContext``), an allow-list,
and a deadline.  Revocation removes the complete authority object.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from . import specialist_wire_v3 as wire


@dataclass
class ToolSession:
    request_id: str
    token: str
    credential_id: str
    toolbox: object
    allow: frozenset[str]
    expires_at: float
    issued_evidence: set[str] = field(default_factory=set)
    evidence_objects: dict[str, tuple[str, object]] = field(default_factory=dict)
    _next_evidence: int = 1

    def issue_evidence(self, kind: str, value: object) -> dict[str, str]:
        evidence_id = f"e{self._next_evidence}"
        self._next_evidence += 1
        self.issued_evidence.add(evidence_id)
        self.evidence_objects[evidence_id] = (kind, value)
        return {"id": evidence_id, "kind": kind}


class ToolSessionRegistry:
    """In-memory authority registry.  Tokens are never persisted or logged."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._sessions: dict[str, ToolSession] = {}
        self._lock = threading.Lock()

    def open(
        self,
        request_id: str,
        toolbox,
        *,
        credential_id: str,
        allow: tuple[str, ...] = wire.DEFAULT_TOOLS,
        lifetime_s: float = wire.DEADLINE_MS / 1000,
    ) -> ToolSession:
        unknown = set(allow) - set(wire.TOOLS)
        if not request_id.strip() or not credential_id.strip() or unknown or lifetime_s <= 0:
            raise ValueError("invalid v3 tool session")
        token = wire.new_tool_token()
        session = ToolSession(
            request_id=request_id,
            token=token,
            credential_id=credential_id,
            toolbox=toolbox,
            allow=frozenset(allow),
            expires_at=self._clock() + lifetime_s,
        )
        with self._lock:
            self._sessions[token] = session
        return session

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def issued(self, token: str) -> frozenset[str]:
        with self._lock:
            session = self._sessions.get(token)
            return frozenset(session.issued_evidence) if session else frozenset()

    def credential_for(self, token: str) -> str:
        with self._lock:
            session = self._sessions.get(token)
            return session.credential_id if session else ""

    def resolve(
        self, token: str, evidence_ids: tuple[str, ...]
    ) -> tuple[tuple[str, object], ...]:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return ()
            return tuple(
                session.evidence_objects[item]
                for item in evidence_ids
                if item in session.evidence_objects
            )

    def handle(self, raw: bytes) -> bytes:
        try:
            call = wire.parse_tool_call(raw)
        except wire.WireViolation:
            return wire.tool_refusal("unknown", "invalid-request")

        with self._lock:
            session = self._sessions.get(call.token)
        if session is None:
            return wire.tool_refusal(call.call_id, "expired-token")
        if not secrets.compare_digest(session.request_id, call.request_id):
            return wire.tool_refusal(call.call_id, "request-mismatch")
        if self._clock() >= session.expires_at:
            self.revoke(call.token)
            return wire.tool_refusal(call.call_id, "expired-token")
        if call.tool not in session.allow:
            return wire.tool_refusal(call.call_id, "tool-not-allowed")

        touched = session.toolbox.touched
        before = _touch_counts(touched)
        text = session.toolbox.run(call.tool, call.args)
        evidence = [
            session.issue_evidence(kind, value)
            for kind, value in _new_evidence(touched, before)
        ]
        return wire.tool_result(
            call.call_id,
            text,
            evidence=evidence,
            budget_left=_budget_left(session.toolbox.budget),
        )


def _touch_counts(touched) -> tuple[int, int, int]:
    return (
        len(getattr(touched, "evidence_hits", ()) or ()),
        len(getattr(touched, "documents", ()) or ()),
        len(getattr(touched, "live_permalinks", ()) or ()),
    )


def _new_evidence(touched, before: tuple[int, int, int]) -> tuple[tuple[str, object], ...]:
    # A line already belongs to a document, so only issue document IDs when no
    # concrete line was returned by that tool call.
    lines = tuple((getattr(touched, "evidence_hits", ()) or ())[before[0] :])
    documents = (
        ()
        if lines
        else tuple((getattr(touched, "documents", ()) or ())[before[1] :])
    )
    links = tuple((getattr(touched, "live_permalinks", ()) or ())[before[2] :])
    messages = tuple((getattr(touched, "live_messages", ()) or ())[before[2] :])
    live = tuple(
        (link, messages[index] if index < len(messages) else None)
        for index, link in enumerate(links)
    )
    return tuple(
        [("archive_line", value) for value in lines]
        + [("document", value) for value in documents]
        + [("slack_message", value) for value in live]
    )


def _budget_left(budget) -> dict[str, int]:
    return {
        "calls_left": max(0, int(budget.max_calls - budget.calls)),
        "chars_left": max(0, int(budget.max_chars - budget.chars)),
        "seconds_left": max(0, int(budget.max_seconds - budget.elapsed)),
    }


__all__ = ["ToolSession", "ToolSessionRegistry"]
