"""Authenticated Hermes review generation; archive coordinates stay in TYBot."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid

from .. import specialist_wire as signing
from ..master_v2_rollout import ENV_NAME, RolloutConfigError, enabled_workspaces
from ..specialist_http import TransportError, UnixSocketTransport
from ..specialist_v3_endpoints import EndpointPolicy
from ..specialist_v3_secrets import CredentialStore

PATH = "/v1/review-candidates"
CAPABILITY = "review_candidates_v1"
REVIEW_ENV = "TYBOT_HERMES_REVIEW_WORKSPACES"
DEADLINE_MS = 120_000
ERRORS = frozenset({"model_timeout", "model_error", "invalid_json", "truncated_output",
                    "candidate_validation_failed", "cancelled", "internal_error"})
REASONS = frozenset({"unknown_source_id", "quote_not_in_source", "quote_from_other_source",
    "missing_quote", "invented_number", "bad_kind", "bad_form", "quote_mismatch",
    "empty_proposed_text", "approved_text_only", "duplicate_candidate", "over_limit"})
log = logging.getLogger("tybot.gateway.hermes_review")


class ReviewRPCError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Hermes review RPC: {code}")


def _json(raw: bytes) -> dict:
    if len(raw) > signing.MAX_RESPONSE_BYTES:
        raise ReviewRPCError("response-too-large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ReviewRPCError("invalid-response") from exc
    if not isinstance(data, dict):
        raise ReviewRPCError("invalid-response")
    return data


def _count(value) -> bool:
    return type(value) is int and 0 <= value <= 100_000_000


def validate(data: dict, request: dict) -> dict:
    expected = {"schema": "tybot.hermes.review.response.v1",
                **{key: request[key] for key in ("request_id", "workspace", "source_digest")}}
    if any(data.get(key) != value for key, value in expected.items()):
        raise ReviewRPCError("response-binding")
    status, rows, metrics = data.get("status"), data.get("candidates"), data.get("metrics")
    if status not in {"ok", "empty", "failed"} or not isinstance(rows, list):
        raise ReviewRPCError("invalid-response")
    if not isinstance(metrics, dict):
        raise ReviewRPCError("invalid-metrics")
    for key in ("input_tokens", "output_tokens", "elapsed_ms", "proposed_count", "accepted_count"):
        if not _count(metrics.get(key)):
            raise ReviewRPCError("invalid-metrics")
    for key in ("model", "stop_reason"):
        if not isinstance(metrics.get(key), str) or len(metrics[key]) > 128:
            raise ReviewRPCError("invalid-metrics")
    rejected = metrics.get("rejected_counts")
    if (not isinstance(rejected, dict) or not set(rejected) <= REASONS
            or not all(_count(value) for value in rejected.values())):
        raise ReviewRPCError("invalid-metrics")
    proposed, accepted = metrics["proposed_count"], metrics["accepted_count"]
    if proposed < accepted or accepted != len(rows):
        raise ReviewRPCError("invalid-metrics")
    if status == "failed":
        if rows or data.get("error_code") not in ERRORS:
            raise ReviewRPCError("invalid-response")
    elif data.get("error_code") != "" or metrics["stop_reason"] == "max_tokens":
        raise ReviewRPCError("invalid-response")
    elif status == "empty":
        if rows or proposed or rejected:
            raise ReviewRPCError("false-empty")
    elif not 1 <= len(rows) <= 10 or not all(isinstance(row, dict) for row in rows):
        raise ReviewRPCError("invalid-response")
    return data


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class ReviewGenerator:
    """Use RPC only for opted-in workspaces, never fallback after RPC failure."""

    def __init__(self, legacy, router, *, env=None, credentials=None, transport_factory=None):
        env = os.environ if env is None else env
        self._legacy = legacy
        self._router = router
        self._enabled = enabled_workspaces({ENV_NAME: env.get(REVIEW_ENV, "")})
        if not self._enabled <= enabled_workspaces(env):
            raise RolloutConfigError(f"{REVIEW_ENV} must be a subset of {ENV_NAME}")
        self._policy = EndpointPolicy.from_env(env)
        self._credentials = credentials or CredentialStore(
            env.get("TYBOT_SPECIALIST_V3_HMAC_DIR", "/etc/tybot/credentials/specialist-v3"))
        self._transport = transport_factory or UnixSocketTransport

    def __call__(self, system, user, workspace):
        return self._legacy(system, user, workspace)

    def review_candidates(self, *, workspace, source, approved, digest):
        if workspace not in self._enabled:
            return None
        if (not source or len(source) > 1000
                or sum(_units(line.text) for line in source) > 31_500):
            raise ReviewRPCError("source-limit")
        old, used = [], 0
        for text in approved:
            size = _units(text)
            if used + size > 8000:
                break
            old.append(text)
            used += size
        endpoint = self._policy.for_request("hermes", workspace)
        try:
            _, secret = self._credentials.get("hermes", endpoint.credential_workspace)
        except (RuntimeError, ValueError) as exc:
            raise ReviewRPCError("credential-unavailable") from exc
        transport = self._transport(endpoint.socket)
        try:
            health = transport.post(signing.HEALTH_PATH, b"{}", signing.signature_headers(
                secret, method="POST", path=signing.HEALTH_PATH, body=b"{}"), timeout_s=2)
        except TransportError as exc:
            raise ReviewRPCError("health-unavailable") from exc
        if health.status != 200:
            raise ReviewRPCError("health-unavailable")
        ready = _json(health.body)
        try:
            signing.validate_health(health.body)
        except signing.WireViolation as exc:
            raise ReviewRPCError("invalid-health") from exc
        capabilities = ready.get("capabilities")
        if not isinstance(capabilities, list) or CAPABILITY not in capabilities:
            raise ReviewRPCError("capability-missing")
        if "workspaces" in ready and (not isinstance(ready["workspaces"], dict)
                                      or ready["workspaces"].get(workspace) is not True):
            raise ReviewRPCError("workspace-not-ready")
        request = {"schema": "tybot.hermes.review.request.v1", "request_id": str(uuid.uuid4()),
            "workspace": workspace, "source_digest": digest,
            "sources": [{"id": f"r{i}", "at": line.at, "author": line.author, "text": line.text}
                        for i, line in enumerate(source, 1)],
            "approved": old, "limits": {"deadline_ms": DEADLINE_MS, "max_candidates": 10}}
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(body) > signing.MAX_REQUEST_BYTES:
            raise ReviewRPCError("request-too-large")
        # The worker owns its model profile. Reserve a conservative registry envelope
        # before dispatch; unpriced or uncertain usage must never become free usage.
        reservation = self._router.check_external_review(workspace, input_chars=len(body))
        charged = False
        try:
            try:
                reply = transport.post(PATH, body, signing.signature_headers(
                    secret, method="POST", path=PATH, body=body), timeout_s=DEADLINE_MS / 1000)
            except TransportError as exc:
                cancel = json.dumps({"request_id": request["request_id"]}).encode()
                with contextlib.suppress(Exception):
                    transport.post("/v3/cancel", cancel, signing.signature_headers(
                        secret, method="POST", path="/v3/cancel", body=cancel), timeout_s=1)
                raise ReviewRPCError("transport") from exc
            if reply.status != 200:
                # Explicit pre-dispatch refusals did not consume model tokens.
                if reply.status in {400, 401, 403, 404, 409, 413, 429, 503}:
                    charged = True
                raise ReviewRPCError(f"http-{reply.status}")
            if not signing.verify_response_signature(secret, request_id=request["request_id"],
                    body=reply.body, provided=reply.headers.get("x-tybot-signature", "")):
                raise ReviewRPCError("bad-signature")
            result = validate(_json(reply.body), request)
            self._router.record_external_review(workspace, result["metrics"], reservation=reservation)
            charged = True
            log.info("review_rpc ws=%s request=%s status=%s proposed=%d accepted=%d",
                workspace, request["request_id"], result["status"],
                result["metrics"]["proposed_count"], result["metrics"]["accepted_count"])
            if result["status"] == "failed":
                raise ReviewRPCError(result["error_code"])
            return result
        finally:
            if not charged:
                self._router.record_external_review(workspace, {}, reservation=reservation)
