"""PII 판정 fixture — **점수를 고정한다**.

설계: `docs/design/pii-guardrail-and-canvas-artifacts.md` §2, §7.1

오탐이 나왔다고 임계값만 낮추면 실제 등기부등본이 통과한다. 그건 고친 게 아니라
끈 것이다. 그래서 「통과해야 하는 것」 과 「막혀야 하는 것」 을 **같은 파일에**
둔다 — 한쪽만 보고 숫자를 만지면 다른 쪽이 즉시 깨진다.

여기 fixture 는 전부 **합성 비식별 값**이다. 실제 등기부등본·명단은 저장소에
넣지 않는다(원칙 5).
"""
from __future__ import annotations

import pytest

from tybot.pii_screen import (
    DOC_REGISTRY,
    DOC_ROSTER,
    RRN,
    TERM_MENTIONED,
    TERM_PARTIAL,
    direct_identifiers,
    screen_document,
    screen_line,
)

# 실제 등기부등본의 양식 표식만 남긴 합성본. 소유자 이름·주소는 넣지 않았다.
REGISTRY_FIXTURE = """등기사항전부증명서(말소사항 포함) - 건물
고유번호 1234-5678-901234
[표제부] (건물의 표시)
표시번호  접수  소재지번 및 건물번호  건물내역  등기원인 및 기타사항
1  2019년3월4일  경상남도 김해시 외동  철근콘크리트조
[갑구] (소유권에 관한 사항)
순위번호  등기목적  접수  등기원인  권리자 및 기타사항
1  소유권보존  2019년3월4일  -  소유자 주식회사 갑
2  소유권이전  2021년7월9일  매매  소유자 주식회사 을
[을구] (소유권 이외의 권리에 관한 사항)
순위번호  등기목적  접수  등기원인  권리자 및 기타사항
1  근저당권설정  2021년7월9일  설정계약  채권최고액 금 1,200,000,000원
열람일시 : 2026년09월15일 10시12분
"""

# 실제 명단의 열 구조만 남긴 합성본.
ROSTER_FIXTURE = """계약자 명단
번호  성명  생년월일  연락처  동 호수  자택 주소
1  김**  1978-04-11  010-****-1234  101동 1502호  경남 김해시
2  이**  1985-11-02  010-****-5678  102동 903호  부산 강서구
3  박**  1990-02-20  010-****-9012  103동 401호  경남 김해시
"""


def test_schedule_mentioning_a_registry_is_collected():
    """§7.1-1 · 「등기부등본 제출 일정」 한 문장짜리 표 이미지는 통과한다."""
    result = screen_document(
        "9월 인허가 일정\n9/22 등기부등본 제출\n9/25 착공계 제출",
        filename="9월_인허가일정.png",
        coverage_state="succeeded",
    )
    assert result.blocked is False
    assert TERM_MENTIONED in result.codes
    assert result.state == "passed_with_notice"


def test_general_report_mentioning_the_term_is_collected():
    """§7.1-2 · 일반 공지·보고서가 용어를 써도 통과한다."""
    result = screen_document(
        "주간 업무 보고\n- 등기부등본 발급 비용은 예산에 반영함\n- 착공 준비 완료",
        filename="주간보고_2026-09.docx",
    )
    assert result.blocked is False


def test_resident_registration_number_blocks_the_whole_file():
    """§7.1-3 · 직접 식별자는 계속 파일 전체 차단."""
    result = screen_document(
        "출입자 명부\n홍길동 900101-1234567 방문", filename="출입자.xlsx"
    )
    assert result.blocked is True
    assert result.block_code == RRN


def test_real_registry_fixture_is_blocked():
    """§7.1-4 · 파일명과 여러 고유 필드가 일치하면 차단."""
    result = screen_document(REGISTRY_FIXTURE, filename="등기부등본_김해외동.pdf")
    assert result.blocked is True
    assert result.block_code == DOC_REGISTRY


def test_registry_fixture_is_blocked_even_without_a_telling_filename():
    """스캔 파일명이 `scan001.pdf` 여도 양식 표식만으로 차단된다.

    파일명에만 기대면 이름만 바꿔 올리는 순간 통과한다.
    """
    result = screen_document(REGISTRY_FIXTURE, filename="scan001.pdf")
    assert result.blocked is True
    assert result.block_code == DOC_REGISTRY


def test_term_only_and_real_roster_are_told_apart():
    """§7.1-5 · 용어만 있는 문장과 실제 명단을 구분한다."""
    mention = screen_document(
        "다음 주까지 계약자 명단 취합 예정입니다", filename="회의록.md"
    )
    assert mention.blocked is False
    assert TERM_MENTIONED in mention.codes

    roster = screen_document(ROSTER_FIXTURE, filename="계약자명단_1차.xlsx")
    assert roster.blocked is True
    assert roster.block_code == DOC_ROSTER


def test_partial_ocr_with_composite_signal_is_blocked():
    """§7.1-6 · 부분 OCR + 고위험 복합 신호는 차단."""
    result = screen_document(
        REGISTRY_FIXTURE, filename="scan001.pdf", coverage_state="partial"
    )
    assert result.blocked is True


def test_partial_ocr_with_only_a_term_passes_with_a_marker():
    """부분 OCR + 용어만이면 통과하되 **못 읽은 쪽이 있다**고 표시한다(§2.3)."""
    result = screen_document(
        "9/22 등기부등본 제출", filename="일정.pdf", coverage_state="partial"
    )
    assert result.blocked is False
    assert TERM_PARTIAL in result.codes


def test_findings_never_carry_body_text():
    """§7.1-8 · 판정 결과에 원문·번호가 실리지 않는다."""
    result = screen_document(
        "홍길동 900101-1234567", filename="x.txt"
    )
    blob = " ".join(f"{f.code} {f.label}" for f in result.findings)
    assert "900101" not in blob
    assert "홍길동" not in blob


@pytest.mark.parametrize(
    "text",
    [
        "송장번호 123456-1234567",   # 월 34는 날짜가 아니다
        "계좌 110208-2876543",       # 11-02-08 은 날짜 모양이다 → 아래에서 별도 확인
        "코드 001399-1234567",       # 월 13
    ],
)
def test_non_date_shaped_numbers_are_not_all_treated_as_rrn(text):
    """생년월일 자리가 날짜가 아니면 주민등록번호로 세지 않는다.

    일련번호까지 막으면 회계 문서가 통째로 사라진다.
    """
    if text.startswith("계좌"):
        # 11-02-08 은 실제로 날짜 모양이다. **모양이 같으면 막는 쪽이 기본값**(원칙 3).
        assert direct_identifiers(text) == 1
        return
    assert direct_identifiers(text) == 0


def test_screen_line_only_blocks_direct_identifiers():
    """사람이 한 말은 단어 하나로 버리지 않는다(§2.4)."""
    assert screen_line("등기부등본 제출 예정입니다") is None
    assert screen_line("계약자 명단 취합 중") is None
    assert screen_line("주민등록번호 900101-1234567 입니다") == "주민등록번호 형식"
