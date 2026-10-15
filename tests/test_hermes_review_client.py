import copy
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from tybot import summary_review as sr
from tybot.gateway.hermes_review import PATH, ReviewGenerator, ReviewRPCError
from tybot.specialist_http import HttpReply, TransportError

SECRET = b"a" * 32
ENV = {"TYBOT_MASTER_V2_WORKSPACES": "tyit,mgmt", "TYBOT_HERMES_SHARED_WORKSPACES": "tyit",
       "TYBOT_HERMES_REVIEW_WORKSPACES": "tyit,mgmt"}


def source():
    return [sr.SourceLine("2026-10-15 09:00", "author", "The deadline is 2026-12-31.",
                          "raw.md:5", channel_id="C1", message_ts="123.456")]


def candidate():
    return {"source_id": "r1", "kind": "number_or_schedule", "form": "quote",
            "current_text": "", "proposed_text": source()[0].text,
            "evidence_quote": source()[0].text, "evidence_at": "wrong", "evidence_author": "wrong"}


class Router:
    def __init__(self):
        self.records = []
        self.block = False

    def check_external_review(self, workspace, *, input_chars):
        assert input_chars > 0
        if self.block:
            raise RuntimeError("budget")
        return 1.0

    def record_external_review(self, workspace, metrics, *, reservation):
        self.records.append((workspace, metrics, reservation))


class Transport:
    def __init__(self):
        self.calls = []
        self.health = {"status": "ok", "contract_version": "v2", "version": "test",
                       "capabilities": ["review_candidates_v1"], "workspaces": {"tyit": True, "mgmt": True}}
        self.status = 200
        self.bad_signature = False
        self.mutate = lambda data: None
        self.fail = False

    def post(self, path, body, headers, *, timeout_s):
        self.calls.append((path, json.loads(body)))
        if path == "/v1/health":
            return HttpReply(200, json.dumps(self.health).encode(), {})
        if path == "/v3/cancel":
            return HttpReply(200, b"{}", {})
        assert path == PATH
        assert timeout_s == 120
        assert headers["X-TYBot-Signature"]
        if self.fail:
            raise TransportError("not logged")
        req = json.loads(body)
        data = {"schema": "tybot.hermes.review.response.v1", **{
            key: req[key] for key in ("request_id", "workspace", "source_digest")},
            "status": "ok", "candidates": [candidate()], "error_code": "",
            "metrics": {"model": "model", "input_tokens": 2, "output_tokens": 3,
                        "stop_reason": "end_turn", "elapsed_ms": 1,
                        "proposed_count": 1, "accepted_count": 1, "rejected_counts": {}}}
        self.mutate(data)
        raw = json.dumps(data).encode()
        signed = req["request_id"] + "\n" + hashlib.sha256(raw).hexdigest()
        sig = hmac.new(SECRET, signed.encode(), hashlib.sha256).hexdigest()
        return HttpReply(self.status, raw, {"x-tybot-signature": "bad" if self.bad_signature else sig})


def setup():
    transport, router, credentials = Transport(), Router(), []
    def get(key, ws):
        credentials.append((key, ws))
        return "identity", SECRET
    paths, legacy = [], []
    generator = ReviewGenerator(lambda *args: legacy.append(args) or "legacy", router, env=ENV,
        credentials=SimpleNamespace(get=get),
        transport_factory=lambda path: paths.append(path) or transport)
    return generator, transport, router, credentials, paths, legacy


def invoke(generator, workspace="tyit", sources=None):
    lines = source() if sources is None else sources
    return generator.review_candidates(workspace=workspace, source=lines, approved=[],
                                       digest=sr.source_digest(lines))


def test_shared_endpoint_uses_actual_workspace_and_shared_credential():
    generator, transport, router, credentials, paths, legacy = setup()
    result = invoke(generator)
    assert result["status"] == "ok"
    assert paths == ["/run/tybot-subbots/hermes-shared/http.sock"]
    assert credentials == [("hermes", "shared")]
    assert transport.calls[-1][1]["workspace"] == "tyit"
    assert router.records[0][0] == "tyit"
    assert not legacy


