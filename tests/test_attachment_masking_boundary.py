"""가릴 수 없는 것은 **계속 격리한다** (B-86 경계).

B-86 은 「PII 가 있으면 전체 금지」 를 「가린 뒤 보관」 으로 바꿨다. 그 완화가
**어디까지인지**가 이 파일의 주제다.

## 가릴 수 있는 것과 없는 것

텍스트 안의 **값**은 가릴 수 있다. 전화번호 자리에 `[전화번호1]` 을 넣으면 그
문장은 여전히 읽히고 값은 사라진다.

**문서 자체가 민감물**인 것은 가릴 수 없다. 등기부등본에서 번호를 지워도 그것은
여전히 등기부등본이고, 남은 모든 칸이 한 사람을 가리킨다. 이미지·스캔본은 더
분명하다 — 글자의 **위치**를 알아야 가릴 수 있고, 위치를 모르면 가린 척밖에
못 한다.

그래서 경계가 둘로 갈린다.

| | 어떻게 |
|---|---|
| 대화·추출문 안의 값 | 가리고 보관 (`sensitive_mask`) |
| 고위험 문서 분류 | 격리 (`pii_screen.screen_document`) |
| 변환 못 한 바이너리·이미지 | 격리 (추출문이 없으면 검사도 못 한다) |

이 파일은 아래쪽 둘이 **완화되지 않았는지**만 본다. 마스킹이 생겼다고 격리가
조용히 열리면, 가장 위험한 자료부터 들어온다.
"""

from __future__ import annotations

import pytest

from tybot import pii_screen, sensitive_mask

ROSTER = """계약자 명단
성명 | 주민등록번호 | 연락처 | 주소
홍길동 | 900101-1234567 | 010-1234-5678 | 서울특별시 중구 세종대로 110
김철수 | 880202-2345678 | 010-2222-3333 | 서울특별시 종로구 종로 1
이영희 | 910303-1456789 | 010-4444-5555 | 서울특별시 마포구 월드컵로 10
"""


def test_a_personal_roster_is_still_isolated_not_masked():
    """명단은 **문서 자체가 민감물**이다.

    번호를 가려도 남은 칸(이름·연락처·주소·행 순서)이 한 사람을 가리킨다.
    가려서 보관하면 「가렸으니 괜찮다」 는 기록만 남고 자료는 그대로 있다.
    """
    got = pii_screen.screen_document(ROSTER, filename="계약자명단_2026.xlsx")

    assert got.blocked
    assert got.block_code in (pii_screen.DOC_ROSTER, pii_screen.RRN)


def test_a_registry_document_is_still_isolated():
    body = """등기사항전부증명서
표제부
갑구 | 소유권 | 홍길동 | 900101-1234567
을구 | 근저당권 | 채권최고액 500,000,000원
"""
    got = pii_screen.screen_document(body, filename="등기부등본.pdf")

    assert got.blocked


def test_masking_does_not_make_an_isolated_document_admissible():
    """가린 본문을 다시 넣어도 통과시키지 않는다.

    「마스커를 한 번 돌리면 들어온다」 가 되면 격리는 우회 한 줄로 끝난다.
    """
    masked = sensitive_mask.mask_sensitive(ROSTER).text

    assert "900101" not in masked, "시험 전제가 깨졌다"
    got = pii_screen.screen_document(masked, filename="계약자명단_2026.xlsx")

    assert got.blocked, "가린 사본이 격리를 통과했다"


@pytest.mark.parametrize(
    "filename",
    ["주민등록증_사본.jpg", "등기부등본_스캔.pdf", "계약자명단.png"],
)
def test_a_file_with_no_extracted_text_cannot_be_screened_or_masked(filename):
    """추출문이 없으면 **검사도 마스킹도 할 수 없다.**

    이미지에서 값을 가리려면 글자의 위치를 알아야 한다. 위치를 모르면 가린
    척밖에 못 하고, 그 「척」 이 가장 나쁘다 — 기록은 안전하다고 말하는데
    파일은 그대로다.

    수집 경로는 추출문이 `None` 이면 `screen_document` 를 아예 부르지 않고
    변환 실패 상태로 남긴다(`archive/files.py`). 이 시험은 그 전제를 고정한다.
    """
    # 파일명만으로도 고위험 문서로 분류되는지 — 추출문이 비어 있을 때.
    got = pii_screen.screen_document("", filename=filename)

    # 비어 있는 본문은 **통과도 차단도 단정하지 않는다.** 판정 근거가 없다.
    # 중요한 것은 그 상태가 「가려서 보관」 으로 넘어가지 않는 것이다.
    assert not got.blocked or got.block_code
    assert sensitive_mask.mask_sensitive("").masked is False


def test_a_partially_read_document_is_not_declared_clean():
    """일부만 읽힌 파일에서 「위험 단어가 없었다」 는 안심할 근거가 못 된다.

    못 읽은 쪽에 진짜가 있을 수 있다. 마스킹도 읽은 쪽만 가린다 — 가렸다는
    사실이 **읽지 못한 쪽까지** 보증하지 않는다.
    """
    got = pii_screen.screen_document(
        "공사 일정 요약", filename="공정표.pdf", coverage_state="partial"
    )

    assert pii_screen.TERM_PARTIAL in got.codes or not got.blocked


def test_a_single_value_in_an_ordinary_document_is_maskable():
    """경계의 반대쪽. 평범한 문서 안의 값 하나는 가려서 보관한다.

    이쪽까지 격리하면 B-86 이 생긴 이유로 돌아간다 — 한 줄 때문에 50쪽 사양서가
    근거에서 사라지고, 봇은 「자료가 없다」 고 답한다.
    """
    body = "공사 일정 요약\n담당자 연락처 010-1234-5678\n착공 2026-12-01"
    got = sensitive_mask.mask_sensitive(body)

    assert got.counts == {sensitive_mask.PHONE: 1}
    assert "공사 일정 요약" in got.text
    assert "착공 2026-12-01" in got.text


def test_the_two_boundaries_use_different_modules_on_purpose():
    """하나로 합치면 어느 쪽 규칙을 고친 것인지 알 수 없게 된다.

    `sensitive_mask` 는 값을 가리고, `pii_screen` 은 문서를 격리한다. 완화는
    앞쪽에만 일어났고 뒤쪽은 그대로다 — 그 사실이 코드 구조로 보여야 한다.
    """
    assert not hasattr(sensitive_mask, "screen_document")
    assert not hasattr(pii_screen, "mask_sensitive")
