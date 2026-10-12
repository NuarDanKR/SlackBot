from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from tybot import specialist_v3_client as client
from tybot import specialist_wire as signing
from tybot import specialist_wire_v3 as wire
from tybot.specialist_http import HttpReply, TransportError
from tybot.specialist_v3_broker import ToolSessionRegistry

SECRET = b"test-secret" * 4
CREDENTIAL_ID = "hermes:tyit"


class Box:
    def __init__(self, *, live_fetch=None):
        self.live_fetch = live_fetch
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
        self.touched.evidence_hits.append(object())
        return "authorized source"


class Hermes:
    def __init__(self, registry, *, bad_signature=False):
        self.registry = registry
        self.bad_signature = bad_signature
        self.calls = []
        self.requests = []

    def post(self, path, body, headers, *, timeout_s):
        self.calls.append(path)
        if path == wire.CANCEL_ENDPOINT:
            return HttpReply(200, b"{}")
        request = json.loads(body)
        self.requests.append(request)
        tool = json.dumps(
            {
                "schema": wire.SCHEMA_TOOL_CALL,
                "request_id": request["request_id"],
                "token": request["tools"]["token"],
                "call_id": "t1",
                "tool": "search",
                "args": {"query": "status"},
            }
        ).encode()
        tool_result = json.loads(self.registry.handle(tool))
        evidence_id = tool_result["evidence"][0]["id"]
        response = json.dumps(
            {
                "schema": wire.SCHEMA_RESPONSE,
                "request_id": request["request_id"],
                "status": wire.STATUS_ANSWERED,
                "answer": "grounded answer",
                "used_evidence": [evidence_id],
                "usage": {
                    "model": "claude-opus-5-5",
                    "stop_reason": "end_turn",
                    "input_tokens": 123,
                    "output_tokens": 45,
                    "tool_calls": 1,
                    "tool_calls_by_name": {"search": 1},
                },
            },
            sort_keys=True,
        ).encode()
        signature = signing.sign(
            SECRET,
            f"{request['request_id']}\n{hashlib.sha256(response).hexdigest()}".encode(),
        )
        if self.bad_signature:
            signature = "bad"
        return HttpReply(200, response, {"x-tybot-signature": signature})


def test_v3_call_allows_callbacks_and_revokes_the_token():
    registry = ToolSessionRegistry()
    hermes = Hermes(registry)

    outcome = client.call(
        hermes,
        workspace="tyit",
        authorization_id="tyit:member",
        question="summarize",
        toolbox=Box(),
        secret=SECRET,
        credential_id=CREDENTIAL_ID,
        registry=registry,
    )

    assert outcome.answer is not None
    assert outcome.answer.text == "grounded answer"
    assert registry.active_count == 0


def test_response_signature_is_checked_before_accepting_the_answer():
    registry = ToolSessionRegistry()
    outcome = client.call(
        Hermes(registry, bad_signature=True),
        workspace="tyit",
        authorization_id="tyit:member",
        question="summarize",
        toolbox=Box(),
        secret=SECRET,
        credential_id=CREDENTIAL_ID,
        registry=registry,
    )

    assert outcome.answer is None
    assert outcome.error_code == "bad-signature"


def test_transport_failure_is_not_retried_and_sends_best_effort_cancel():
    class Broken:
        def __init__(self):
            self.paths = []

        def post(self, path, body, headers, *, timeout_s):
            self.paths.append(path)
            if path == wire.ANSWER_ENDPOINT:
                raise TransportError("down")
            return HttpReply(200, b"{}")

    transport = Broken()
    outcome = client.call(
        transport,
        workspace="tyit",
        authorization_id="tyit:member",
        question="summarize",
        toolbox=Box(),
        secret=SECRET,
        credential_id=CREDENTIAL_ID,
        registry=ToolSessionRegistry(),
    )

    assert outcome.error_code == "transport"
    assert transport.paths == [wire.ANSWER_ENDPOINT, wire.CANCEL_ENDPOINT]


def test_socket_path_is_derived_and_rejects_path_input():
    assert client.socket_path("hermes", "tyit") == (
        "/run/tybot-subbots/hermes-tyit/http.sock"
    )
    for value in ("../hermes", "/tmp/x", "Hermes"):
        try:
            client.socket_path(value, "tyit")
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_external_adapter_allows_live_tool_only_when_request_can_fetch_live():
    class Deadline:
        def remaining(self):
            return 30

    request = SimpleNamespace(question="summarize", deadline=Deadline())

    def allowed(live_fetch):
        registry = ToolSessionRegistry()
        hermes = Hermes(registry)
        adapter = client.ExternalV3Adapter(
            "hermes",
            workspace="tyit",
            authorization_id="tyit:member",
            toolbox=Box(live_fetch=live_fetch),
            secret=SECRET,
            credential_id=CREDENTIAL_ID,
            registry=registry,
            transport=hermes,
        )
        assert adapter.complete(request) == "grounded answer"
        return hermes.requests[0]["tools"]["allow"]

    assert "fetch_recent_slack" not in allowed(None)
    assert "fetch_recent_slack" in allowed(lambda _channel, _limit: [])


def test_external_adapter_exposes_sanitized_rpc_runtime_metrics():
    class Deadline:
        def remaining(self):
            return 30

    registry = ToolSessionRegistry()
    adapter = client.ExternalV3Adapter(
        "hermes",
        workspace="tyit",
        authorization_id="tyit:member",
        toolbox=Box(),
        secret=SECRET,
        credential_id=CREDENTIAL_ID,
        registry=registry,
        transport=Hermes(registry),
    )

    assert adapter.complete(SimpleNamespace(question="summarize", deadline=Deadline()))
    assert adapter.last_model == "claude-opus-5-5"
    assert adapter.last_stop_reason == "end_turn"
    assert adapter.input_tokens == 123
    assert adapter.output_tokens == 45
    assert adapter.rounds == 1
    assert adapter.search_calls == 1
    assert adapter.search_rounds == 1
    assert adapter.tool_calls_by_name == {"search": 1}
