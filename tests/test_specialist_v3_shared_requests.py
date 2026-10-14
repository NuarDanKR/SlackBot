"""Shared worker credentials must not merge request authority or evidence."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from tybot import specialist_v3_client as client
from tybot import specialist_wire as signing
from tybot import specialist_wire_v3 as wire
from tybot.specialist_http import HttpReply
from tybot.specialist_v3_broker import ToolSessionRegistry

SECRET = b"shared-worker-fixture-secret-12345"
IDENTITY = "hermes:shared"


class ScopedBox:
    def __init__(self, workspace):
        self.workspace = workspace
        self.line = object()
        self.touched = SimpleNamespace(
            evidence_hits=[], documents=[], live_permalinks=[],
        )
        self.budget = SimpleNamespace(
            max_calls=4, calls=0, max_chars=1000, chars=0, max_seconds=30, elapsed=0,
        )

    def run(self, tool, args):
        assert tool == "search"
        self.budget.calls += 1
        self.touched.evidence_hits.append(self.line)
        return f"source for {self.workspace}"


def tool_body(request, *, token=None):
    return json.dumps({
        "schema": wire.SCHEMA_TOOL_CALL,
        "request_id": request["request_id"],
        "token": request["tools"]["token"] if token is None else token,
        "call_id": "t1", "tool": "search", "args": {"query": "status"},
    }).encode()


def signed_answer(request, text, evidence):
    body = json.dumps({
        "schema": wire.SCHEMA_RESPONSE, "request_id": request["request_id"],
        "status": "answered", "answer": text,
        "used_evidence": [evidence],
    }).encode()
    signature = signing.sign(
        SECRET,
        f"{request['request_id']}\n{hashlib.sha256(body).hexdigest()}".encode(),
    )
    return HttpReply(200, body, {"x-tybot-signature": signature})


def test_shared_concurrent_requests_resolve_same_ids_to_their_own_sources():
    registry = ToolSessionRegistry()
    received = {}
    barrier = Barrier(2, timeout=5)

    class SharedWorker:
        def post(self, path, body, headers, *, timeout_s):
            assert path == wire.ANSWER_ENDPOINT
            request = json.loads(body)
            workspace = request["workspace"]
            assert request["authorization_id"] == f"{workspace}:member"
            received[workspace] = request
            barrier.wait()
            other = received["mgmt" if workspace == "tyit" else "tyit"]
            refused = json.loads(registry.handle(tool_body(
                request, token=other["tools"]["token"],
            )))
            assert refused["reason"] == "request-mismatch"
            result = json.loads(registry.handle(tool_body(request)))
            assert result["text"] == f"source for {workspace}"
            assert result["evidence"][0]["id"] == "e1"
            barrier.wait()
            return signed_answer(request, result["text"], "e1")

    worker = SharedWorker()
    boxes = {workspace: ScopedBox(workspace) for workspace in ("tyit", "mgmt")}

    def invoke(workspace):
        return client.call(
            worker, workspace=workspace, authorization_id=f"{workspace}:member",
            question="summarize", toolbox=boxes[workspace], secret=SECRET,
            credential_id=IDENTITY, registry=registry,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {workspace: executor.submit(invoke, workspace) for workspace in boxes}
        outcomes = {workspace: future.result(timeout=10) for workspace, future in futures.items()}
    for workspace, outcome in outcomes.items():
        assert outcome.answer.text == f"source for {workspace}"
        assert outcome.evidence_objects == (("archive_line", boxes[workspace].line),)
        assert boxes[workspace].budget.calls == 1
    assert received["tyit"]["tools"]["token"] != received["mgmt"]["tools"]["token"]
    assert registry.active_count == 0


def test_worker_busy_is_not_retried_and_revokes_only_that_request():
    registry = ToolSessionRegistry()
    other = registry.open("other", ScopedBox("mgmt"), credential_id=IDENTITY)

    class BusyWorker:
        def __init__(self):
            self.calls = []

        def post(self, path, body, headers, *, timeout_s):
            self.calls.append(path)
            return HttpReply(429, b'{"error":"busy"}')

    worker = BusyWorker()
    outcome = client.call(
        worker, workspace="tyit", authorization_id="tyit:member", question="summarize",
        toolbox=ScopedBox("tyit"), secret=SECRET, credential_id=IDENTITY, registry=registry,
    )
    assert outcome.answer is None
    assert outcome.error_code == "http-429"
    assert worker.calls == [wire.ANSWER_ENDPOINT]
    assert registry.active_count == 1
    assert registry.credential_for(other.token) == IDENTITY
    request = {"request_id": other.request_id, "tools": {"token": other.token}}
    assert json.loads(registry.handle(tool_body(request)))["text"] == "source for mgmt"
    registry.revoke(other.token)
