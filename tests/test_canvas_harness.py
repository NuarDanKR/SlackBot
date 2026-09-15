"""답변 하네싱 — 형식은 통일하되 **사실은 만들지 않는다**.

설계: `docs/design/pii-guardrail-and-canvas-artifacts.md` §5, §7.3
"""
from __future__ import annotations

from decimal import Decimal

from tybot.canvas_harness import (
    CANVAS_HARNESS_VERSION,
    HarnessCell,
    apply,
    common_unit,
    format_date,
    format_money,
    format_percent,
    format_period,
    parse_money,
    percent_decimals,
    verify,
)


# --- §7.3-1,2,3 날짜 -----------------------------------------------------------
def test_month_end_and_week_keep_their_precision():
    """§7.3-1 · 문법은 통일되지만 **일자를 만들지 않는다.**"""
    assert format_date("2026.08월말") == ("2026-08 말", "converted")
    assert format_date("2026년 9월 2주차") == ("2026-09 2주차", "converted")
    # 없던 일자가 생기면 그건 형식 통일이 아니다.
    for text, _ in (format_date("2026.08월말"), format_date("2026년 9월 2주차")):
        assert "-31" not in text and "-15" not in text


def test_explicit_dates_become_iso():
    """§7.3-2 · 명시적인 4자리 일자는 모두 `YYYY-MM-DD`."""
    assert format_date("2026.9.2")[0] == "2026-09-02"
    assert format_date("2026년 9월 2일")[0] == "2026-09-02"
    assert format_date("2026-09-02")[0] == "2026-09-02"


def test_two_digit_year_is_not_expanded_without_a_hint():
    """§7.3-3 · 세기를 확정할 수 없으면 원문을 유지한다."""
    assert format_date("26.8.1") == ("26.8.1", "unresolved")
    # 주변에 4자리 연도가 있으면 같은 세기로만 확장한다.
    assert format_date("26.8.1", century_hint="2026")[0] == "2026-08-01"
    assert format_date("26.8.1", century_hint="2019") == ("26.8.1", "unresolved")


# --- §7.3-4 기간 ---------------------------------------------------------------
def test_period_separator_is_unified_and_missing_side_is_not_filled():
    assert format_period("2026.3.1-2026.12.31")[0] == "2026-03-01 ~ 2026-12-31"
    assert format_period("2026.3.1 ~ 2026.12.31")[0] == "2026-03-01 ~ 2026-12-31"
    # 한쪽만 있으면 채우지 않는다.
    display, status = format_period("2026.3.1 착공")
    assert "~" not in display and status in ("exact", "unresolved")


def test_period_keeps_extra_condition_outside_the_range():
    display, _ = format_period("2026.3.1-2026.12.31 (우기 제외)")
    assert display == "2026-03-01 ~ 2026-12-31 (우기 제외)"


def test_iso_dates_do_not_split_on_their_own_hyphens():
    """ISO 날짜의 하이픈은 기간 구분자가 아니다."""
    assert format_period("2026-03-01 ~ 2026-12-31")[0] == (
        "2026-03-01 ~ 2026-12-31"
    )


# --- §7.3-5,6 금액 -------------------------------------------------------------
def test_mixed_units_become_one_exact_unit():
    """§7.3-5 · `억원` 과 `백만원` 이 섞인 열을 반올림 없이 한 단위로."""
    values = [parse_money("12억원")[0], parse_money("340백만원")[0]]
    unit = common_unit(values)
    assert unit == "억원"
    assert format_money(values[0], unit) == "12"
    assert format_money(values[1], unit) == "3.4"


def test_unit_drops_down_rather_than_rounding():
    """§7.3-6 · 정확한 공통 단위가 없으면 더 작은 단위로 내려간다."""
    values = [parse_money("12억원")[0], parse_money("1,234,567원")[0]]
    assert common_unit(values) == "원"


def test_money_conversion_survives_reverse_verification():
    value, _ = parse_money("340백만원")
    cell = HarnessCell(
        "도급액", "340백만원", format_money(value, "억원"), "money",
        transform="money:억원", status="converted",
    )
    assert verify(cell) == ""
    # 반올림한 값은 역변환에서 잡힌다.
    broken = HarnessCell(
        "도급액", "340백만원", "3", "money", transform="money:억원", status="converted"
    )
    assert verify(broken)


# --- §7.3-8 비율 ---------------------------------------------------------------
def test_percent_pads_zeros_but_never_rounds():
    assert percent_decimals(["12.5%", "7%", "3.25%"]) == 2
    assert format_percent("7%", decimals=2)[0] == "7.00%"
    assert format_percent("12.5%", decimals=2)[0] == "12.50%"
    # 원문이 더 정밀하면 깎지 않는다.
    assert format_percent("3.256%", decimals=2)[0] == "3.256%"


