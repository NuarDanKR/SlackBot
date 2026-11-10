"""민감정보 결정적 마스킹 (B-86).

이 파일이 지키는 것은 **양쪽 방향**이다.

| 놓치면 | 과하면 |
|---|---|
| 평문 민감정보가 아카이브·로그·답변에 남는다 | 근거가 `[이름1]` 로 뒤덮여 답이 사라진다 |

과한 쪽을 가볍게 보기 쉽지만, B-86 이 생긴 이유가 바로 그것이다 — 한 줄 때문에
파일 전체를 막았더니 봇이 「자료가 없다」 고 답했고 사람은 고장으로 읽었다.
가리는 것이 막는 것으로 되돌아가면 아무것도 나아지지 않는다.
"""

from __future__ import annotations

import pytest

from tybot import sensitive_mask as sm


def _masked(text: str) -> str:
    return sm.mask_sensitive(text).text


# --- 1. 가려야 하는 것 --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("주민등록번호 900101-1234567 입니다", sm.RRN),
        ("900101 - 1234567", sm.RRN),
        ("외국인등록번호 900101-5234567", sm.FOREIGN_ID),
        ("연락처 010-1234-5678", sm.PHONE),
        ("사무실 02-123-4567", sm.PHONE),
        ("+82-10-1234-5678", sm.PHONE),
        ("메일 hong@taeyoung.com", sm.EMAIL),
        ("카드 4111-1111-1111-1111 결제", sm.CARD),
        ("계좌 110-123-456789 (국민)", sm.ACCOUNT),
        ("서울특별시 중구 세종대로 110", sm.ADDRESS),
        ("성명: 홍길동", sm.NAME),
    ],
)
def test_each_type_is_masked_and_counted(raw, code):
    got = sm.mask_sensitive(raw)

    assert got.masked
    assert got.counts == {code: 1}
    assert got.types == (code,)
    assert sm.LABELS[code] in got.text


def test_the_original_value_is_gone_from_the_output():
    """가린 자리에 원문 조각이 남으면 가린 것이 아니다."""
    got = sm.mask_sensitive("홍길동 900101-1234567 / 010-1234-5678 / hong@ty.com")

    for secret in ("900101", "1234567", "1234-5678", "hong@ty.com"):
        assert secret not in got.text


# --- 2. 가리면 안 되는 것 -----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "총 1,250,000 원 입금 예정",
        "일련번호 1234-5678-9012-3456 참조",
        "서울 사무소에서 회의했습니다",
        "홍길동 과장이 보고했다",
        "2026-11-10 회의록",
        "공사비 15억원 집행률 72%",
        "문서번호 2026-000123",
    ],
)
def test_ordinary_business_text_is_left_alone(raw):
    """**과한 쪽이 더 조용하다.** 가려진 근거는 없는 근거와 구별되지 않는다."""
    got = sm.mask_sensitive(raw)

    assert not got.masked, f"업무 문장이 가려졌다: {got.text}"
    assert got.text == raw


def test_a_number_group_without_a_card_checksum_is_not_a_card():
    """자릿수만 보면 일련번호까지 먹는다. Luhn 을 통과해야 카드다."""
    assert not sm.mask_sensitive("1234-5678-9012-3456").masked
    assert sm.mask_sensitive("4111-1111-1111-1111").counts == {sm.CARD: 1}


def test_a_digit_run_without_account_context_is_not_an_account():
    """계좌 형식은 은행마다 달라 문맥 없이 가리면 금액·수량이 사라진다."""
    assert not sm.mask_sensitive("110-123-456789").masked
    assert sm.mask_sensitive("계좌 110-123-456789").counts == {sm.ACCOUNT: 1}


def test_an_impossible_birth_date_is_not_a_resident_number():
    # 견본 번호로 회계 일련번호까지 막지 않는다.
    assert not sm.mask_sensitive("991345-1234567").masked


def test_a_place_without_a_street_number_is_not_an_address():
    assert not sm.mask_sensitive("서울특별시 중구 사무소").masked
    assert sm.mask_sensitive("서울특별시 중구 세종대로 110").counts == {sm.ADDRESS: 1}


