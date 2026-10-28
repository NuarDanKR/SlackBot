"""전송 직전 안전 판정. **기본이 닫혀 있는지**를 중심으로 건다."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from tybot.archive.attachment_doc import AttachmentDoc
from tybot.archive.image_description_clearance import (
    ACL_CHANGED,
    ACL_UNKNOWN,
    ASSESSMENT_EXPIRED,
    ASSESSMENT_INVALID,
    ASSESSMENT_MISSING,
    ASSESSMENT_NOT_YET_VALID,
    ASSESSMENT_REVOKED,
    CHANNEL_MISMATCH,
    CODES,
    DOC_COORDINATE_MISMATCH,
    DOC_MISSING,
    FILE_MISMATCH,
    INVALID_CLOCK,
    MODEL_MISMATCH,
    ORIGINAL_SHA256_MISMATCH,
    PII_NOT_CLEARED,
    POLICY_REVISION_MISMATCH,
    PROVIDER_MISMATCH,
    VISIBILITY_CHANGED,
    VISIBILITY_UNKNOWN,
    WORKSPACE_MISMATCH,
    Clearance,
    SafetyAssessment,
    clear_for_dispatch,
)
from tybot.archive.image_description_runner import CALLER_CHECKS
from tybot.archive.image_description_source import ImageCandidate

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
SHA = "a" * 64
WS, CH, FID = "tyit", "C0BQYAC28F3", "F0C9LHFTHLM"
ACL = frozenset({"U_ONE", "U_TWO"})
PROVIDER, MODEL, POLICY = "approved-provider", "free-vision-v1", "g:7"

#: 결과에 **절대 나오면 안 되는** 값들(규칙 7).
FILENAME = "주민등록증-사본.png"
PERSON = "U_REVIEWER_HONG"


def candidate(**over) -> ImageCandidate:
    base = dict(
        workspace=WS, channel_id=CH, file_id=FID, original_sha256=SHA,
        filetype="png", mime="image/png", visibility="internal", acl=ACL,
        text_screened=False, conversion_state="converted",
    )
    return ImageCandidate(**(base | over))


def doc(**over) -> AttachmentDoc:
    base = dict(
        workspace=WS, channel_id=CH, channel="#팀_전산(ABB)_공지", file_id=FID,
        name=FILENAME, revision="r1", visibility="internal", acl=ACL,
        sha256=SHA, filetype="png", conversion_state="converted",
        human_upload_confirmed=True,
    )
    return AttachmentDoc(**(base | over))


def assessment(**over) -> SafetyAssessment:
    base = dict(
        workspace=WS, channel_id=CH, file_id=FID, original_sha256=SHA,
        provider=PROVIDER, model=MODEL, policy_revision=POLICY,
        visibility="internal", acl=ACL, assessed_by=PERSON,
        assessed_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        pii_cleared=True,
    )
    return SafetyAssessment(**(base | over))


def run(**over) -> Clearance:
    kwargs = dict(
        doc=doc(), assessment=assessment(), provider=PROVIDER, model=MODEL,
        policy_revision=POLICY, now=NOW,
    )
    said = over.pop("candidate", None) or candidate()
    return clear_for_dispatch(said, **(kwargs | over))


# --- 기본값 ------------------------------------------------------------------


def test_the_default_clearance_is_closed():
    """아무 말 안 한 상태는 **보내지 마라** 여야 한다."""
    empty = Clearance(WS, CH, FID)
    assert empty.access_verified is False
    assert empty.pii_blocked is True
    assert empty.cleared is False


def test_the_checks_match_what_the_runner_accepts():
    """이름이 어긋나면 러너가 `ValueError` 로 거절한다 — 조용히 안 맞는 게 아니다."""
    assert set(run().as_checks()) == set(CALLER_CHECKS)


def test_a_fully_bound_assessment_clears():
    said = run()
    assert said.cleared is True
    assert (said.access_verified, said.pii_blocked, said.code) == (True, False, "")
    assert said.as_checks() == {"access_verified": True, "pii_blocked": False}


# --- 시계 · 승인 존재 --------------------------------------------------------


@pytest.mark.parametrize("clock", [
    datetime(2026, 10, 28, 9, 0),          # tz 없음
    "2026-10-28T09:00:00Z",
    None,
])
def test_a_clock_we_cannot_compare_refuses(clock):
    assert run(now=clock).code == INVALID_CLOCK


def test_no_assessment_refuses():
    assert run(assessment=None).code == ASSESSMENT_MISSING


# --- 승인 꼴 -----------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"workspace": ""},
    {"channel_id": "   "},
    {"file_id": ""},
    {"provider": ""},
    {"model": ""},
    {"policy_revision": ""},
    {"visibility": ""},
    {"channel_id": " " + CH},              # 앞뒤 공백은 결속을 무르게 한다
    {"original_sha256": "A" * 64},         # 대문자 표기
    {"original_sha256": "a" * 63},
    {"original_sha256": "z" * 64},
    {"acl": ["U_ONE"]},                    # 집합이 아니다
    {"pii_cleared": 1},                    # 참처럼 보이지만 bool 이 아니다
    {"revoked": "no"},
    {"assessed_at": datetime(2026, 10, 28, 8, 0)},        # tz 없음
    {"expires_at": datetime(2026, 10, 28, 10, 0)},        # tz 없음
    {"expires_at": NOW - timedelta(hours=2)},             # 발급보다 이르다
])
def test_an_unusable_assessment_refuses(over):
    assert run(assessment=assessment(**over)).code == ASSESSMENT_INVALID


# --- 만료 · 취소 -------------------------------------------------------------


def test_a_revoked_assessment_refuses():
    assert run(assessment=assessment(revoked=True)).code == ASSESSMENT_REVOKED


def test_revocation_beats_a_still_valid_window():
    """철회는 **만료를 기다리지 않는다.**"""
    said = assessment(revoked=True, expires_at=NOW + timedelta(days=30))
    assert run(assessment=said).code == ASSESSMENT_REVOKED


def test_an_assessment_from_the_future_refuses():
    said = assessment(assessed_at=NOW + timedelta(minutes=1))
    assert run(assessment=said).code == ASSESSMENT_NOT_YET_VALID


@pytest.mark.parametrize("expires", [NOW, NOW - timedelta(seconds=1)])
def test_an_expired_assessment_refuses(expires):
    """만료 시각 **정각도** 만료다."""
    said = assessment(assessed_at=NOW - timedelta(hours=2), expires_at=expires)
    assert run(assessment=said).code == ASSESSMENT_EXPIRED


# --- 결속 --------------------------------------------------------------------


@pytest.mark.parametrize("over,code", [
    ({"workspace": "other"}, WORKSPACE_MISMATCH),
    ({"channel_id": "C9999999999"}, CHANNEL_MISMATCH),
    ({"file_id": "F9999999999"}, FILE_MISMATCH),
    ({"original_sha256": "b" * 64}, ORIGINAL_SHA256_MISMATCH),
    ({"provider": "other-provider"}, PROVIDER_MISMATCH),
    ({"model": "other-model"}, MODEL_MISMATCH),
    ({"policy_revision": "g:8"}, POLICY_REVISION_MISMATCH),
])
def test_an_assessment_for_something_else_refuses(over, code):
    assert run(assessment=assessment(**over)).code == code


@pytest.mark.parametrize("over,code", [
    ({"provider": "other-provider"}, PROVIDER_MISMATCH),
    ({"model": "other-model"}, MODEL_MISMATCH),
    ({"policy_revision": "g:8"}, POLICY_REVISION_MISMATCH),
])
def test_changing_the_route_after_approval_refuses(over, code):
    """승인 뒤 모델·정책을 바꾸면 그 도장은 **다른 경로**의 것이다."""
    assert run(**over).code == code


def test_a_different_original_in_the_candidate_refuses():
    said = candidate(original_sha256="c" * 64)
    assert run(candidate=said).code == ORIGINAL_SHA256_MISMATCH


# --- 정본 --------------------------------------------------------------------


def test_no_doc_refuses():
    assert run(doc=None).code == DOC_MISSING


@pytest.mark.parametrize("over", [
    {"workspace": "other"},
    {"channel_id": "C9999999999"},
    {"file_id": "F9999999999"},
])
def test_a_doc_for_another_coordinate_refuses(over):
    assert run(doc=doc(**over)).code == DOC_COORDINATE_MISMATCH


def test_a_doc_whose_original_changed_refuses():
    """같은 자리에 다른 원본이 들어왔다. 사람이 본 사진이 아니다."""
    assert run(doc=doc(sha256="d" * 64)).code == ORIGINAL_SHA256_MISMATCH


def test_a_doc_with_no_sha_refuses():
    assert run(doc=doc(sha256="")).code == ORIGINAL_SHA256_MISMATCH


# --- 권한 --------------------------------------------------------------------


def test_an_empty_visibility_is_unknown_not_public():
    assert run(doc=doc(visibility="")).code == VISIBILITY_UNKNOWN


@pytest.mark.parametrize("acl", [frozenset(), set(), None, "U_ONE"])
def test_an_empty_or_unreadable_acl_is_unknown(acl):
    assert run(doc=doc(acl=acl)).code == ACL_UNKNOWN


def test_a_changed_visibility_refuses():
    said = doc(visibility="public")
    assert run(doc=said).code == VISIBILITY_CHANGED


@pytest.mark.parametrize("acl", [
    ACL | {"U_THREE"},          # 넓어졌다
    frozenset({"U_ONE"}),       # 좁아졌다
    frozenset({"U_ONE", "U_X"}),
])
def test_a_changed_acl_refuses(acl):
    """넓어졌는지 좁아졌는지 **여기서 알 수 없다.** 맞혀서 통과시키지 않는다."""
    assert run(doc=doc(acl=acl)).code == ACL_CHANGED


def test_the_acl_is_compared_as_a_set_not_an_order():
    said = doc(acl=frozenset({"U_TWO", "U_ONE"}))
    assert run(doc=said).cleared is True


# --- 개인정보 ----------------------------------------------------------------


def test_without_an_explicit_human_clearance_pii_stays_blocked():
    said = run(assessment=assessment(pii_cleared=False))
    assert said.code == PII_NOT_CLEARED
    assert said.pii_blocked is True


def test_a_clearance_with_no_person_behind_it_is_not_a_clearance():
    """`pii_cleared=True` 만 있고 **누가** 승인했는지 없으면 승인이 아니다."""
    said = run(assessment=assessment(assessed_by="   "))
    assert said.code == PII_NOT_CLEARED
    assert said.pii_blocked is True


def test_waiting_for_review_still_reports_access_verified():
    """「권한 모름」 과 「사람이 아직 안 봄」 은 사람이 할 일이 다르다."""
    said = run(assessment=assessment(pii_cleared=False))
    assert said.access_verified is True
    assert said.cleared is False


@pytest.mark.parametrize("screened", [True, False])
def test_text_screening_never_moves_the_pii_verdict(screened):
    """OCR 이 돌았다는 사실은 **개인정보가 없다는 뜻이 아니다**(규칙 4)."""
    blocked = run(
        candidate=candidate(text_screened=screened),
        assessment=assessment(pii_cleared=False),
    )
    assert blocked.pii_blocked is True
    cleared = run(candidate=candidate(text_screened=screened))
    assert cleared.pii_blocked is False


def _code_lines() -> list[str]:
    """모듈에서 **실행되는 줄**만. 설명과 주석은 뺀다.

    모듈 설명은 `text_screened` 를 왜 안 읽는지 길게 적어 둔 자리라, 그대로
    훑으면 설명이 곧 위반으로 잡힌다.
    """
    import inspect

    from tybot.archive import image_description_clearance as mod

    source = inspect.getsource(mod).replace(mod.__doc__ or "", "")
    return [line for line in source.splitlines() if not line.strip().startswith("#")]


def test_the_module_never_reads_text_screened():
    """읽고 무시하는 것보다 **안 읽는** 쪽이 낫다 — 다음 사람이 조건을 붙인다."""
    body = [line for line in _code_lines() if "text_screened" in line]
    assert body == [], body


# --- 닫히는 방향 -------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"assessment": None},
    {"assessment": assessment(revoked=True)},
    {"assessment": assessment(expires_at=NOW - timedelta(seconds=1))},
    {"assessment": assessment(workspace="other")},
    {"doc": None},
    {"doc": doc(visibility="")},
    {"doc": doc(acl=frozenset())},
    {"doc": doc(sha256="d" * 64)},
    {"now": None},
])
def test_every_refusal_closes_both_axes(over):
    """승인을 믿을 수 없는데 그 승인이 말한 「개인정보 없음」 만 살릴 수 없다."""
    said = run(**over)
    assert (said.access_verified, said.pii_blocked) == (False, True)
    assert said.cleared is False
    assert said.code


def test_every_code_is_a_known_ascii_constant():
    for code in CODES:
        assert code.isascii() and re.fullmatch(r"[a-z0-9_]+", code), code


def test_the_result_always_carries_the_candidate_coordinates():
    said = run(assessment=None)
    assert (said.workspace, said.channel_id, said.file_id) == (WS, CH, FID)


# --- 규칙 6 · 7 --------------------------------------------------------------


def test_nothing_sensitive_reaches_the_result():
    """본문·파일명·경로·사람 이름이 결과에 없어야 한다(규칙 7)."""
    said = run(assessment=assessment(pii_cleared=False))
    printed = repr(said) + str(said) + str(said.as_checks())
    for secret in (FILENAME, PERSON, "주민등록증", "#팀_전산(ABB)_공지"):
        assert secret not in printed, secret


def test_the_module_calls_no_model_db_file_or_network():
    """규칙 6. 주석이 아니라 **실행되는 줄**만 본다."""
    lines = _code_lines()
    forbidden = (
        "psycopg", "requests", "httpx", "socket", "urllib", "router",
        "open(", "Path(", "subprocess", "describe_image",
    )
    hits = [line for line in lines for word in forbidden if word in line]
    assert hits == [], hits


def test_the_inputs_are_never_modified():
    said_candidate, said_doc, said_assessment = candidate(), doc(), assessment()
    before = (said_candidate, said_doc, said_assessment)
    clear_for_dispatch(
        said_candidate, doc=said_doc, assessment=said_assessment,
        provider=PROVIDER, model=MODEL, policy_revision=POLICY, now=NOW,
    )
    assert (said_candidate, said_doc, said_assessment) == before
    assert said_assessment.acl == ACL
    assert said_doc.acl == ACL