def test_isolated_endpoint_and_legacy_opt_out_are_explicit():
    generator, _, _, credentials, paths, legacy = setup()
    invoke(generator, "mgmt")
    assert paths == ["/run/tybot-subbots/hermes-mgmt/http.sock"]
    assert credentials == [("hermes", "mgmt")]
    assert invoke(generator, "other") is None
    assert generator("system", "input", "other") == "legacy"
    assert len(legacy) == 1


def test_review_rollout_is_off_by_default_and_cannot_escape_master_allowlist():
    from tybot.master_v2_rollout import RolloutConfigError

    disabled = ReviewGenerator(lambda *_: "legacy", Router(), env={"TYBOT_MASTER_V2_WORKSPACES": "tyit"})
    assert invoke(disabled) is None
    with pytest.raises(RolloutConfigError):
        ReviewGenerator(None, Router(), env={"TYBOT_HERMES_REVIEW_WORKSPACES": "tyit"})


@pytest.mark.parametrize("field", ["workspace", "source_digest", "request_id", "schema"])
def test_response_is_bound_to_request(field):
    generator, transport, router, *_ = setup()
    transport.mutate = lambda data: data.update({field: "wrong"})
    with pytest.raises(ReviewRPCError, match="response-binding"):
        invoke(generator)
    assert router.records[0][1] == {}


def test_signature_is_checked_before_body_is_used():
    generator, transport, *_ = setup()
    transport.bad_signature = True
    with pytest.raises(ReviewRPCError, match="bad-signature"):
        invoke(generator)


@pytest.mark.parametrize("change", [
    {"capabilities": []}, {"capabilities": "review_candidates_v1"},
    {"workspaces": {"tyit": False}}, {"status": "failed"},
])
def test_unsupported_or_unready_worker_never_calls_model_or_legacy(change):
    generator, transport, router, _, _, legacy = setup()
    transport.health.update(change)
    with pytest.raises(ReviewRPCError):
        invoke(generator)
    assert len(transport.calls) == 1
    assert not router.records and not legacy


def test_empty_is_only_successfully_processed_zero_proposals():
    generator, transport, *_ = setup()
    def empty(data):
        data.update(status="empty", candidates=[])
        data["metrics"].update(proposed_count=0, accepted_count=0)
    transport.mutate = empty
    assert invoke(generator)["status"] == "empty"
    def false_empty(data):
        empty(data)
        data["metrics"]["proposed_count"] = 1
    transport.mutate = false_empty
    with pytest.raises(ReviewRPCError, match="false-empty"):
        invoke(generator)


@pytest.mark.parametrize("code", ["model_timeout", "model_error", "invalid_json", "truncated_output",
                                  "candidate_validation_failed", "cancelled"])
def test_failed_result_is_not_empty_or_legacy(code):
    generator, transport, _, _, _, legacy = setup()
    def fail(data):
        data.update(status="failed", candidates=[], error_code=code)
        data["metrics"]["accepted_count"] = 0
    transport.mutate = fail
    with pytest.raises(ReviewRPCError, match=code):
        invoke(generator)
    assert not legacy


def test_timeout_cancels_in_flight_work_and_accounts_uncertain_cost():
    generator, transport, router, *_ = setup()
    transport.fail = True
    with pytest.raises(ReviewRPCError, match="transport"):
        invoke(generator)
    assert transport.calls[-1][0] == "/v3/cancel"
    assert router.records == [("tyit", {}, 1.0)]


def test_budget_check_precedes_model_call():
    generator, transport, router, *_ = setup()
    router.block = True
    with pytest.raises(RuntimeError, match="budget"):
        invoke(generator)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("lines", [[], source() * 1001,
    [sr.SourceLine("now", "author", "x" * 31501, "file:1")],
    [sr.SourceLine("now", "author", "\U0001f600" * 16000, "file:1")]])
def test_source_limits_apply_before_dispatch(lines):
    generator, transport, *_ = setup()
    with pytest.raises(ReviewRPCError, match="source-limit"):
        invoke(generator, sources=lines)
    assert not transport.calls