def test_a_name_is_masked_only_in_a_labelled_field():
    """자유 문장의 두세 글자 한글을 이름으로 보면 문장이 통째로 사라진다.

    명단·계약서는 항목 이름이 붙어 있고, 그 밖의 이름은 `pii_screen` 의 문서
    분류가 계속 격리한다.
    """
    assert sm.mask_sensitive("계약자: 홍길동").counts == {sm.NAME: 1}
    assert not sm.mask_sensitive("홍길동 과장이 보고했다").masked
    assert not sm.mask_sensitive("이번 분기 실적").masked


# --- 3. 되돌릴 수 없다 --------------------------------------------------------


def test_the_mask_carries_no_digest_of_the_value():
    """짧은 해시 토큰을 쓰지 않는다.

    전화번호의 경우의 수는 1억 남짓이라 해시를 통째로 돌려보면 역산된다.
    소금을 쳐도 소금이 같은 저장소에 있으면 같은 이야기다.
    """
    one = _masked("010-1234-5678")
    two = _masked("010-9999-0000")

    # 두 결과가 **같다**. 값에서 나온 글자가 토큰에 섞이지 않았다는 뜻이다.
    assert one == two == "[전화번호1]"


def test_the_ordinal_restarts_in_every_document():
    """문서를 가로질러 사람을 이을 수 있으면 그게 곧 식별이다."""
    first = sm.mask_sensitive("계약자: 홍길동")
    second = sm.mask_sensitive("계약자: 김철수")

    assert first.text == second.text == "계약자: [이름1]"


def test_the_result_object_holds_no_fragment_of_the_original():
    got = sm.mask_sensitive("성명: 홍길동 / 010-1234-5678")

    blob = repr(got)
    for secret in ("홍길동", "1234", "5678"):
        assert secret not in blob


# --- 4. 문서 안에서는 구분된다 ------------------------------------------------


def test_two_different_values_get_two_numbers():
    """전부 `[이름]` 이면 두 사람인지 한 사람이 두 번인지 사라진다."""
    got = sm.mask_sensitive("성명: 홍길동 / 계약자: 김철수")

    assert got.text == "성명: [이름1] / 계약자: [이름2]"
    assert got.counts == {sm.NAME: 2}


def test_the_same_value_keeps_the_same_number_even_when_spelled_differently():
    got = sm.mask_sensitive("010-1234-5678 과 010 1234 5678")

    assert got.text == "[전화번호1] 과 [전화번호1]"
    assert got.counts == {sm.PHONE: 2}


def test_numbering_is_per_type_not_global():
    got = sm.mask_sensitive("성명: 홍길동 전화 010-1234-5678")

    assert "[이름1]" in got.text
    assert "[전화번호1]" in got.text


def test_lines_are_masked_as_one_document():
    """줄마다 따로 가리면 같은 사람이 줄마다 1번을 받아 번호가 쓸모없어진다."""
    rows, got = sm.mask_lines(["계약자: 홍길동", "담당자: 김철수", "성명: 홍길동"])

    assert rows == ["계약자: [이름1]", "담당자: [이름2]", "성명: [이름1]"]
    assert got.counts == {sm.NAME: 3}


# --- 5. 멱등 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "주민등록번호 900101-1234567",
        "성명: 홍길동 / 010-1234-5678",
        "서울특별시 중구 세종대로 110 / hong@ty.com",
    ],
)
def test_masking_twice_changes_nothing(raw):
    """본문은 수집 → 변환 → 답변 → 전달까지 여러 겹을 지난다.

    겹마다 한 번씩 가리면 `[[이름1]2]` 같은 것이 생기고, 그러면 사람도 다음
    단계의 정규식도 그것을 못 읽는다.
    """
    once = _masked(raw)

    assert _masked(once) == once


def test_an_already_masked_token_is_not_counted_again():
    got = sm.mask_sensitive("계약자: [이름1] 담당자: 김철수")

    assert got.counts == {sm.NAME: 1}


