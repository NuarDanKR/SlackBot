"""B-81 민감도 통합 시험 (f86542f 독립 검증) — 정책 → 예약 영수증 → 실제 Router → provider.

대역 Router 가 아니라 **운영 worker 가 만드는 Router**(`image_description_job._router`)를 쓴다.
provider 만 네트워크 없는 가짜로 바꾼다(실제 Google 호출 없음).

거는 것:
1. public 정책은 실제 Router 의 `complete` 까지 public 으로 간다
2. public 카탈로그에서 confidential 요청은 계속 막힌다 — 예약 전 판정에서도, Router 에서도
3. 정책보다 넓은(또는 좁은) 민감도로 **조용히 바뀌지 않는다** — 들어간 값 그대로 나간다

f86542f 전에는 `describe_image` 가 민감도를 늘 CONFIDENTIAL 로 고정했다. 운영 Router 는
정책 민감도를 모델 상한으로 쓰므로, public 정책에서는 모든 호출이 `ModelNotAllowed` 로
닫혔다(시험 3이 그 회귀를 잡는다).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tybot.archive import image_description as idesc
from tybot.archive import image_description_job as job_mod
from tybot.archive import image_description_worker as worker
from tybot.archive.image_description_policy import (
    ImageDescriptionPolicy,
    preflight_image_description,
)
from tybot.archive.image_model_catalog import FreeQuotaState, ImageModelCatalogEntry
from tybot.gateway.base import LLMResponse, Sensitivity
from tybot.gateway.router import Router

MODEL, PROVIDER, REGION = "free-vision-v1", "google", "asia-northeast3"
IMAGE = idesc.ImageInput("tyit", "C12345678", "F12345678", "a" * 64, b"\x89PNG-x", "image/png")
NOW = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)


class FakeProvider:
    """네트워크 없는 provider. Router 가 실제로 부르는지만 본다."""

    name = PROVIDER

    def __init__(self):
        self.calls = 0

    def complete(self, spec, messages, *, max_tokens, temperature, **_extra):
        self.calls += 1
        body = json.dumps(
            {"describable": True, "description": "건설현장 외부에 굴착기가 보인다.",
             "tags": ["건설현장", "굴착기"], "uncertainty": []},
            ensure_ascii=False,
        )
        return LLMResponse(
            text=body, model=spec.model, provider=spec.provider,
            input_tokens=10, output_tokens=5, cost_usd=0.0,
        )


def _policy(sensitivity: Sensitivity) -> ImageDescriptionPolicy:
    return ImageDescriptionPolicy(
        workspace="tyit", revision="g:l", enabled=True, workspace_enabled=True,
        model=MODEL, provider=PROVIDER, region=REGION, sensitivity=sensitivity,
        catalog_verification_ttl_seconds=86_400, max_image_bytes=5_000_000,
        max_output_tokens=256, timeout_seconds=30,
    )


def _catalog(max_sensitivity: Sensitivity) -> ImageModelCatalogEntry:
    return ImageModelCatalogEntry(
        model=MODEL, provider=PROVIDER, supports_vision=True, zero_cost_verified=True,
        verified_at=NOW - timedelta(minutes=5), verification_revision="v1",
        max_sensitivity=max_sensitivity, allowed_regions=frozenset({REGION}),
        credential_ready=True, quota_state=FreeQuotaState.AVAILABLE, quota_remaining=10,
    )


@pytest.fixture
def runtime_router(monkeypatch, tmp_path):
    """운영 `_router` 그대로, provider 만 가짜. Router.complete 에 들어간 민감도를 적는다."""
    provider = FakeProvider()
    monkeypatch.setattr(job_mod, "_provider", lambda name, key: provider)
    seen: list[Sensitivity] = []
    original = Router.complete

    def spy(self, messages, **kw):
        seen.append(kw.get("sensitivity"))
        return original(self, messages, **kw)

    monkeypatch.setattr(Router, "complete", spy)

    def build(policy):
        return job_mod._router(policy, cost_path=tmp_path / "cost.json", api_key="unused")

    return SimpleNamespace(build=build, provider=provider, seen=seen)


def _describe(router, sensitivity):
    return idesc.describe_image(
        IMAGE, router=router, model=MODEL, provider=PROVIDER, sensitivity=sensitivity,
        max_output_tokens=256, timeout_seconds=30,
    )


# 1 ---------------------------------------------------------------------------


def test_a_public_policy_reaches_the_real_router_as_the_contract_value(runtime_router):
    """public 정책도 계약 단계(일반 업무 = internal)로 Router 까지 간다 — 기밀로 올리지 않는다."""
    from tybot.archive.vision_sensitivity import request_sensitivity

    router = runtime_router.build(_policy(Sensitivity.PUBLIC))
    result = _describe(router, request_sensitivity(Sensitivity.PUBLIC))
    assert result.status == idesc.STATUS_DESCRIBED
    assert runtime_router.provider.calls == 1
    assert runtime_router.seen == [Sensitivity.INTERNAL]


def test_the_worker_passes_the_reserved_sensitivity_through(runtime_router):
    """예약 영수증(grant)의 민감도가 worker → describe_image → Router 로 그대로 간다."""
    router = runtime_router.build(_policy(Sensitivity.PUBLIC))

    class Store:
        def begin_dispatch(self, job_id, image, *, checks):
            return SimpleNamespace(
                model=MODEL, provider=PROVIDER, sensitivity=Sensitivity.INTERNAL,
                max_output_tokens=256, timeout_seconds=30,
            )

        def finish_and_publish(self, job_id, result):
            return "revision-1"

    run = worker.dispatch_reserved_image(Store(), "job-1", IMAGE, router=router, checks={})
    assert run.state == "published"
    assert runtime_router.seen == [Sensitivity.INTERNAL]


# 2 ---------------------------------------------------------------------------


def _preflight(policy_level, catalog_level, *, pii_blocked=False):
    return preflight_image_description(
        IMAGE, policy=_policy(policy_level), catalog=_catalog(catalog_level), approval=None,
        dispatch=None, prompt_revision=idesc.PROMPT_REVISION, now=NOW,
        source_verified=True, access_verified=True, pii_blocked=pii_blocked, bot_generated=False,
    )


def test_a_public_catalog_blocks_confidential_policies_before_reservation():
    assert _preflight(Sensitivity.CONFIDENTIAL, Sensitivity.PUBLIC).code == "sensitivity_not_allowed"


@pytest.mark.parametrize("policy_level", [Sensitivity.PUBLIC, Sensitivity.INTERNAL])
def test_general_business_policies_pass_a_public_catalog(policy_level):
    """오너 결정(2026-11-19): 표기 불일치(public↔internal)로 막지 않는다. 승인 단계까지 간다."""
    assert _preflight(policy_level, Sensitivity.PUBLIC).code == "approval_missing"


def test_pii_blocking_is_not_relaxed_by_the_owner_decision():
    assert _preflight(Sensitivity.INTERNAL, Sensitivity.PUBLIC, pii_blocked=True).code == "pii_blocked"


def test_a_public_router_refuses_a_confidential_call_without_reaching_the_provider(runtime_router):
    router = runtime_router.build(_policy(Sensitivity.PUBLIC))
    with pytest.raises(idesc.ImageInputError):
        _describe(router, Sensitivity.CONFIDENTIAL)
    assert runtime_router.provider.calls == 0
    assert runtime_router.seen == []


def test_the_old_hardcoded_confidential_default_fails_closed_on_a_public_router(runtime_router):
    """민감도를 안 넘기면 기본값은 CONFIDENTIAL 이다 — public Router 에서는 나가지 않는다.

    즉 호출부가 정책 값을 빠뜨려도 **넓은 쪽으로 열리지 않고 닫힌다.**
    """
    router = runtime_router.build(_policy(Sensitivity.PUBLIC))
    with pytest.raises(idesc.ImageInputError):
        idesc.describe_image(
            IMAGE, router=router, model=MODEL, provider=PROVIDER,
            max_output_tokens=256, timeout_seconds=30,
        )
    assert runtime_router.provider.calls == 0


# 3 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy_level", "sent"),
    [
        (Sensitivity.PUBLIC, Sensitivity.INTERNAL),  # 같은 일반 업무 단계로 모인다
        (Sensitivity.INTERNAL, Sensitivity.INTERNAL),
        (Sensitivity.CONFIDENTIAL, Sensitivity.CONFIDENTIAL),  # 기밀은 내려가지 않는다
    ],
)
def test_the_sensitivity_is_the_contract_value_never_widened(runtime_router, policy_level, sent):
    from tybot.archive.vision_sensitivity import request_sensitivity

    router = runtime_router.build(_policy(policy_level))
    _describe(router, request_sensitivity(policy_level))
    assert runtime_router.seen == [sent]
    assert runtime_router.provider.calls == 1


@pytest.mark.parametrize(
    ("policy_level", "asked"),
    [
        (Sensitivity.PUBLIC, Sensitivity.CONFIDENTIAL),
        (Sensitivity.INTERNAL, Sensitivity.CONFIDENTIAL),
    ],
)
def test_a_request_wider_than_the_policy_router_never_goes_out(runtime_router, policy_level, asked):
    router = runtime_router.build(_policy(policy_level))
    with pytest.raises(idesc.ImageInputError):
        _describe(router, asked)
    assert runtime_router.provider.calls == 0


def test_a_non_sensitivity_value_is_rejected_before_any_call(runtime_router):
    router = runtime_router.build(_policy(Sensitivity.PUBLIC))
    with pytest.raises(idesc.ImageInputError):
        _describe(router, "public")  # 문자열은 받지 않는다 — 열거형만
    assert runtime_router.provider.calls == 0
