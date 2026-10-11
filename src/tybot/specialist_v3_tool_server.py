"""Authenticated Unix HTTP server for Hermes -> TYBot v3 tool calls."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass, field

from . import specialist_wire as signing
from . import specialist_wire_v3 as wire
from .specialist_v3_broker import ToolSessionRegistry


@dataclass
class RequestAuthenticator:
    secret: bytes
    wall_clock: object = time.time
    _nonces: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def accepts(self, path: str, body: bytes, headers: dict[str, str]) -> bool:
        try:
            stamp = int(headers.get("x-tybot-timestamp", ""))
        except ValueError:
            return False
        nonce = headers.get("x-tybot-nonce", "")
        provided = headers.get("x-tybot-signature", "")
        now = int(self.wall_clock())
        if (
            abs(now - stamp) > signing.CLOCK_SKEW_SECONDS
            or not nonce
            or len(nonce) > 128
            or not provided
        ):
            return False
        expected = signing.sign(
            self.secret,
            signing.canonical(
                method="POST", path=path, timestamp=stamp, nonce=nonce, body=body
            ),
        )
        if not hmac.compare_digest(expected, provided):
            return False
        with self._lock:
            cutoff = now - signing.CLOCK_SKEW_SECONDS
            self._nonces = {key: seen for key, seen in self._nonces.items() if seen >= cutoff}
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = now
        return True


class ToolServer:
    """Lifecycle wrapper used by the Slack process."""

    def __init__(
        self,
        registry: ToolSessionRegistry,
        *,
        path: str = wire.TOOL_SOCKET_PATH,
        socket_group: str = "tybot-specialist-rpc",
    ) -> None:
        self.registry = registry
        self._authenticators: dict[str, RequestAuthenticator] = {}
        self._auth_lock = threading.Lock()
        self.path = path
        self.socket_group = socket_group
        self._server = None
        self._thread = None

    def register_credential(self, identity: str, secret: bytes) -> None:
        if not identity.strip() or len(secret) < 32:
            raise ValueError("invalid v3 credential")
        with self._auth_lock:
            existing = self._authenticators.get(identity)
            if existing is not None:
                if not hmac.compare_digest(existing.secret, secret):
                    raise RuntimeError("v3 credential changed while TYBot is running")
                return
            self._authenticators[identity] = RequestAuthenticator(secret)

    def start(self) -> None:
        import socketserver

        if self._server is not None:
            return
        if not hasattr(socketserver, "UnixStreamServer"):
            raise RuntimeError("Unix sockets are unavailable")
        if self.path != wire.TOOL_SOCKET_PATH:
            raise ValueError("tool socket path is fixed")
        os.makedirs(os.path.dirname(self.path), mode=0o750, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

        owner = self

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                status, response, request_id, secret = owner._handle_http(self.request)
                headers = {}
                if request_id and secret:
                    digest = hashlib.sha256(response).hexdigest()
                    headers["X-TYBot-Signature"] = signing.sign(
                        secret,
                        f"{request_id}\n{digest}".encode(),
                    )
                self.request.sendall(_http_response(status, response, headers))

        self._server = Server(self.path, Handler)
        import grp

        group_id = grp.getgrnam(self.socket_group).gr_gid
        os.chown(os.path.dirname(self.path), -1, group_id)
        os.chmod(os.path.dirname(self.path), 0o750)
        os.chown(self.path, -1, group_id)
        os.chmod(self.path, 0o660)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tybot-specialist-tools",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    def _handle_http(self, sock) -> tuple[int, bytes, str, bytes | None]:
        try:
            path, headers, body = _read_request(sock)
        except ValueError:
            return 400, b'{"error":"bad_request"}', "", None
        if path != wire.TOOL_ENDPOINT:
            return 404, b'{"error":"not_found"}', "", None
        identity, secret = self._authenticate(path, body, headers)
        if secret is None:
            return 401, b'{"error":"unauthorized"}', "", None
        try:
            call = wire.parse_tool_call(body)
        except wire.WireViolation:
            return 400, b'{"error":"bad_request"}', "", None
        if not hmac.compare_digest(self.registry.credential_for(call.token), identity):
            return 401, b'{"error":"unauthorized"}', "", None
        return 200, self.registry.handle(body), call.request_id, secret

    def _authenticate(
        self, path: str, body: bytes, headers: dict[str, str]
    ) -> tuple[str, bytes | None]:
        with self._auth_lock:
            authenticators = tuple(self._authenticators.items())
        for identity, authenticator in authenticators:
            if authenticator.accepts(path, body, headers):
                return identity, authenticator.secret
        return "", None


def _read_request(sock) -> tuple[str, dict[str, str], bytes]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(8192)
        if not chunk or len(raw) + len(chunk) > 16 * 1024:
            raise ValueError("invalid headers")
        raw += chunk
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    if len(parts) != 3 or parts[0] != "POST" or parts[2] != "HTTP/1.1":
        raise ValueError("invalid request line")
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "-1"))
    except ValueError as exc:
        raise ValueError("invalid length") from exc
    if length < 0 or length > wire.MAX_TOOL_RESULT_BYTES:
        raise ValueError("invalid length")
    while len(body) < length:
        chunk = sock.recv(min(8192, length - len(body)))
        if not chunk:
            raise ValueError("short body")
        body += chunk
    return parts[1], headers, body[:length]


def _http_response(status: int, body: bytes, headers: dict[str, str]) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}[status]
    lines = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


__all__ = ["RequestAuthenticator", "ToolServer"]
