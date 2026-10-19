"""Unix-socket client for external specialists using the v3 tool contract."""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

from . import specialist_wire as signing
from . import specialist_wire_v3 as wire
from .specialist_http import Transport, TransportError, UnixSocketTransport
from .specialist_v3_broker import ToolSessionRegistry

log = logging.getLogger("tybot.specialist_v3_client")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class CallOutcome:
    answer: wire.Answer | None
    error_code: str = ""
    elapsed_ms: int = 0
    evidence_objects: tuple[tuple[str, object], ...] = ()


def socket_path(specialist: str, workspace: str) -> str:
    """Derive the worker socket path from validated registry keys only."""
    if not _KEY.fullmatch(specialist) or not _KEY.fullmatch(workspace):
        raise ValueError("invalid specialist socket key")
    return f"/run/tybot-subbots/{specialist}-{workspace}/http.sock"


def transport_for(specialist: str, workspace: str) -> UnixSocketTransport:
    return UnixSocketTransport(socket_path(specialist, workspace))


def call(
    transport: Transport,
    *,
    workspace: str,
    authorization_id: str,
    question: str,
    toolbox,
    secret: bytes,
    credential_id: str,
    registry: ToolSessionRegistry,
    allow: tuple[str, ...] = wire.DEFAULT_TOOLS,
    deadline_ms: int = wire.DEADLINE_MS,
) -> CallOutcome:
    """Call once and revoke the request authority on every exit path."""
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    session = registry.open(
        request_id,
        toolbox,
        credential_id=credential_id,
        allow=allow,
        lifetime_s=deadline_ms / 1000,
    )
    request = wire.Request(
        workspace=workspace,
        authorization_id=authorization_id,
        question=question,
        tool_token=session.token,
        allow=allow,
        request_id=request_id,
        deadline_ms=deadline_ms,
    )
    try:
        body = request.body()
        headers = signing.signature_headers(
            secret, method="POST", path=wire.ANSWER_ENDPOINT, body=body
        )
        try:
            reply = transport.post(
                wire.ANSWER_ENDPOINT,
                body,
                headers,
                timeout_s=deadline_ms / 1000,
            )
        except TransportError as exc:
            log.warning("v3 specialist transport failed: %s", type(exc).__name__)
            _cancel(transport, request_id=request_id, secret=secret)
            return CallOutcome(None, "transport", _elapsed_ms(started))

        if reply.status != 200:
            return CallOutcome(None, f"http-{reply.status}", _elapsed_ms(started))

        signature = reply.headers.get("x-tybot-signature", "")
        if not signing.verify_response_signature(
            secret,
            request_id=request_id,
            body=reply.body,
            provided=signature,
        ):
            return CallOutcome(None, "bad-signature", _elapsed_ms(started))

        try:
            answer = wire.validate_response(
                reply.body,
                request_id=request_id,
                issued_evidence=registry.issued(session.token),
                max_output_chars=request.max_output_chars,
            )
        except wire.WireViolation:
            return CallOutcome(None, "invalid-output", _elapsed_ms(started))
        return CallOutcome(
            answer,
            elapsed_ms=_elapsed_ms(started),
            evidence_objects=registry.resolve(session.token, answer.used_evidence),
        )
    finally:
        registry.revoke(session.token)


def _cancel(transport: Transport, *, request_id: str, secret: bytes) -> None:
    body = json.dumps({"request_id": request_id}, sort_keys=True).encode()
    headers = signing.signature_headers(
        secret, method="POST", path=wire.CANCEL_ENDPOINT, body=body
    )
    with contextlib.suppress(Exception):
        transport.post(wire.CANCEL_ENDPOINT, body, headers, timeout_s=1.0)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["CallOutcome", "call", "socket_path", "transport_for"]


