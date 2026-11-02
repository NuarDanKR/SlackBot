"""B-81 정책 변경 경계. **켤 수 없는 설정을 켜는 순간 막는가**를 건다."""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from tybot.archive.image_description import ImageInput
from tybot.archive.image_description_policy import (
    ImageDescriptionPolicy,
    preflight_image_description,
)
from tybot.archive.image_description_store import CONFIG_KEYS, ImageDescriptionDenied
from tybot.console import image_description_policy_admin as admin

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
WORKSPACE = "tyit"

GOOD = {
    "model": "gemini-2.0-flash",
    "provider": "google",
    "region": "asia-northeast3",
    "sensitivity": "internal",
    "catalog_verification_ttl_seconds": 86400,
    "max_image_bytes": 5_000_000,
    "max_output_tokens": 300,
    "timeout_seconds": 20,
    "max_concurrency": 2,
    "daily_calls": 100,
}


def catalog_row(**over) -> dict:
    base = {
        "model": GOOD["model"],
        "provider": GOOD["provider"],
        "supports_vision": True,
        "zero_cost_verified": True,
        "verified_at": NOW - timedelta(hours=1),
        "verification_revision": "cat-1",
        "max_sensitivity": "confidential",
        "allowed_regions": [GOOD["region"]],
        "credential_ready": True,
        "quota_state": "available",
        "quota_remaining": 100,
        "quota_resets_at": None,
    }
    return base | over


# --- 가짜 DB -----------------------------------------------------------------


class Cursor:
    def __init__(self, policies, catalog):
        self.policies = policies
        self.catalog = catalog
        self.rows: list = []

    def execute(self, sql, params=None):
        if "archiver_image_description_policy" in sql:
            self.rows = list(self.policies)
        elif "archiver_image_model_catalog" in sql:
            self.rows = list(self.catalog)
        else:  # pragma: no cover - 이 경계는 두 표만 읽는다
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Connection:
    def __init__(self, policies, catalog):
        self.cur = Cursor(policies, catalog)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Store:
    """`save_policy` 말고는 아무것도 부르면 안 된다."""

    calls: ClassVar[list] = []
    denial: ClassVar[Exception | None] = None

    def __init__(self, conn):
        self.conn = conn

    def save_policy(self, workspace, **kwargs):
        Store.calls.append((workspace, kwargs))
        if Store.denial is not None:
            raise Store.denial
        return "rev-new"

    def __getattr__(self, name):  # pragma: no cover - 불리면 시험이 실패한다
        raise AssertionError(f"정책 저장이 {name} 을 불렀다")


@pytest.fixture(autouse=True)
def store(monkeypatch):
    Store.calls = []
    Store.denial = None
    monkeypatch.setattr(admin, "ImageDescriptionStore", Store)
    return Store


def policy_row(workspace, config, *, enabled=False, revision=None) -> dict:
    return {
        "workspace": workspace,
        "revision": revision or f"rev-{workspace or 'global'}",
        "enabled": enabled,
        "config": dict(config),
    }


def connector(policies=(), catalog=None):
    rows = list(policies)
    entries = [catalog_row()] if catalog is None else list(catalog)
    return lambda: Connection(rows, entries)


def request(**over) -> admin.PolicyChangeRequest:
    base = {
        "workspace": WORKSPACE,
        "expected_revision": f"rev-global:rev-{WORKSPACE}",
        "enabled": True,
        "config": {},
        "reason": "현장 사진 검색을 켠다",
        "actor": "operator@taeyoung.com",
    }
    return admin.PolicyChangeRequest(**(base | over))


def save(req=None, *, policies=None, catalog=None, now=NOW):
    if policies is None:
        policies = [policy_row("", GOOD), policy_row(WORKSPACE, GOOD)]
    return admin.save_policy_change(
        req or request(), now=now, connect=connector(policies, catalog)
    )


def refuse(req=None, **kwargs) -> admin.ImageDescriptionPolicyAdminError:
    with pytest.raises(admin.ImageDescriptionPolicyAdminError) as error:
        save(req, **kwargs)
    return error.value


