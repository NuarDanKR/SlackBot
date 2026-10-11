import json
import time
from types import SimpleNamespace

from tybot import specialist_wire as signing
from tybot import specialist_wire_v3 as wire
from tybot.specialist_v3_broker import ToolSessionRegistry
from tybot.specialist_v3_tool_server import RequestAuthenticator, ToolServer


def _headers(secret, body, *, now=100, nonce="n1"):
    return {
        key.lower(): value
        for key, value in signing.signature_headers(
            secret,
            method="POST",
            path="/v3/tools/call",
            body=body,
            now=now,
            nonce=nonce,
        ).items()
    }


def test_tool_request_hmac_is_accepted_once():
    body = b"{}"
    auth = RequestAuthenticator(b"secret", wall_clock=lambda: 100)
    headers = _headers(b"secret", body)

    assert auth.accepts("/v3/tools/call", body, headers)
    assert not auth.accepts("/v3/tools/call", body, headers)


def test_old_or_modified_tool_requests_are_rejected():
    body = b"{}"
    auth = RequestAuthenticator(b"secret", wall_clock=lambda: 200)

    assert not auth.accepts(
        "/v3/tools/call", body, _headers(b"secret", body, now=100)
    )
    assert not auth.accepts(
        "/v3/tools/call", b'{"changed":true}', _headers(b"secret", body, now=200)
    )


def test_wrong_secret_is_rejected():
    body = b"{}"
    auth = RequestAuthenticator(b"right", wall_clock=lambda: 100)

    assert not auth.accepts(
        "/v3/tools/call", body, _headers(b"wrong", body)
    )


class _Socket:
    def __init__(self, request: bytes):
        self.request = request

    def recv(self, _size):
        result, self.request = self.request, b""
        return result


class _Box:
    def __init__(self):
        self.touched = SimpleNamespace(
            evidence_hits=[], documents=[], live_permalinks=[]
        )
        self.budget = SimpleNamespace(
            max_calls=1,
            calls=0,
            max_chars=100,
            chars=0,
            max_seconds=10,
            elapsed=0,
        )

    def run(self, _name, _args):
        self.budget.calls += 1
        return "ok"


def _request(body: bytes, headers: dict[str, str]) -> bytes:
    lines = [
        "POST /v3/tools/call HTTP/1.1",
        f"Content-Length: {len(body)}",
        *(f"{key}: {value}" for key, value in headers.items()),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_tool_token_is_bound_to_the_workspace_credential():
    secret_a = b"a" * 32
    secret_b = b"b" * 32
    registry = ToolSessionRegistry()
    session = registry.open(
        "req-1", _Box(), credential_id="hermes:tyit", lifetime_s=10
    )
    body = json.dumps(
        {
            "schema": wire.SCHEMA_TOOL_CALL,
            "request_id": "req-1",
            "token": session.token,
            "call_id": "t1",
            "tool": "search",
            "args": {},
        }
    ).encode()
    server = ToolServer(registry)
    server.register_credential("hermes:tyit", secret_a)
    server.register_credential("hermes:pilot", secret_b)

    now = int(time.time())
    wrong = server._handle_http(
        _Socket(_request(body, _headers(secret_b, body, now=now)))
    )
    accepted = server._handle_http(
        _Socket(_request(body, _headers(secret_a, body, now=now, nonce="n2")))
    )

    assert wrong[0] == 401
    assert accepted[0] == 200
    assert accepted[3] == secret_a