class ExternalV3Adapter:
    """Adapter facade used by the existing specialist execution guardrails."""

    def __init__(
        self,
        key: str,
        *,
        workspace: str,
        authorization_id: str,
        toolbox,
        secret: bytes,
        credential_id: str,
        registry: ToolSessionRegistry,
        transport: Transport | None = None,
    ) -> None:
        from .specialist_tools import Touched

        self.key = key
        self.workspace = workspace
        self.authorization_id = authorization_id
        self.toolbox = toolbox
        self.secret = secret
        self.credential_id = credential_id
        self.registry = registry
        self.transport = transport or transport_for(key, workspace)
        self.touched = Touched()
        self.budget = toolbox.budget
        self.last_model = f"external:{key}"
        self.last_cost_usd = 0.0
        self.last_error_code = ""
        self.last_stop_reason = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.rounds = 0
        self.search_calls = 0
        self.search_rounds = 0
        self.tool_calls_by_name: dict[str, int] = {}
        self.phase = "external-v3"

    def complete(self, request) -> str:
        from .specialist_adapters import _with_display_hint

        # 실시간 Slack 조회는 요청별 ToolBox가 그 경로를 실제로 제공할 때만 연다.
        # 기본 계약에는 이 도구가 없으므로 여기서 명시하지 않으면, 수집 직후의
        # 질문은 아카이브가 따라잡을 때까지 외부 전문가가 근거를 얻을 수 없다.
        allow = (
            wire.TOOLS
            if getattr(self.toolbox, "live_fetch", None) is not None
            else wire.DEFAULT_TOOLS
        )
        outcome = call(
            self.transport,
            workspace=self.workspace,
            authorization_id=self.authorization_id,
            question=_with_display_hint(request.question, request),
            toolbox=self.toolbox,
            secret=self.secret,
            credential_id=self.credential_id,
            registry=self.registry,
            allow=allow,
            deadline_ms=int(request.deadline.remaining() * 1000),
        )
        if outcome.answer is None:
            self.last_error_code = outcome.error_code or "external-failed"
            return ""
        answer = outcome.answer
        self.last_cost_usd = float(answer.usage.get("cost_usd", 0.0) or 0.0)
        self.last_model = str(answer.usage.get("model") or self.last_model)
        self.last_stop_reason = str(answer.usage.get("stop_reason") or "")
        self.input_tokens = int(answer.usage.get("input_tokens", 0) or 0)
        self.output_tokens = int(answer.usage.get("output_tokens", 0) or 0)
        self.rounds = int(answer.usage.get("tool_calls", 0) or 0)
        by_name = answer.usage.get("tool_calls_by_name", {})
        if isinstance(by_name, dict):
            self.tool_calls_by_name = {
                str(name): int(count or 0) for name, count in by_name.items()
            }
        self.search_calls = self.tool_calls_by_name.get("search", 0)
        self.search_rounds = self.search_calls
        self._record_used(outcome.evidence_objects)
        if answer.status == wire.STATUS_ANSWERED:
            return answer.text
        self.last_error_code = {
            wire.STATUS_NO_EVIDENCE: "evidence_insufficient",
            wire.STATUS_REFUSED: "specialist-refused",
            wire.STATUS_FAILED: "specialist-failed",
        }[answer.status]
        return ""

    def _record_used(self, values: tuple[tuple[str, object], ...]) -> None:
        for kind, value in values:
            if kind == "archive_line":
                if value not in self.touched.evidence_hits:
                    self.touched.evidence_hits.append(value)
                doc = getattr(value, "doc", None)
                if doc is not None:
                    self.touched.record_doc(doc)
            elif kind == "document":
                self.touched.record_doc(value)
            elif kind == "slack_message":
                link, coordinate = value
                if link and link not in self.touched.live_permalinks:
                    self.touched.live_permalinks.append(link)
                if coordinate and coordinate not in self.touched.live_messages:
                    self.touched.live_messages.append(coordinate)


__all__.append("ExternalV3Adapter")