# --- 요청 검사 ---------------------------------------------------------------


def test_a_clean_change_is_saved():
    said = save()
    assert said.revision == "rev-new"
    assert len(Store.calls) == 1


@pytest.mark.parametrize("field,value,code", [
    ("actor", "", "actor_required"),
    ("actor", "   ", "actor_required"),
    ("reason", "", "reason_required"),
    ("reason", "\n", "reason_required"),
    ("reason", "가" * (admin.MAX_REASON_CHARS + 1), "reason_too_long"),
    ("enabled", "true", "policy_invalid"),
    ("enabled", 1, "policy_invalid"),
    ("config", None, "policy_invalid"),
    ("workspace", None, "policy_invalid"),
    ("expected_revision", 7, "policy_invalid"),
])
def test_a_malformed_request_is_refused_before_any_read(field, value, code):
    assert refuse(request(**{field: value})).code == code
    assert Store.calls == []


def test_an_unknown_config_key_is_refused_not_dropped():
    """조용히 버리면 운영자는 저장됐다고 믿는다."""
    assert refuse(request(config={"max_cost_usd": 1})).code == "config_key_unknown"


@pytest.mark.parametrize("key", sorted(CONFIG_KEYS))
def test_every_config_key_has_a_validator(key):
    assert key in admin.VALIDATORS


@pytest.mark.parametrize("key,value", [
    ("model", ""), ("provider", "  "), ("region", 7), ("sensitivity", "secret"),
    ("sensitivity", "INTERNAL"), ("catalog_verification_ttl_seconds", 0),
    ("catalog_verification_ttl_seconds", float("inf")), ("timeout_seconds", -1),
    ("max_image_bytes", 0), ("max_image_bytes", 2.5), ("max_output_tokens", -3),
    ("max_concurrency", 0), ("daily_calls", "100"),
])
def test_an_invalid_value_names_the_key_it_came_from(key, value):
    assert refuse(request(config={key: value})).code == f"config_invalid_{key}"


@pytest.mark.parametrize("key", ["max_image_bytes", "max_concurrency", "daily_calls"])
def test_a_boolean_does_not_pass_as_a_count(key):
    """`bool` 은 `int` 의 하위형이다. 안 거르면 `True` 가 1 로 통과한다."""
    assert refuse(request(config={key: True})).code == f"config_invalid_{key}"


# --- 충돌 --------------------------------------------------------------------


def test_the_composite_revision_is_split_for_a_workspace_save():
    """읽기 API 는 `전역:워크스페이스` 를 내려 준다. 안 갈라 쓰면 저장이
    영원히 `policy_conflict` 가 된다."""
    save(request(expected_revision="rev-global:rev-tyit"))
    assert Store.calls[0][1]["expected_revision"] == "rev-tyit"


def test_the_composite_revision_is_split_for_a_global_save():
    save(
        request(workspace=admin.GLOBAL, expected_revision="rev-global:rev-tyit"),
        policies=[policy_row("", GOOD)],
    )
    assert Store.calls[0][1]["expected_revision"] == "rev-global"


@pytest.mark.parametrize("value", ["rev-tyit", None])
def test_a_plain_revision_is_passed_through(value):
    save(request(expected_revision=value))
    assert Store.calls[0][1]["expected_revision"] == value


def test_the_storage_conflict_is_reported_as_is():
    """판정은 전역 행을 잠근 저장소가 한다. 여기서 다시 하지 않는다."""
    Store.denial = ImageDescriptionDenied("policy_conflict")
    assert refuse().code == "policy_conflict"


def test_a_database_failure_does_not_leak_its_body():
    Store.denial = RuntimeError("relation ... config={'model': 'secret-model'}")
    said = refuse()
    assert said.code == "policy_unavailable"
    assert "secret-model" not in str(said)


# --- 전역 / 워크스페이스 override --------------------------------------------


