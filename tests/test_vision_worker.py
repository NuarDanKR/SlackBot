from decimal import Decimal

import pytest
from test_vision_extract import FakeRouter, _payload, _Resp
from test_vision_policy import PAGE, RESERVATION

from tybot.archive import vision_extract as vx
from tybot.archive.vision_store import VisionDenied, VisionDispatch
from tybot.archive.vision_worker import dispatch_reserved_page

CHECKS = dict(
    capability_verified=True,
    credential_ready=True,
    source_verified=True,
    rendering_verified=True,
    access_verified=True,
    pii_blocked=False,
)


class Store:
    def __init__(self, *, begin_error=None, finish_error=None):
        self.begin_error = begin_error
        self.finish_error = finish_error
        self.events = []

    def begin_dispatch(self, job_id, page, *, checks):
        self.events.append(("begin", job_id, page, checks))
        if self.begin_error:
            raise self.begin_error
        return VisionDispatch(
            reservation=RESERVATION,
            model="claude-sonnet-5-5",
            provider="anthropic",
            max_output_tokens=321,
            timeout_seconds=12.5,
        )

    def finish(self, job_id, *, actual_usd):
        self.events.append(("finish", job_id, actual_usd))
        if self.finish_error:
            raise self.finish_error


def test_committed_grant_controls_the_only_model_call_and_cost_settlement():
    store = Store()
    router = FakeRouter(_Resp(_payload(), cost_usd=0.0042))

    got = dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=router)

    assert got.status == vx.STATUS_EXTRACTED
    assert len(router.calls) == 1
    assert router.calls[0]["model"] == "claude-sonnet-5-5"
    assert router.calls[0]["max_tokens"] == 321
    assert router.calls[0]["timeout_seconds"] == 12.5
    assert store.events[-1] == ("finish", "job-1", Decimal("0.0042"))


def test_denied_dispatch_never_calls_or_settles_a_model():
    store = Store(begin_error=VisionDenied("approval_revoked"))
    router = FakeRouter(_Resp(_payload()))

    with pytest.raises(VisionDenied, match="approval_revoked"):
        dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=router)

    assert router.calls == []
    assert [event[0] for event in store.events] == ["begin"]


def test_unknown_gateway_charge_is_not_settled_as_zero():
    store = Store()
    router = FakeRouter(RuntimeError("provider failed after send"))

    got = dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=router)

    assert got.status == vx.STATUS_FAILED
    assert got.error_code == vx.ERROR_MODEL_ERROR
    assert store.events[-1] == ("finish", "job-1", None)


def test_pre_call_input_rejection_is_known_zero_and_still_raised():
    store = Store()
    # The committed provider is deliberately inconsistent with the registry.
    grant = VisionDispatch(RESERVATION, "claude-sonnet-5-5", "openai", 321, 12.5)
    store.begin_dispatch = lambda job_id, page, *, checks: grant
    router = FakeRouter(_Resp(_payload()))

    with pytest.raises(vx.VisionInputError):
        dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=router)

    assert router.calls == []
    assert store.events[-1] == ("finish", "job-1", Decimal("0"))


def test_unexpected_post_dispatch_exception_becomes_unknown_and_is_not_retried(monkeypatch):
    store = Store()
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise LookupError("unexpected parser failure")

    monkeypatch.setattr("tybot.archive.vision_worker.extract_page", boom)
    with pytest.raises(LookupError):
        dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=object())

    assert calls == [1]
    assert store.events[-1] == ("finish", "job-1", None)


def test_settlement_failure_is_visible_to_the_caller():
    store = Store(finish_error=RuntimeError("ledger unavailable"))
    router = FakeRouter(_Resp(_payload()))

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        dispatch_reserved_page(store, "job-1", PAGE, checks=CHECKS, router=router)

    assert len(router.calls) == 1
