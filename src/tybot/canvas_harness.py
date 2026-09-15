"""답변 하네싱 — 표의 **표시 형식**만 결정적으로 통일한다.

설계: [`docs/design/pii-guardrail-and-canvas-artifacts.md`](../../docs/design/pii-guardrail-and-canvas-artifacts.md) §5

## 무엇을 하고 무엇을 안 하나

| 단계 | 책임 |
|---|---|
| 마스터 LLM | 표 종류·비교 축 판단 |
| 전문 봇 | 근거에 있는 사실과 source ID |
| **이 모듈** | 날짜·기간·금액·비율의 **표시 형식** 통일 |
| validator | 새 숫자·날짜·이름 생성, 반올림, 행 누락 차단 |

**사실을 만들지 않는다.** 보기 좋은 표를 위해 추론하지 않는다 — `2026.08월말`
을 `2026-08-31` 로 바꾸면 그건 형식 통일이 아니라 **없던 날짜를 만든 것**이다.
정밀도는 그대로 두고 문법만 통일한다.

## 왜 프롬프트로 하지 않나

"표로 답하라" 는 힌트는 형식을 **맞추려고 노력하게** 할 뿐 보장하지 않는다.
같은 표 안에서 `억` 과 `백만원` 이 섞이고, `2026.8.1~2026.12.31` 과
`26.8.1 - 26.12.31` 이 나란히 놓인다. 형식은 결정적 코드의 일이다.

## 실패하면

**원문을 그대로 둔다.** 하네스가 확신할 수 없으면 손대지 않는다 — 반쯤 고친 표는
안 고친 표보다 나쁘다. 어느 값이 원문이고 어느 값이 우리가 만든 것인지 알 수 없게
되기 때문이다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

log = logging.getLogger("tybot.canvas_harness")

# 형식 규칙의 버전. **QA 기록에는 이 값만 남긴다** — 규칙 본문을 기록에 복제하면
# 기록이 커지고, 규칙이 바뀌었을 때 어느 기록이 어느 규칙이었는지 더 헷갈린다.
CANVAS_HARNESS_VERSION = 1

ValueKind = Literal["text", "date", "period", "money", "percent"]
CellStatus = Literal["exact", "converted", "unresolved"]

# 기간 구분자. **모든 행에서 같아야 한다**(§5.3).
PERIOD_SEP = " ~ "
# 자료에 없는 값. 추정하지 않는다(§5.5).
ABSENT = "-"


@dataclass(frozen=True)
class HarnessCell:
    """셀 하나. `raw` 와 `source_id` 는 **반드시 남긴다**(§5.2).

    `display` 만 Canvas 에 쓰더라도, validator 는 `display` 가 `raw` 에서 손실
    없이 만들어졌는지 검사한다. AI 가 새 값을 직접 쓰는 경로를 만들지 않는다.
    """

    field: str
    raw: str
    display: str
    value_kind: ValueKind = "text"
    source_id: str = ""
    transform: str = "identity"
    status: CellStatus = "exact"

    @property
    def needs_check(self) -> bool:
        return self.status == "unresolved"


@dataclass
class HarnessResult:
    text: str
    result: Literal["passed", "fallback", "failed"] = "passed"
    version: int = CANVAS_HARNESS_VERSION
    converted_cells: int = 0
    unresolved_cells: int = 0
    notes: list[str] = field(default_factory=list)


# --- 날짜와 기간 (§5.3) --------------------------------------------------------
#
# 보고 기준일은 **모두 같은 정밀도가 아니다.** `2026년 9월 2주차` 와 `2026년 8월 말`
# 을 임의의 일자로 바꾸면 오답이다. 문법만 통일한다.
_FULL_DATE = re.compile(
    r"^(?P<y>\d{4})\s*[./년-]\s*(?P<m>\d{1,2})\s*[./월-]\s*(?P<d>\d{1,2})\s*일?$"
)
_SHORT_DATE = re.compile(
    r"^(?P<y>\d{2})\s*[./년-]\s*(?P<m>\d{1,2})\s*[./월-]\s*(?P<d>\d{1,2})\s*일?$"
)
_MONTH_END = re.compile(r"^(?P<y>\d{4})\s*[./년-]?\s*(?P<m>\d{1,2})\s*월?\s*(말|말일|말경)$")
_MONTH_WEEK = re.compile(r"^(?P<y>\d{4})\s*[./년-]?\s*(?P<m>\d{1,2})\s*월?\s*(?P<w>\d)\s*주\s*차?$")
_MONTH_ONLY = re.compile(r"^(?P<y>\d{4})\s*[./년-]?\s*(?P<m>\d{1,2})\s*월$")


def format_date(raw: str, *, century_hint: str = "") -> tuple[str, CellStatus]:
    """날짜 한 칸. **정밀도를 올리지 않는다.**

    `century_hint` 는 주변 근거에 있던 4자리 연도다. 두 자리 연도는 그 힌트로
    같은 세기를 확정할 수 있을 때만 확장한다 — 그렇지 않으면 원문을 유지하고
    `unresolved` 로 둔다(§5.3).
    """
    text = " ".join((raw or "").split())
    if not text:
        return ABSENT, "exact"

    m = _FULL_DATE.match(text)
    if m:
        return f"{m['y']}-{int(m['m']):02d}-{int(m['d']):02d}", "converted"
    m = _MONTH_END.match(text)
    if m:
        return f"{m['y']}-{int(m['m']):02d} 말", "converted"
    m = _MONTH_WEEK.match(text)
    if m:
        return f"{m['y']}-{int(m['m']):02d} {m['w']}주차", "converted"
    m = _MONTH_ONLY.match(text)
    if m:
        return f"{m['y']}-{int(m['m']):02d}", "converted"
    m = _SHORT_DATE.match(text)
    if m:
        hint = (century_hint or "").strip()
        if len(hint) == 4 and hint.isdigit() and hint[2:] == m["y"]:
            return f"{hint}-{int(m['m']):02d}-{int(m['d']):02d}", "converted"
        # **세기를 지어내지 않는다.** `26` 이 2026 인지 1926 인지 우리는 모른다.
        return text, "unresolved"
    return text, "exact"


# ASCII `-` is also part of an ISO date.  Treat it as a range separator only
# when whitespace surrounds it; the other separators cannot be confused with
# `YYYY-MM-DD` and may be adjacent to dates.
_PERIOD_SPLIT = re.compile(
    r"\s*(?:~|–|—|부터|to)\s*|\s+-\s+|"
    r"(?<=\d)-(?=(?:\d{4}[./년-]|\d{2}[./년]))",
    re.IGNORECASE,
)


def format_period(raw: str, *, century_hint: str = "") -> tuple[str, CellStatus]:
    """기간 한 칸 → `시작 ~ 종료`.

    **한쪽이 없으면 채우지 않는다.** 시작만 적힌 공사기간에 종료일을 만들어 넣으면
    없던 약속이 생긴다.
    """
    text = " ".join((raw or "").split())
    if not text:
        return ABSENT, "exact"
    # 괄호 안 추가 조건은 기간과 분리해 둔다(§5.3).
    tail = ""
    if (bracket := re.search(r"\s*[(（].*[)）]\s*$", text)) is not None:
        tail = " " + bracket.group(0).strip()
        text = text[: bracket.start()].strip()

    parts = [p for p in _PERIOD_SPLIT.split(text) if p]
    if len(parts) != 2:
        # 한쪽만 있거나 셋 이상으로 갈리면 손대지 않는다.
        return f"{text}{tail}".strip(), "unresolved" if parts else "exact"
    start, start_state = format_date(parts[0], century_hint=century_hint)
    end, end_state = format_date(parts[1], century_hint=century_hint)
    status: CellStatus = (
        "unresolved" if "unresolved" in (start_state, end_state) else "converted"
    )
    return f"{start}{PERIOD_SEP}{end}{tail}", status


# --- 금액 (§5.4) ---------------------------------------------------------------
#
# 한 열은 **하나의 단위**를 쓴다. 셀마다 `억`·`백만원` 을 섞으면 사람이 비교할 수
# 없고, 눈으로 환산하다 자리를 틀린다.
UNIT_SCALE: dict[str, Decimal] = {
    "원": Decimal(1),
    "천원": Decimal(1_000),
    "백만원": Decimal(1_000_000),
    "억원": Decimal(100_000_000),
}
# 큰 단위부터. 「모든 값이 반올림 없이 표현되는 가장 읽기 좋은 공통 단위」 를
# 찾을 때 위에서부터 내려온다.
UNIT_ORDER = ("억원", "백만원", "천원", "원")

_MONEY = re.compile(
    r"^(?P<sign>[-−]?)\s*(?P<num>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>억\s*원?|백\s*만\s*원?|천\s*원?|만\s*원?|원)?$"
)
_UNIT_ALIAS = {
    "억": "억원", "억원": "억원",
    "백만": "백만원", "백만원": "백만원",
    "천": "천원", "천원": "천원",
    "원": "원",
}


def parse_money(raw: str) -> tuple[Decimal | None, str]:
    """(원 단위 값, 원문 단위). 못 읽으면 `(None, "")`.

    **부동소수점을 쓰지 않는다.** `Decimal` 로만 환산한다 — `0.1` 을 더하는
    순간 표에 적힌 금액이 원문과 달라진다.
    """
    text = " ".join((raw or "").split()).replace(" ", "")
    m = _MONEY.match(text)
    if not m:
        return None, ""
    unit_raw = (m["unit"] or "").replace(" ", "")
    unit = _UNIT_ALIAS.get(unit_raw, "")
    if unit_raw == "만원":
        # `만원` 은 지원 단위가 아니다. 환산은 가능하지만 **표시 단위로는 쓰지
        # 않는다**(§5.4 — 지원 단위를 넷으로 제한).
        unit = "만원"
    if not unit:
        return None, ""
    scale = UNIT_SCALE.get(unit) or (Decimal(10_000) if unit == "만원" else None)
    if scale is None:
        return None, ""
    try:
        value = Decimal(m["num"].replace(",", "")) * scale
    except InvalidOperation:
        return None, ""
    if m["sign"]:
        value = -value
    return value, unit


def _exact_in(value: Decimal, unit: str) -> bool:
    """그 단위로 **반올림 없이** 적을 수 있는가."""
    quotient = value / UNIT_SCALE[unit]
    return quotient == quotient.to_integral_value() or quotient.as_tuple().exponent >= -2


def common_unit(values: list[Decimal], *, preferred: str = "") -> str:
    """모든 값을 반올림 없이 적을 수 있는 가장 읽기 좋은 단위.

    우선순위(§5.4): 사용자가 명시한 단위 → 큰 단위부터 내려오며 정확한 첫 단위.
    **정확할 수 없으면 더 작은 단위로 내려간다.** 억지로 맞추고 반올림하지 않는다.
    """
    if not values:
        return ""
    if preferred in UNIT_SCALE and all(_exact_in(v, preferred) for v in values):
        return preferred
    for unit in UNIT_ORDER:
        if all(_exact_in(v, unit) for v in values):
            return unit
    return "원"


def format_money(value: Decimal, unit: str) -> str:
    """`Decimal` → 표시 문자열. 천 단위 구분만 넣고 **반올림하지 않는다.**"""
    quotient = value / UNIT_SCALE[unit]
    normalized = quotient.normalize()
    if normalized == normalized.to_integral_value():
        return f"{int(normalized):,}"
    return f"{normalized:,f}".rstrip("0").rstrip(".")


# --- 비율 (§5.5) ---------------------------------------------------------------
_PERCENT = re.compile(r"^(?P<num>[-−]?[\d,]+(?:\.\d+)?)\s*%$")


def format_percent(raw: str, *, decimals: int) -> tuple[str, CellStatus]:
    """비율 한 칸. **0 을 덧붙일 수는 있어도 반올림하지 않는다.**"""
    text = " ".join((raw or "").split())
    if not text:
        return ABSENT, "exact"
    m = _PERCENT.match(text)
    if not m:
        return text, "exact"
    try:
        value = Decimal(m["num"].replace(",", "").replace("−", "-"))
    except InvalidOperation:
        return text, "unresolved"
    current = -value.as_tuple().exponent
    if current > decimals:
        # 원문이 더 정밀하다. **깎지 않는다** — 열 자릿수는 가장 정밀한 원문에
        # 맞춘다(§5.5).
        return f"{value}%", "exact"
    return f"{value:.{decimals}f}%", "converted" if current != decimals else "exact"


def percent_decimals(values: list[str]) -> int:
    """열의 소수 자릿수 = **가장 정밀한 원문**의 자릿수."""
    best = 0
    for raw in values:
        m = _PERCENT.match(" ".join((raw or "").split()))
        if not m:
            continue
        try:
            best = max(best, -Decimal(m["num"].replace(",", "")).as_tuple().exponent)
        except InvalidOperation:
            continue
    return best


# --- 역검증 (§5.2) -------------------------------------------------------------
_DIGITS = re.compile(r"\d")
_NUMBER = re.compile(r"\d+")


def verify(cell: HarnessCell) -> str:
    """`display` 가 `raw` 에서 손실 없이 나왔는지. 문제가 없으면 빈 문자열.

    **AI 가 새 값을 직접 쓰는 경로를 만들지 않는다.** 여기서 막는 것은 셋이다 —
    없던 숫자, 사라진 숫자, 그리고 정밀도가 올라간 날짜.
    """
    if cell.status == "unresolved":
        return ""
    if cell.display == ABSENT:
        return "" if not _DIGITS.search(cell.raw) else "자료가 있는 셀을 빈 칸으로 바꿨습니다"

    if cell.value_kind == "money":
        value, unit = parse_money(cell.raw)
        if value is None or not cell.transform.startswith("money:"):
            return ""
        target = cell.transform.partition(":")[2]
        if target not in UNIT_SCALE:
            return f"알 수 없는 단위 {target}"
        try:
            back = Decimal(cell.display.replace(",", "")) * UNIT_SCALE[target]
        except InvalidOperation:
            return "환산 결과를 되돌릴 수 없습니다"
        # **역변환이 원문과 같아야 한다.** 다르면 어딘가에서 반올림된 것이다.
        return "" if back == value else f"환산이 원문과 다릅니다({unit}→{target})"

    if cell.value_kind in ("date", "period"):
        # **숫자 토큰의 개수와 값**을 본다. 자릿수로 세면 `9` → `09` 같은 정상
        # 자리맞춤이 위반으로 잡히고, `2026.08월말` → `2026-08-31` 같은 진짜
        # 위반은 자릿수 차이가 작아서 빠져나간다.
        raw_numbers = [int(n) for n in _NUMBER.findall(cell.raw)]
        shown_numbers = [int(n) for n in _NUMBER.findall(cell.display)]
        if len(shown_numbers) != len(raw_numbers):
            return "날짜 값의 개수가 원문과 다릅니다"
        allow_century = cell.transform.endswith(":century-expanded")
        for raw_number, shown_number in zip(raw_numbers, shown_numbers, strict=True):
            if raw_number == shown_number:
                continue
            if allow_century and raw_number < 100 and shown_number % 100 == raw_number:
                continue
            return "원문에 없던 날짜 값이 들어갔습니다"
        return ""

    if cell.value_kind == "percent":
        raw_num = "".join(_DIGITS.findall(cell.raw))
        shown_num = "".join(_DIGITS.findall(cell.display))
        return "" if shown_num.startswith(raw_num) or raw_num.startswith(shown_num) else (
            "비율이 원문과 다릅니다"
        )
    return ""


# --- 표 단위 적용 --------------------------------------------------------------
#
# 여기가 「프롬프트로 맞췄다고 치지 않는다」 는 §5.7 의 실제 구현이다. 전문 봇
# 출력의 표를 읽어 **열 단위로** 형식을 통일하고, 하나라도 역검증에 걸리면
# **그 표를 통째로 원문으로 되돌린다.**
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_YEAR4 = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _cells(line: str) -> list[str]:
    marker = "\x00TYBOT_PIPE\x00"
    protected = line.strip().replace(r"\|", marker).strip("|")
    return [c.strip().replace(marker, r"\|") for c in protected.split("|")]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(_SEPARATOR.fullmatch(c) for c in cells)


def _kind_of(values: list[str]) -> ValueKind:
    """열 종류. **과반이 같은 모양일 때만** 그 종류다.

    섞여 있으면 `text` 로 둔다 — 반만 맞는 판정으로 절반만 고치면 같은 열에서
    형식이 두 가지가 되고, 그건 통일 전보다 읽기 어렵다.
    """
    filled = [v for v in values if v.strip()]
    if not filled:
        return "text"

    def share(pred) -> float:
        return sum(1 for v in filled if pred(v)) / len(filled)

    if share(lambda v: _PERCENT.match(v.strip())) > 0.5:
        return "percent"
    if share(lambda v: parse_money(v)[0] is not None) > 0.5:
        return "money"
    if share(lambda v: len(_PERIOD_SPLIT.split(v.strip())) == 2
             and format_date(_PERIOD_SPLIT.split(v.strip())[0])[1] != "exact") > 0.5:
        return "period"
    if share(lambda v: format_date(v)[1] != "exact") > 0.5:
        return "date"
    return "text"


def _format_column(
    header: str, values: list[str], *, century_hint: str, target_unit: str
) -> tuple[str, list[HarnessCell]]:
    """(새 머리글, 셀들). 형식을 못 정하면 원문 그대로 돌려준다."""
    kind = _kind_of(values)
    cells: list[HarnessCell] = []

    if kind == "money":
        parsed = [parse_money(v) for v in values]
        amounts = [v for v, _ in parsed if v is not None]
        unit = common_unit(amounts, preferred=target_unit)
        if not unit:
            return header, [HarnessCell(header, v, v) for v in values]
        for raw, (value, _) in zip(values, parsed, strict=True):
            if value is None:
                cells.append(HarnessCell(header, raw, raw or ABSENT, "money"))
                continue
            cells.append(HarnessCell(
                header, raw, format_money(value, unit), "money",
                transform=f"money:{unit}", status="converted",
            ))
        # 단위는 **머리글에 한 번만**. 셀마다 붙이면 눈이 매 행 단위를 다시 읽는다.
        new_header = header if f"({unit})" in header else f"{header} ({unit})"
        return new_header, cells

    if kind == "percent":
        decimals = percent_decimals(values)
        for raw in values:
            display, status = format_percent(raw, decimals=decimals)
            cells.append(HarnessCell(header, raw, display, "percent", status=status))
        return header, cells

    if kind in ("date", "period"):
        fmt = format_period if kind == "period" else format_date
        for raw in values:
            display, status = fmt(raw, century_hint=century_hint)
            expanded = bool(
                century_hint
                and status == "converted"
                and re.search(r"(?<!\d)\d{2}[./년-]", raw)
                and century_hint in display
            )
            transform = f"{kind}:century-expanded" if expanded else f"{kind}:format"
            cells.append(HarnessCell(
                header, raw, display, kind, transform=transform, status=status
            ))
        return header, cells

    return header, [HarnessCell(header, v, v or ABSENT) for v in values]


def apply_table(lines: list[str], *, century_hint: str = "", target_unit: str = "") -> tuple[list[str], int, int, str]:
    """표 한 덩어리 → (새 줄들, 바꾼 셀 수, 확인 필요 수, 실패 사유).

    실패 사유가 비어 있지 않으면 호출자는 **원문을 그대로 쓴다.**
    """
    header_cells = _cells(lines[0])
    separator = lines[1]
    rows = [_cells(ln) for ln in lines[2:]]
    width = len(header_cells)
    if any(len(r) != width for r in rows):
        # 열 개수가 다른 행이 있으면 어느 값이 어느 열인지 알 수 없다. 손대지 않는다.
        return lines, 0, 0, "행마다 열 개수가 다릅니다"

    new_headers: list[str] = []
    columns: list[list[HarnessCell]] = []
    for index in range(width):
        values = [r[index] for r in rows]
        head, cells = _format_column(
            header_cells[index], values,
            century_hint=century_hint, target_unit=target_unit,
        )
        new_headers.append(head)
        columns.append(cells)

    for column in columns:
        for cell in column:
            if problem := verify(cell):
                return lines, 0, 0, problem

    converted = sum(1 for col in columns for c in col if c.status == "converted")
    unresolved = sum(1 for col in columns for c in col if c.status == "unresolved")
    out = ["| " + " | ".join(new_headers) + " |", separator]
    for row_index in range(len(rows)):
        out.append("| " + " | ".join(col[row_index].display for col in columns) + " |")
    return out, converted, unresolved, ""


def apply(body: str, *, target_unit: str = "") -> HarnessResult:
    """답변 본문의 모든 표에 형식 규칙을 적용한다.

    한 표가 실패해도 **그 표만** 원문으로 두고 나머지는 적용한다. 전부 되돌리면
    형식이 맞던 표까지 잃는다.
    """
    lines = (body or "").splitlines()
    # 두 자리 연도를 확장할 때 쓸 힌트. 본문 전체에서 가장 흔한 4자리 연도다.
    years = _YEAR4.findall(body or "")
    hint = max(set(years), key=years.count) if years else ""

    out: list[str] = []
    converted = unresolved = 0
    notes: list[str] = []
    index = 0
    while index < len(lines):
        if (
            index + 2 < len(lines)
            and "|" in lines[index]
            and _is_separator(lines[index + 1])
        ):
            end = index + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            block = lines[index:end]
            if len(block) > 2:
                new_block, got, left, problem = apply_table(
                    block, century_hint=hint, target_unit=target_unit
                )
                if problem:
                    notes.append(problem)
                out.extend(new_block)
                converted += got
                unresolved += left
                index = end
                continue
        out.append(lines[index])
        index += 1

    text = "\n".join(out)
    if unresolved:
        # **모르는 것은 모른다고 적는다.** 확인 필요 표시가 없으면 사람은 그
        # 값을 확정된 것으로 읽는다.
        text = f"{text}\n\n_일부 값은 원문 정밀도를 유지했습니다(확인 필요 {unresolved}건)._"
    if converted and target_unit:
        text = f"{text}\n\n_금액은 표시 단위로 환산했으며 반올림하지 않았습니다._"
    return HarnessResult(
        text=text,
        result="fallback" if notes else "passed",
        converted_cells=converted,
        unresolved_cells=unresolved,
        notes=notes,
    )


__all__ = [
    "ABSENT",
    "CANVAS_HARNESS_VERSION",
    "PERIOD_SEP",
    "UNIT_ORDER",
    "UNIT_SCALE",
    "CellStatus",
    "HarnessCell",
    "HarnessResult",
    "ValueKind",
    "apply",
    "apply_table",
    "common_unit",
    "format_date",
    "format_money",
    "format_percent",
    "format_period",
    "parse_money",
    "percent_decimals",
    "verify",
]