def test_a_workspace_inherits_the_global_defaults():
    """워크스페이스가 모델만 덮어써도 나머지는 전역에서 온다."""
    rows = [policy_row("", GOOD), policy_row(WORKSPACE, {"max_concurrency": 1,
                                                         "daily_calls": 5})]
    said = save(request(config={"model": GOOD["model"]}), policies=rows)
    assert said.enabled is True
    assert Store.calls[0][1]["config"] == {"model": GOOD["model"]}


def test_only_the_submitted_keys_are_written():
    """합친 값을 써 넣으면 전역을 고친 순간 워크스페이스가 안 따라온다."""
    save(request(config={"timeout_seconds": 30}))
    assert Store.calls[0][1]["config"] == {"timeout_seconds": 30}


def test_the_changed_keys_are_reported():
    said = save(request(config={"timeout_seconds": 30, "max_output_tokens": 300}))
    # `max_output_tokens` 는 이미 같은 값이다.
    assert said.changed_keys == ("enabled", "timeout_seconds") or \
           said.changed_keys == ("timeout_seconds", "enabled")


def test_turning_it_on_counts_as_a_change():
    said = save(request(config={}))
    assert "enabled" in said.changed_keys


# --- 필수 상한 ---------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(admin.MERGED_KEYS))
def test_activation_needs_every_merged_limit(key):
    partial = {name: value for name, value in GOOD.items() if name != key}
    rows = [policy_row("", partial), policy_row(WORKSPACE, partial)]
    said = refuse(request(config={}), policies=rows)
    assert said.code == "activation_blocked"
    assert f"activation_requires_{key}" in said.blockers
    assert Store.calls == []


@pytest.mark.parametrize("key", sorted(admin.PER_ROW_KEYS))
def test_activation_needs_the_limit_on_the_global_row(key):
    """`reserve` 는 전역 행과 워크스페이스 행에서 **따로** 읽는다."""
    rows = [
        policy_row("", {name: value for name, value in GOOD.items() if name != key}),
        policy_row(WORKSPACE, GOOD),
    ]
    said = refuse(request(config={}), policies=rows)
    assert f"activation_requires_global_{key}" in said.blockers


@pytest.mark.parametrize("key", sorted(admin.PER_ROW_KEYS))
def test_activation_needs_the_limit_on_the_workspace_row(key):
    rows = [
        policy_row("", GOOD),
        policy_row(WORKSPACE, {n: v for n, v in GOOD.items() if n != key}),
    ]
    said = refuse(request(config={}), policies=rows)
    assert f"activation_requires_workspace_{key}" in said.blockers


def test_a_submitted_limit_satisfies_the_per_row_requirement():
    """이번 저장으로 채워 넣는 값도 센다 — 아니면 두 번 저장해야 켜진다."""
    rows = [policy_row("", GOOD),
            policy_row(WORKSPACE, {n: v for n, v in GOOD.items()
                                   if n != "daily_calls"})]
    assert save(request(config={"daily_calls": 50}), policies=rows).enabled is True


def test_turning_it_off_never_needs_the_limits():
    """끄는 것은 언제나 할 수 있어야 한다. 막으면 사고 때 못 끈다."""
    rows = [policy_row("", {}), policy_row(WORKSPACE, {})]
    assert save(request(enabled=False, config={}), policies=rows).enabled is False


def test_a_global_activation_must_stand_on_its_own():
    """전역 행은 상속받을 곳이 없다. 혼자 완전해야 한다."""
    said = refuse(
        request(workspace=admin.GLOBAL, config={}, expected_revision="rev-global"),
        policies=[policy_row("", {"model": "m"})],
    )
    assert said.code == "activation_blocked"
    assert "activation_requires_provider" in said.blockers


# --- 서버 카탈로그에 있는 무료 Vision 모델만 --------------------------------