# --- §7.3-9 validator ----------------------------------------------------------
def test_validator_rejects_a_date_that_gained_precision():
    cell = HarnessCell("기준일", "2026.08월말", "2026-08-31", "date", status="converted")
    assert verify(cell)


def test_validator_rejects_dropping_or_reordering_date_parts():
    dropped = HarnessCell(
        "기준일", "2026-08-31", "2026-08", "date", status="converted"
    )
    reordered = HarnessCell(
        "기준일", "2026-08-31", "2026-31-08", "date", status="converted"
    )
    assert verify(dropped)
    assert verify(reordered)


def test_validator_rejects_emptying_a_cell_that_had_data():
    cell = HarnessCell("금액", "1,200원", "-", "money", status="converted")
    assert verify(cell)


# --- 표 단위 ------------------------------------------------------------------
TABLE = """| 현장 | 보고 기준일 | 공사기간 | 도급액 |
| --- | --- | --- | --- |
| 김해외동 | 2026.08월말 | 2026.3.1-2026.12.31 | 12억원 |
| 부산명지 | 2026년 9월 2주차 | 2026.5.1 ~ 2026.11.30 | 340백만원 |
"""


def test_apply_unifies_a_whole_table():
    got = apply(TABLE)
    assert got.result == "passed"
    assert got.version == CANVAS_HARNESS_VERSION
    assert "2026-08 말" in got.text
    assert "2026-09 2주차" in got.text
    assert "2026-03-01 ~ 2026-12-31" in got.text
    assert "2026-05-01 ~ 2026-11-30" in got.text
    # 단위는 머리글에 한 번, 셀에는 붙이지 않는다.
    assert "도급액 (억원)" in got.text
    assert "12억원" not in got.text and "340백만원" not in got.text
    assert got.converted_cells >= 6


def test_apply_leaves_prose_untouched():
    body = "결론부터 말하면 착공은 3월입니다.\n\n" + TABLE
    got = apply(body)
    assert got.text.startswith("결론부터 말하면 착공은 3월입니다.")


def test_apply_returns_the_original_table_when_columns_do_not_line_up():
    """열 개수가 어긋나면 손대지 않는다 — 반쯤 고친 표가 더 나쁘다."""
    broken = "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    got = apply(broken)
    assert got.result == "fallback"
    assert "| 1 | 2 | 3 |" in got.text


def test_unresolved_values_are_marked_for_a_human():
    body = "| 현장 | 기준일 |\n| --- | --- |\n| 가 | 26.8.1 |\n| 나 | 27.9.2 |\n"
    got = apply(body)
    assert got.unresolved_cells == 2
    assert "확인 필요" in got.text
    # 원문이 그대로 남아야 사람이 확인하러 갈 수 있다.
    assert "26.8.1" in got.text


def test_two_digit_year_conversion_survives_reverse_verification():
    body = (
        "| 현장 | 기준일 |\n| --- | --- |\n"
        "| 가 | 2026.8.1 |\n| 나 | 26.9.2 |\n"
    )
    got = apply(body)
    assert got.result == "passed"
    assert "2026-09-02" in got.text


def test_target_unit_is_honoured_when_it_is_exact():
    body = "| 항목 | 금액 |\n| --- | --- |\n| 계약 | 12억원 |\n| 실행 | 3억원 |\n"
    got = apply(body, target_unit="백만원")
    assert "금액 (백만원)" in got.text
    assert "1,200" in got.text
    assert "반올림하지 않았습니다" in got.text


def test_money_math_uses_decimal_not_float():
    """0.1 을 더하는 순간 표의 금액이 원문과 달라진다."""
    value, _ = parse_money("0.1억원")
    assert value == Decimal("10000000")
    assert format_money(value, "백만원") == "10"


def test_long_prose_is_never_forced_into_a_table():
    """§7.2-9 · 하네스는 **표를 만들지 않는다.** 형식만 통일한다.

    긴 설명을 표 한 칸에 밀어 넣으면 읽을 수 없는 문서가 된다. 표로 쓸지는
    마스터 판정의 몫이고, 여기서 뒤집지 않는다.
    """
    prose = (
        "결론부터 말하면 착공은 3월입니다.\n"
        "감리 계약이 2월에 끝나야 하고, 그 전에 인허가 보완이 남아 있습니다.\n"
        "보완 항목은 구조 검토서와 교통 영향 평가입니다.\n"
    )
    got = apply(prose)
    assert got.text.strip() == prose.strip()
    assert "|" not in got.text
    assert got.converted_cells == 0
