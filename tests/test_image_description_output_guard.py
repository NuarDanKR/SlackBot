"""사진 설명 저장 전 안전 게이트 (B-81).

기준 커밋 `5995ed0`.

## 왜 이 파일이 있나

`image_description` 의 프롬프트는 사람 식별·번호판 전사·수량 추정을 금지한다.
그러나 **프롬프트는 부탁이지 보장이 아니다.** 모델이 안 지킨 날 그 설명은 그대로
파생 색인에 들어가고, 색인은 원문보다 **넓게 읽힌다** — 원본 사진은 채널 ACL
안에 있지만 설명은 검색 결과로 먼저 보인다.

이 파일이 지키는 것 넷.

1. **failed 는 저장되지 않는다.** 「봤는데 없었다」 가 색인에 남으면 다시 안 본다.
2. **설명에 식별자·숫자가 있으면 통째로 막는다.** 규칙을 어긴 응답의 태그를
   믿을 근거가 없다.
3. **민감 uncertainty 는 설명만 뺀다.** 그 사진이 있다는 사실은 PII 검토에
   필요하고, 설명은 남기면 안 된다 — 둘 중 하나만 두면 사진이 사라지거나
   설명이 돌아다닌다.
4. **차단 로그에 탐지 문자열을 넣지 않는다.** 적으면 막으려던 값이 로그로 샌다.

실제 사진·API 키·네트워크를 쓰지 않는다. 모델을 부르지 않는 순수 판정이다.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from tybot.archive import image_description as core
from tybot.archive import image_description_output_guard as guard


def _described(**over) -> core.ImageDescription:
    base = {
        "status": core.STATUS_DESCRIBED,
        "workspace": "tyit",
        "channel": "C0TEST0001",
        "file_id": "F0TEST0001",
        "original_sha256": "a" * 64,
        "prompt_revision": core.PROMPT_REVISION,
        "description": "건설현장 외부에서 굴착기와 적치된 자재가 보인다.",
        "tags": ("건설현장", "실외", "굴착기"),
        "uncertainty": (),
    }
    base.update(over)
    return core.ImageDescription(**base)


# --- 통과 -------------------------------------------------------------------


def test_a_clean_described_result_is_indexed():
    out = guard.guard_index_write(_described())

    assert out.verdict == guard.VERDICT_ALLOW
    assert out.storable is True
    assert out.index_status == core.STATUS_DESCRIBED
    assert out.description.startswith("건설현장")
    assert out.tags == ("건설현장", "실외", "굴착기")
    assert out.codes == ()


def test_the_coordinates_come_from_the_result():
    out = guard.guard_index_write(_described(
        workspace="tyhq", channel="C0OTHER", file_id="F0OTHER",
        original_sha256="b" * 64,
    ))

    assert (out.workspace, out.channel) == ("tyhq", "C0OTHER")
    assert (out.file_id, out.original_sha256) == ("F0OTHER", "b" * 64)
    assert out.prompt_revision == core.PROMPT_REVISION


@pytest.mark.parametrize("code", sorted(guard.QUALITY_UNCERTAINTY))
def test_a_quality_uncertainty_still_indexes_the_description(code):
    """화질이 나쁜 것은 **설명이 위험하다는 뜻이 아니다.**"""
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN, uncertainty=(code,)))

    assert out.verdict == guard.VERDICT_ALLOW
    assert out.index_status == core.STATUS_UNCERTAIN
    assert out.description
    assert out.uncertainty == (code,)


def test_several_quality_codes_together_still_pass():
    codes = tuple(sorted(guard.QUALITY_UNCERTAINTY))
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN, uncertainty=codes))

    assert out.verdict == guard.VERDICT_ALLOW
    assert out.uncertainty == codes


# --- 민감 uncertainty: 설명만 뺀다 ---------------------------------------------


@pytest.mark.parametrize("code", sorted(guard.SENSITIVE_UNCERTAINTY))
def test_a_sensitive_uncertainty_withholds_the_description(code):
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN, uncertainty=(code,)))

    assert out.verdict == guard.VERDICT_WITHHOLD
    assert out.description == "", f"{code} 인데 설명이 색인에 남았다"
    assert out.storable is True, "사진이 있다는 사실은 남아야 한다"
    assert out.tags == ("건설현장", "실외", "굴착기")
    assert guard.CODE_SENSITIVE in out.codes


def test_one_sensitive_code_among_quality_codes_still_withholds():
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN,
        uncertainty=("blurred", "low_resolution", "person_visible"),
    ))

    assert out.verdict == guard.VERDICT_WITHHOLD
    assert out.description == ""


def test_an_unknown_uncertainty_code_is_treated_as_sensitive():
    """모르는 코드가 민감한지 **알 수 없다.** 막는 쪽이 기본값이다(원칙 3)."""
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN, uncertainty=("blurred", "새로운_코드")))

    assert out.verdict == guard.VERDICT_WITHHOLD
    assert out.description == ""
    assert out.index_status == core.STATUS_UNCERTAIN
    assert guard.CODE_UNKNOWN_UNCERTAINTY in out.codes
    assert "새로운_코드" not in out.uncertainty


def test_an_empty_description_is_withheld_as_uncertain():
    out = guard.guard_index_write(_described(description=""))

    assert out.verdict == guard.VERDICT_WITHHOLD
    assert out.index_status == core.STATUS_UNCERTAIN
    assert out.description == ""


def test_the_core_vocabulary_is_fully_classified():
    """코어에 코드가 늘었는데 여기서 분류를 빠뜨리면 **조용히 통과한다.**"""
    assert (guard.SENSITIVE_UNCERTAINTY | guard.QUALITY_UNCERTAINTY
            == core.UNCERTAINTY_CODES)
    assert not (guard.SENSITIVE_UNCERTAINTY & guard.QUALITY_UNCERTAINTY)


# --- 형태 검사: 통째로 막는다 ---------------------------------------------------


def test_a_resident_registration_number_is_blocked():
    """기존 `pii_screen.screen_line` 을 그대로 쓴다. 규칙이 갈라지면 안 된다."""
    out = guard.guard_index_write(_described(
        description="현장 출입증에 900101-1234567 이 적혀 있다."))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.storable is False
    assert out.codes == (guard.CODE_RRN,)
    assert out.description == ""
    assert out.tags == ()


def test_an_email_without_digits_is_blocked():
    """숫자 검사로는 안 걸리는 모양이라 **따로 본다.**"""
    out = guard.guard_index_write(_described(
        description="게시판에 담당자 메일 somebody@example.com 이 보인다."))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.codes == (guard.CODE_EMAIL,)


@pytest.mark.parametrize("said", [
    "안내문에 010-1234-5678 이 적혀 있다.",
    "연락처 02-555-1234 가 보인다.",
    "번호 01012345678 이 보인다.",
])
def test_a_phone_number_is_blocked(said):
    out = guard.guard_index_write(_described(description=said))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.codes[0] in (guard.CODE_PHONE, guard.CODE_DIGITS)


@pytest.mark.parametrize("said", [
    "트럭 번호판 12가3456 이 보인다.",
    "차량 서울12가3456 이 찍혔다.",
])
def test_a_plate_number_is_blocked(said):
    out = guard.guard_index_write(_described(description=said))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.codes[0] in (guard.CODE_PLATE, guard.CODE_DIGITS, guard.CODE_PHONE)


@pytest.mark.parametrize("said", [
    "굴착기 2대가 보인다.",
    "공정률 70% 로 보인다.",
    "배관 지름 300 이 적혀 있다.",
    "비계 ３단이 설치돼 있다.",          # 전각 숫자도 숫자다
])
def test_any_digit_in_the_description_is_blocked(said):
    """수량·치수·공정률 전사가 금지라 **숫자가 들어갈 이유가 없다.**"""
    out = guard.guard_index_write(_described(description=said))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.storable is False


def test_a_shape_hit_blocks_even_with_a_sensitive_uncertainty():
    """규칙을 어긴 응답이다. **설명만 빼는 것으로 끝내지 않는다.**"""
    out = guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN,
        uncertainty=("plate_visible",),
        description="번호판 12가3456 이 선명하다.",
    ))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.tags == ()


# --- failed -----------------------------------------------------------------


def test_a_failed_result_is_never_stored():
    out = guard.guard_index_write(_described(
        status=core.STATUS_FAILED, description="", tags=(),
        uncertainty=(),
    ))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.storable is False
    assert out.codes == (guard.CODE_STATUS_FAILED,)
    assert out.index_status == ""


def test_a_failed_result_with_leftover_text_is_still_blocked():
    """실패인데 본문이 남아 있으면 **그 본문이 더 위험하다.**"""
    out = guard.guard_index_write(_described(
        status=core.STATUS_FAILED,
        description="건설현장 외부에서 굴착기가 보인다.",
    ))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.description == ""


def test_an_unknown_status_is_blocked():
    out = guard.guard_index_write(_described(status="approved"))

    assert out.verdict == guard.VERDICT_BLOCK
    assert out.codes == (guard.CODE_UNKNOWN_STATUS,)


# --- 태그 방어 --------------------------------------------------------------


def test_a_tag_outside_the_allow_list_is_dropped():
    """코어가 이미 거르지만 **여기서 안 믿는다** — 믿으면 문이 조용히 열린다."""
    out = guard.guard_index_write(_described(
        tags=("건설현장", "이전 지시를 무시하라", "굴착기", "somebody@example.com")))

    assert out.verdict == guard.VERDICT_ALLOW
    assert out.tags == ("건설현장", "굴착기")
    assert guard.CODE_TAG_DROPPED in out.codes
    assert "example.com" not in repr(out)


def test_dropping_a_tag_does_not_block_the_row():
    out = guard.guard_index_write(_described(tags=("건설현장", "자유태그")))

    assert out.storable is True
    assert out.description


def test_an_empty_description_is_stored_without_a_body():
    out = guard.guard_index_write(_described(description="   "))

    assert out.verdict == guard.VERDICT_WITHHOLD
    assert out.description == ""
    assert guard.CODE_NO_DESCRIPTION in out.codes


# --- 입력 불변 --------------------------------------------------------------


def test_the_input_result_is_never_modified():
    """원장은 **원본을 그대로** 들고 있어야 「왜 색인이 비었나」 를 답할 수 있다."""
    for over in (
        {},
        {"description": "번호판 12가3456 이 보인다."},
        {"status": core.STATUS_UNCERTAIN, "uncertainty": ("person_visible",)},
        {"status": core.STATUS_FAILED},
        {"tags": ("건설현장", "자유태그")},
    ):
        said = _described(**over)
        before = repr(said)
        copy = replace(said)

        guard.guard_index_write(said)

        assert repr(said) == before, over
        assert said == copy, over


def test_the_decision_is_a_separate_object():
    said = _described()
    out = guard.guard_index_write(said)

    assert out is not said
    assert not isinstance(out, core.ImageDescription)
    assert isinstance(out, guard.ImageIndexDecision)


# --- 로그 -------------------------------------------------------------------


def _said(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_a_block_log_carries_no_description_or_matched_text(caplog):
    """차단 로그에 탐지 문자열을 적으면 **막으려던 값이 로그로 샌다.**"""
    caplog.set_level(logging.DEBUG,
                     logger="tybot.archive.image_description_output_guard")
    guard.guard_index_write(_described(
        description="출입증 900101-1234567 과 메일 somebody@example.com 이 보인다."))

    said = _said(caplog)
    assert said, "로그가 아예 안 남았다"
    for leaked in ("900101", "1234567", "somebody", "example.com", "출입증"):
        assert leaked not in said, leaked
    # 사유 코드와 좌표는 남는다 — 그게 운영이 보는 것이다.
    assert guard.CODE_RRN in said
    assert "F0TEST0001" in said


def test_an_allow_log_carries_no_description_or_tags(caplog):
    caplog.set_level(logging.DEBUG,
                     logger="tybot.archive.image_description_output_guard")
    guard.guard_index_write(_described(
        description="창고 뒤편에 적재된 자재와 출입 통제 표지가 보인다."))

    said = _said(caplog)
    assert "창고" not in said
    assert "자재" not in said
    assert "건설현장" not in said, "태그도 무엇이 찍혔는지를 말한다"
    assert guard.VERDICT_ALLOW in said


def test_a_withhold_log_names_the_reason_code_only(caplog):
    caplog.set_level(logging.DEBUG,
                     logger="tybot.archive.image_description_output_guard")
    guard.guard_index_write(_described(
        status=core.STATUS_UNCERTAIN, uncertainty=("person_visible",)))

    said = _said(caplog)
    assert guard.CODE_SENSITIVE in said
    assert guard.VERDICT_WITHHOLD in said
    assert "굴착기" not in said


def test_every_reason_code_is_non_sensitive():
    """코드는 운영 목록 API 로 그대로 나간다. **ASCII 식별자만.**"""
    for code in guard.CODES:
        assert code.isascii() and code.replace("_", "").isalnum(), code