@pytest.mark.parametrize("over,blocker", [
    ({"supports_vision": False}, "model_vision_not_verified"),
    ({"zero_cost_verified": False}, "model_zero_cost_not_verified"),
    ({"credential_ready": False}, "model_credential_not_ready"),
    ({"allowed_regions": ["us-central1"]}, "model_region_not_allowed"),
    ({"max_sensitivity": "public"}, "model_sensitivity_not_allowed"),
    ({"quota_state": "exhausted", "quota_remaining": 0}, "model_quota_exhausted"),
    ({"quota_state": "unknown", "quota_remaining": None}, "model_quota_unknown"),
    ({"verified_at": NOW - timedelta(days=30)}, "model_verification_stale"),
    ({"verification_revision": "", "zero_cost_verified": False},
     "model_verification_missing"),
])
def test_only_a_verified_free_vision_model_can_be_turned_on(over, blocker):
    said = refuse(request(config={}), catalog=[catalog_row(**over)])
    assert said.code == "activation_blocked"
    assert blocker in said.blockers
    assert Store.calls == []


def test_a_model_outside_the_catalog_is_refused():
    """이름을 자유 입력하면 무료 계약이 없는 모델이 켜진다."""
    said = refuse(request(config={"model": "gpt-4o"}))
    assert "model_model_not_in_catalog" in said.blockers


def test_the_exact_provider_must_match_too():
    said = refuse(request(config={"provider": "openai"}))
    assert "model_model_not_in_catalog" in said.blockers


def test_a_blocked_model_is_not_swapped_for_another():
    """막힌 모델을 바꿔 주면 유료로 조용히 넘어가는 길이 거기서 생긴다."""
    others = [catalog_row(supports_vision=False), catalog_row(model="other-free")]
    said = refuse(request(config={}), catalog=others)
    assert said.code == "activation_blocked"
    assert Store.calls == []


def test_limits_are_reported_before_the_model():
    """민감도·리전이 없으면 애초에 모델을 평가할 수 없다."""
    rows = [policy_row("", {"max_concurrency": 1, "daily_calls": 1}),
            policy_row(WORKSPACE, {"max_concurrency": 1, "daily_calls": 1})]
    said = refuse(request(config={}), policies=rows, catalog=[catalog_row()])
    assert all(code.startswith("activation_requires_") for code in said.blockers)


def test_the_catalog_is_not_consulted_when_turning_off():
    assert save(request(enabled=False, config={}), catalog=[]).enabled is False


# --- 저장은 아무것도 시작하지 않는다 -----------------------------------------


def _code_lines() -> list[str]:
    source = inspect.getsource(admin).replace(admin.__doc__ or "", "")
    return [line for line in source.splitlines() if not line.strip().startswith("#")]


def test_the_module_never_reaches_a_model_or_a_backfill():
    forbidden = ("router", "describe_image", "coordinator", "run_approved",
                 "run_cleared", "dispatch", "reserve", "load_original",
                 "requests", "httpx", "backfill", "subprocess")
    hits = [line for line in _code_lines() for word in forbidden if word in line]
    assert hits == [], hits


def test_saving_calls_the_storage_once_and_nothing_else():
    save()
    assert [name for name, _ in Store.calls] == [WORKSPACE]


def test_a_blocked_activation_writes_nothing():
    refuse(request(config={"model": "gpt-4o"}))
    assert Store.calls == []


# --- 미리보기는 쓰지 않는다 --------------------------------------------------


def preview(req=None, *, policies=None, catalog=None):
    if policies is None:
        policies = [policy_row("", GOOD), policy_row(WORKSPACE, GOOD)]
    return admin.preview_policy_change(
        req or request(), now=NOW, connect=connector(policies, catalog)
    )


def test_the_preview_never_writes():
    said = preview(request(config={"model": "gpt-4o"}))
    assert Store.calls == []
    assert said.savable is False
    assert "model_model_not_in_catalog" in said.blockers


def test_the_preview_reports_a_savable_change():
    said = preview()
    assert said.savable is True
    assert said.row_revision == f"rev-{WORKSPACE}"


def test_the_preview_refuses_a_malformed_request():
    with pytest.raises(admin.ImageDescriptionPolicyAdminError) as error:
        preview(request(reason=""))
    assert error.value.code == "reason_required"


# --- 사유와 실행자 -----------------------------------------------------------


def test_the_audit_record_fits_the_console_audit_store():
    from tybot.console import audit_store

    said = save().audit
    parameters = inspect.signature(audit_store.record).parameters
    assert set(said) <= set(parameters)
    assert {"actor", "category", "action", "target_type", "target_id"} <= set(said)