def test_a_new_value_never_reuses_a_number_that_is_already_in_the_text():
    """**다른 두 사람이 한 번호를 받으면 안 된다.**

    이미 가려진 `[이름1]` 이 누구였는지는 알 수 없다. 합칠 수는 없으니
    겹치지 않게 한다 — 한 사람이 두 번호로 보이는 것은 읽는 사람이 알아차리지만,
    두 사람이 한 번호로 보이는 것은 알아차릴 길이 없다.
    """
    got = sm.mask_sensitive("계약자: [이름1] 담당자: 김철수")

    assert got.text == "계약자: [이름1] 담당자: [이름2]"


def test_the_reserved_number_is_per_type():
    got = sm.mask_sensitive("연락: [전화번호2] 담당자: 김철수 / 010-9999-0000")

    assert "담당자: [이름1]" in got.text
    assert "[전화번호3]" in got.text


def test_is_masked_sees_a_token_and_plain_text_apart():
    assert sm.is_masked("계약자: [이름1]")
    assert not sm.is_masked("계약자: 홍길동")


# --- 6. 결정적 ----------------------------------------------------------------


def test_the_same_input_always_gives_the_same_output():
    """**LLM 에게 묻지 않는다.** 어제 통과한 글이 오늘 달리 가려지면 아무도
    그 차이를 설명할 수 없다."""
    raw = "성명: 홍길동 / 010-1234-5678 / hong@ty.com / 서울특별시 중구 세종대로 110"

    results = {_masked(raw) for _ in range(20)}

    assert len(results) == 1


def test_the_type_list_has_a_fixed_order():
    # 프론트매터가 실행마다 달라지면 의미 없는 diff 가 쌓인다.
    got = sm.mask_sensitive("hong@ty.com 010-1234-5678 성명: 홍길동")

    assert got.types == tuple(
        code for code in sm.TYPES if code in {sm.PHONE, sm.EMAIL, sm.NAME}
    )


# --- 7. 겹치는 규칙의 순서 ----------------------------------------------------


def test_a_resident_number_is_not_re_read_as_a_phone_number():
    """긴 것부터 가려야 짧은 규칙이 그 조각을 다시 집지 않는다."""
    got = sm.mask_sensitive("900101-1234567")

    assert got.counts == {sm.RRN: 1}
    assert got.text == "[주민등록번호1]"


def test_an_email_is_not_split_by_the_phone_rule():
    got = sm.mask_sensitive("call-01012345678@ty.com")

    assert got.counts == {sm.EMAIL: 1}


# --- 8. 전달 직전 관문 --------------------------------------------------------


def test_contains_sensitive_flags_text_the_model_returned_in_the_clear():
    """모델이 평문을 돌려줘도 Slack·Canvas·QA 로그에는 가린 것만 나간다."""
    assert sm.contains_sensitive("확인된 연락처는 010-1234-5678 입니다")
    assert not sm.contains_sensitive("확인된 연락처는 [전화번호1] 입니다")


def test_an_empty_input_is_not_reported_as_masked():
    got = sm.mask_sensitive("")

    assert got.text == ""
    assert not got.masked
    assert got.counts == {}


# --- 9. 프론트매터 ------------------------------------------------------------


def test_metadata_records_the_policy_revision_and_types_only():
    """가린 값도, 주변 원문도 남기지 않는다.

    진단을 돕겠다고 앞 네 글자를 남기면 그 네 글자가 로그에 쌓인다.
    """
    got = sm.mask_sensitive("성명: 홍길동 / 010-1234-5678")
    meta = sm.metadata(got)

    assert meta == {
        "masked": True,
        "mask_revision": sm.POLICY_REVISION,
        "mask_types": [sm.PHONE, sm.NAME],
        "mask_counts": {sm.PHONE: 1, sm.NAME: 1},
    }
    blob = repr(meta)
    for secret in ("홍길동", "1234", "5678"):
        assert secret not in blob


def test_clean_text_metadata_says_so_without_a_revision_lie():
    meta = sm.metadata(sm.mask_sensitive("이번 분기 실적"))

    assert meta["masked"] is False
    assert meta["mask_types"] == []
    assert meta["mask_revision"] == sm.POLICY_REVISION


def test_the_policy_revision_is_a_fixed_word():
    # 규칙이 바뀌면 올린다. 무엇으로 가렸는지 모르면 예전 결과를 설명할 수 없다.
    assert sm.POLICY_REVISION == "mask-v1"
