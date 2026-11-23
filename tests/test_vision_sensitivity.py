"""Vision 민감도 계약 (오너 결정 2026-11-19) — B-81·B-80 이 같은 규칙을 쓴다."""

from __future__ import annotations

import pytest

from tybot.archive import vision_sensitivity as vs
from tybot.gateway.base import Sensitivity as S


def test_general_business_labels_collapse_to_one_stage():
    assert vs.normalize(S.PUBLIC) is vs.GENERAL_BUSINESS
    assert vs.normalize(S.INTERNAL) is vs.GENERAL_BUSINESS
    assert vs.normalize(S.CONFIDENTIAL) is S.CONFIDENTIAL


@pytest.mark.parametrize(("catalog", "policy", "ok"), [
    (S.PUBLIC, S.PUBLIC, True),
    (S.PUBLIC, S.INTERNAL, True),          # 표기 불일치로 막지 않는다
    (S.INTERNAL, S.PUBLIC, True),
    (S.PUBLIC, S.CONFIDENTIAL, False),     # 기밀은 그대로 막는다
    (S.INTERNAL, S.CONFIDENTIAL, False),
    (S.CONFIDENTIAL, S.CONFIDENTIAL, True),
    (S.CONFIDENTIAL, S.INTERNAL, True),
])
def test_catalog_allows_by_contract_stage(catalog, policy, ok):
    assert vs.catalog_allows(catalog, policy) is ok


def test_the_request_value_is_never_widened_or_narrowed_across_stages():
    assert vs.request_sensitivity(S.PUBLIC) is S.INTERNAL
    assert vs.request_sensitivity(S.CONFIDENTIAL) is S.CONFIDENTIAL


@pytest.mark.parametrize("bad", ["public", None, 0])
def test_non_enum_values_are_rejected(bad):
    with pytest.raises(ValueError):
        vs.normalize(bad)


# --- B-80 PDF 페이지 경로도 같은 계약을 쓴다 --------------------------------------

from dataclasses import replace  # noqa: E402

from tests.test_vision_policy import POLICY, check  # noqa: E402


def test_b80_needs_the_catalog_sensitivity_before_any_send():
    assert check(catalog_max_sensitivity=None).code == "catalog_sensitivity_unverified"


@pytest.mark.parametrize("policy_level", [S.PUBLIC, S.INTERNAL])
def test_b80_general_business_pages_pass_a_public_catalog(policy_level):
    assert check(policy=replace(POLICY, sensitivity=policy_level)).ready


def test_b80_confidential_pages_stay_blocked_on_a_public_catalog():
    verdict = check(policy=replace(POLICY, sensitivity=S.CONFIDENTIAL))
    assert verdict.code == "sensitivity_not_allowed"


def test_b80_pii_blocking_is_not_relaxed():
    assert check(pii_blocked=True).code == "pii_blocked"


def test_b80_store_grant_and_worker_carry_the_contract_value():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/tybot/archive"
    store = (root / "vision_store.py").read_text(encoding="utf-8")
    worker = (root / "vision_worker.py").read_text(encoding="utf-8")
    extract = (root / "vision_extract.py").read_text(encoding="utf-8")
    assert "sensitivity=request_sensitivity(policy.sensitivity)" in store
    assert "sensitivity=grant.sensitivity" in worker
    assert "Sensitivity.CONFIDENTIAL,\n" not in extract.split("def extract_page", 1)[1].split("messages = [")[0].replace("sensitivity = Sensitivity.CONFIDENTIAL\n", "")