def test_the_reason_is_recorded_because_the_ledger_has_no_column_for_it():
    said = save(request(reason="현장 소장 요청, 10월 공정 사진 검색")).audit
    assert said["metadata"]["reason"] == "현장 소장 요청, 10월 공정 사진 검색"
    assert said["actor"] == "operator@taeyoung.com"
    assert said["outcome"] == "succeeded"


def test_the_audit_names_the_global_row_readably():
    said = save(
        request(workspace=admin.GLOBAL, expected_revision="rev-global"),
        policies=[policy_row("", GOOD)],
    ).audit
    assert said["target_id"] == "global"
    assert said["workspace"] is None


def test_the_audit_carries_both_revisions():
    said = save().audit
    assert said["metadata"]["previous_revision"] == f"rev-{WORKSPACE}"
    assert said["metadata"]["revision"] == "rev-new"


def test_the_audit_does_not_copy_the_values():
    """값은 원장이 들고 있다. 감사에는 **무엇이 바뀌었는지**만 적는다."""
    said = save(
        request(config={"model": "other-free"}),
        catalog=[catalog_row(), catalog_row(model="other-free")],
    ).audit
    assert "model" in said["metadata"]["changed_keys"]
    assert "other-free" not in str(said["metadata"])


# --- 저장소·전송 판정과 어긋나지 않는다 --------------------------------------


def test_the_key_split_covers_the_storage_contract():
    assert set(CONFIG_KEYS) == admin.MERGED_KEYS | admin.PER_ROW_KEYS
    assert not admin.MERGED_KEYS & admin.PER_ROW_KEYS


def _probe() -> ImageInput:
    return ImageInput(
        workspace=WORKSPACE,
        channel="C0BQYAC28F3",
        file_id="F0C9LHFTHLM",
        original_sha256="a" * 64,
        image=b"\x89PNG",
        mime="image/png",
    )


def _preflight_code(config: dict) -> str:
    policy = ImageDescriptionPolicy(
        workspace=WORKSPACE,
        revision="r",
        enabled=True,
        workspace_enabled=True,
        model=config["model"],
        provider=config["provider"],
        region=config["region"],
        sensitivity=config["sensitivity"],
        catalog_verification_ttl_seconds=config["catalog_verification_ttl_seconds"],
        max_image_bytes=config["max_image_bytes"],
        max_output_tokens=config["max_output_tokens"],
        timeout_seconds=config["timeout_seconds"],
    )
    return preflight_image_description(
        _probe(), policy=policy, catalog=None, approval=None, dispatch=None,
        prompt_revision="probe", now=NOW,
        source_verified=True, access_verified=True,
        pii_blocked=False, bot_generated=False,
    ).code


@pytest.mark.parametrize("key,value", [
    ("model", ""), ("provider", ""), ("region", ""),
    ("catalog_verification_ttl_seconds", 0), ("catalog_verification_ttl_seconds", -1),
    ("max_image_bytes", 0), ("max_output_tokens", 0), ("timeout_seconds", 0),
])
def test_the_validators_agree_with_the_dispatch_gate(key, value):
    """여기서 통과시킨 값이 전송 직전에 거절되면, 운영자는 켰다고 믿고
    아무것도 안 도는 상태를 보게 된다. 두 기준을 맞춰 둔다."""
    probe = GOOD | {"sensitivity": __import__(
        "tybot.gateway.base", fromlist=["Sensitivity"]
    ).Sensitivity(GOOD["sensitivity"])}
    assert admin.VALIDATORS[key](value) is False
    assert _preflight_code(probe | {key: value}) == "policy_invalid"


def test_a_good_config_passes_the_dispatch_gate_policy_checks():
    from tybot.gateway.base import Sensitivity

    said = _preflight_code(GOOD | {"sensitivity": Sensitivity(GOOD["sensitivity"])})
    # 카탈로그를 안 넘겼으므로 정책 단계를 지나 거기서 멈춘다.
    assert said == "catalog_unavailable"