def test_exact_source_id_controls_original_coordinates():
    lines = [*source(), sr.SourceLine("later", "other", source()[0].text, "other.md:9", channel_id="C1")]
    row = candidate()
    row["source_id"] = "r2"
    got = sr.parse_proposals(json.dumps({"candidates": [row]}), lines, require_source_id=True)
    assert got[0].evidence_locator == "other.md:9"
    assert got[0].evidence_at == "later"


@pytest.mark.parametrize("change", [
    {"source_id": "r9"}, {"source_id": []}, {"form": "unknown"},
    {"evidence_quote": "deadline is 2027-12-31"},
    {"form": "abstract", "proposed_text": "금액999원"},
])
def test_python_rejects_forged_candidate_independently(change):
    row = candidate()
    row.update(change)
    assert sr.parse_proposals(json.dumps({"candidates": [row]}), source(),
                              require_source_id=True) == []


def test_approved_text_and_numbers_cannot_become_new_evidence():
    row = candidate()
    row.update(form="abstract", current_text="Old deadline 2027", proposed_text="New deadline 2027")
    assert sr.parse_proposals(json.dumps({"candidates": [row]}), source(),
        approved=["Old deadline 2027"], require_source_id=True) == []
    row = candidate()
    assert sr.parse_proposals(json.dumps({"candidates": [row]}), source(),
        approved=[row["proposed_text"]], require_source_id=True) == []


def test_bad_metrics_cannot_hide_proposals_or_leak_content_keys():
    generator, transport, *_ = setup()
    for change in ({"accepted_count": 0}, {"input_tokens": True}, {"rejected_counts": {"secret text": 1}}):
        transport.mutate = lambda data, change=change: data["metrics"].update(copy.deepcopy(change))
        with pytest.raises(ReviewRPCError, match="invalid-metrics"):
            invoke(generator)


class Store:
    def __init__(self):
        self.conn = SimpleNamespace(rollback=lambda: None)
        self.failed = []
        self.saved = []

    def cursor(self, *args):
        return ""

    def start_at(self, *args):
        return "2026-10-15 00:00"

    def source_run_seen(self, *args):
        return False

    def may_attempt(self, *args):
        return True

    def approved(self, *args):
        return []

    def mark_failed(self, *args):
        self.failed.append(args)

    def save_run(self, **kwargs):
        self.saved.append(kwargs)
        return len(kwargs["proposals"])


def run_channel(monkeypatch, generator, store, lines=None):
    from datetime import datetime

    monkeypatch.setattr(sr, "channel_source", lambda *args, **kwargs: source() if lines is None else lines)
    monkeypatch.setattr(sr, "_counterpart_decisions", lambda _: [])
    return sr.generate_channel(store, None, workspace="tyit", channel_id="C1", channel_name="test",
                               complete=generator, now=datetime(2026, 10, 15, 14, tzinfo=sr.KST))


def test_generation_flow_calls_rpc_and_keeps_archive_owned_coordinates(monkeypatch):
    generator, _, _, _, _, legacy = setup()
    store = Store()
    assert run_channel(monkeypatch, generator, store) == 1
    proposal = store.saved[0]["proposals"][0]
    assert proposal.evidence_locator == "raw.md:5"
    assert proposal.evidence_channel_id == "C1"
    assert proposal.evidence_message_ts == "123.456"
    assert not legacy and not store.failed


def test_no_source_does_not_call_rpc(monkeypatch):
    generator, transport, *_ = setup()
    store = Store()
    assert run_channel(monkeypatch, generator, store, []) == 0
    assert not transport.calls and not store.saved


def test_python_rejection_does_not_advance_source_cursor(monkeypatch):
    generator, transport, *_ = setup()
    transport.mutate = lambda data: data["candidates"][0].update(source_id="r9")
    store = Store()
    assert run_channel(monkeypatch, generator, store) == 0
    assert store.failed and not store.saved


def test_rpc_failure_preserves_cursor_and_marks_retry(monkeypatch):
    generator, transport, *_ = setup()
    transport.health["capabilities"] = []
    store = Store()
    with pytest.raises(ReviewRPCError, match="capability-missing"):
        run_channel(monkeypatch, generator, store)
    assert store.failed and not store.saved
