"""민감정보 결정적 마스킹 (B-86).

## 오너 결정 (2026-11-10)

전에는 「PII 가 있으면 **파일 전체를 아카이브 금지**」 였다. 그래서 한 줄 때문에
문서 하나가 통째로 근거에서 사라졌고, 봇은 「자료가 없다」 고 답했다. 사람은
그것을 봇의 고장으로 읽었다.

이제 **수집 시 마스킹을 먼저 적용하고 가린 뒤에 보관한다.** 완화가 아니다 —
평문 민감정보는 파일·DB·로그·감사기록 **어디에도** 쓰지 않는다는 조건이 먼저다.
마스킹할 수 없는 것(바이너리·이미지·불확실 문서)은 기존처럼 격리한다.

## 되돌릴 수 없다

마스크는 **값을 지운다.** 같은 값에 같은 결과를 주지만 되돌리는 열쇠는 만들지도
저장하지도 않는다.

짧은 해시 토큰(`[전화번호#a1b2]`)을 쓰지 않는 이유가 여기 있다. 전화번호의
경우의 수는 1억 남짓이라 **해시를 통째로 돌려보면 몇 분 만에 역산된다.**
소금을 쳐도 소금이 같은 저장소에 있으면 같은 이야기다. 가린 값을 다시 찾을 수
있으면 가린 것이 아니다.

## 대신 문서 안에서만 구분한다

전부 `[이름]` 으로 바꾸면 「두 사람」 인지 「한 사람이 두 번」 인지 사라진다.
그래서 **그 문서 안에서 처음 나온 순서**로 번호를 붙인다 — `[이름1]`, `[이름2]`.

번호는 값에 대해 아무것도 말하지 않는다. 같은 사람이 다른 문서에서는 다른
번호를 받고, 그것이 맞다 — 문서를 가로질러 사람을 이을 수 있으면 그게 곧
식별이다.

## 멱등하다

마스킹된 글을 다시 마스킹해도 그대로다. 본문은 수집 → 변환 → 답변 → 전달까지
여러 겹을 지나고, 겹마다 한 번씩 가리면 `[[이름1]2]` 같은 것이 생긴다.

## 이름은 **표시된 자리**에서만 가린다

한국어 이름을 자유 문장에서 정규식으로 찾을 수는 없다. 두세 글자 한글 낱말은
대부분 이름이 아니고, 그것을 가리면 문장이 `[이름1]` 으로 뒤덮인다. 근거가
사라지는 것은 차단과 같다.

그래서 `성명: 홍길동` · `계약자 | 홍길동` 처럼 **항목 이름이 붙은 자리**만
가린다. 명단·계약서가 바로 그 모양이고, 그 밖의 이름은 `pii_screen` 의 문서
분류가 계속 격리한다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

#: 정책 판 번호. 아카이브 프론트매터에 적히고, 규칙이 바뀌면 올린다.
#:
#: 무엇으로 가렸는지 모르면 다시 가릴 수도, 예전 결과를 설명할 수도 없다.
POLICY_REVISION = "mask-v1"

# --- 유형 ---------------------------------------------------------------------
#
# 코드는 **비민감 값**이다. 로그·감사·콘솔에 이 코드와 횟수만 나간다.

RRN = "rrn"
FOREIGN_ID = "foreign-id"
CARD = "card"
ACCOUNT = "account"
PHONE = "phone"
EMAIL = "email"
ADDRESS = "address"
NAME = "name"

TYPES = (RRN, FOREIGN_ID, CARD, ACCOUNT, PHONE, EMAIL, ADDRESS, NAME)

LABELS = {
    RRN: "주민등록번호",
    FOREIGN_ID: "외국인등록번호",
    CARD: "카드번호",
    ACCOUNT: "계좌번호",
    PHONE: "전화번호",
    EMAIL: "이메일",
    ADDRESS: "주소",
    NAME: "이름",
}

#: 이미 가려진 자리. 멱등성은 이 모양을 **먼저** 집어내서 지킨다.
#: 번호 자리를 `\d*` 로 열어 두면 글을 쓰는 사람이 `[이름99999]` 를 적어 다음
#: 번호를 밀어낼 수 있다. 우리가 내보내는 모양만 받는다.
MASK_RE = re.compile(
    r"\[(?:" + "|".join(re.escape(label) for label in LABELS.values())
    + r")(?:[1-9][0-9]{0,3})?\]"
)

#: 자리 표시에 쓰는 글자. 입력에서 **먼저 걷어낸다**.
SENTINEL = "\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


@dataclass(frozen=True)
class MaskResult:
    """가린 결과. **원문 조각을 하나도 담지 않는다.**

    `counts` 는 유형별 **횟수**이고, `sample` 같은 칸은 일부러 없다. 진단을
    돕겠다고 가린 값의 앞 네 글자를 남기면, 그 네 글자가 로그에 쌓인다.
    """

    text: str
    counts: dict[str, int] = field(default_factory=dict)
    revision: str = POLICY_REVISION

    @property
    def masked(self) -> bool:
        return bool(self.counts)

    @property
    def types(self) -> tuple[str, ...]:
        """찾은 유형. 고정 순서라 프론트매터가 실행마다 달라지지 않는다."""
        return tuple(code for code in TYPES if self.counts.get(code))

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# --- 탐지 ---------------------------------------------------------------------
#
# 전부 결정적이다. **LLM 에게 묻지 않는다** — 같은 입력이 어제와 오늘 다르게
# 가려지면 그 차이를 아무도 설명할 수 없다(`pii_screen` 과 같은 축).

#: 주민등록번호·외국인등록번호. 뒷자리 첫 숫자로 가른다.
#: 내국인 1~4(+5·6 은 1900년대 외국인), 외국인 5~8.
#:
#: **구분자가 없어도 잡는다.** 시스템에서 내려받은 자료는 `9001011234567` 처럼
#: 붙여 쓰는 일이 흔하고, 하이픈을 필수로 두면 그 형태가 통째로 평문으로
#: 남는다. 더 나쁘게는 계좌 문맥에서 `[계좌번호]` 로 가려져 **유형이 틀린 채로**
#: 기록된다 — 가린 것처럼 보이지만 감사기록이 거짓말을 한다.
#:
#: 13자리를 전부 주민번호로 보지는 않는다. 월·일 자리의 타당성과 뒷자리 첫
#: 숫자(1~8)가 함께 걸러, 임의의 13자리 숫자가 걸릴 확률은 3% 남짓이다. 그
#: 오탐으로 일련번호 하나가 가려지는 것보다 주민번호 하나가 남는 쪽이 훨씬 나쁘다.
_RRN_RE = re.compile(r"(?<![0-9])(\d{2})(\d{2})(\d{2})\s*[-–—]?\s*([1-8])\d{6}(?![0-9])")

#: 카드번호. 자릿수만 보면 일련번호까지 먹으므로 **Luhn 검사**를 통과해야 한다.
_CARD_RE = re.compile(r"(?<![0-9])(?:\d[ -]?){12,18}\d(?![0-9])")

#: 계좌번호. 형식이 은행마다 달라 **문맥 낱말이 가까이 있을 때만** 가린다.
#: 그냥 숫자 묶음을 다 가리면 금액·수량·문서번호가 사라진다.
_ACCOUNT_CONTEXT = r"(?:계좌|예금주|입금|송금|이체|account)"
_ACCOUNT_RE = re.compile(
    rf"{_ACCOUNT_CONTEXT}[^\n0-9]{{0,20}}((?<![0-9-])\d[\d-]{{8,18}}\d(?![0-9-]))",
    re.IGNORECASE,
)

#: 휴대전화·유선전화·국제표기.
_PHONE_RE = re.compile(
    r"(?<![0-9])(?:\+?82[-\s]?1[0-9]|01[0-9]|0[2-6][0-9]?)"
    r"[-\s.]?\d{3,4}[-\s.]?\d{4}(?![0-9])"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: 주소. **번지까지 있을 때만** 가린다 — 「서울 사무소」 같은 말은 주소가 아니다.
_ADDRESS_RE = re.compile(
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|"
    r"경북|경남|제주)(?:특별시|광역시|특별자치시|특별자치도|도)?"
    r"[^\n]{0,30}?"
    r"(?:[가-힣A-Za-z0-9]+(?:읍|면|동|리|로|길))"
    r"\s*\d+(?:-\d+)?(?:번지|호)?"
)

#: 항목 이름이 붙은 사람 이름. 자유 문장에서는 찾지 않는다(모듈 설명 참조).
_NAME_RE = re.compile(
    r"(?P<head>(?:성명|이름|계약자|수급인|도급인|대표자|담당자|피보험자|예금주)"
    r"\s*[:：|]\s*)"
    r"(?P<value>[가-힣]{2,4})(?![가-힣])"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_rrn(match: re.Match[str]) -> bool:
    """생년월일 자리가 말이 되는가. 견본 번호로 회계 일련번호까지 막지 않는다."""
    month = int(match.group(2))
    day = int(match.group(3))
    return 1 <= month <= 12 and 1 <= day <= 31


def _rrn_type(match: re.Match[str]) -> str:
    return FOREIGN_ID if match.group(4) in "5678" else RRN


# --- 마스킹 -------------------------------------------------------------------


class _Masker:
    """한 번의 마스킹. 문서 안에서만 쓰는 번호를 들고 있다."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._seen: dict[tuple[str, str], int] = {}
        self._next: Counter[str] = Counter()
        self._kept: list[str] = []

    def token(self, code: str, raw: str) -> str:
        """`[이름1]`. 같은 값은 같은 번호, 다른 값은 다른 번호.

        **번호는 값에 대해 아무것도 말하지 않는다.** 처음 나온 순서일 뿐이다.
        """
        key = (code, _normalize(raw))
        number = self._seen.get(key)
        if number is None:
            self._next[code] += 1
            number = self._next[code]
            self._seen[key] = number
        self.counts[code] += 1
        return f"[{LABELS[code]}{number}]"

    def park(self, text: str) -> str:
        """이미 가려진 자리를 잠시 빼 두고, **그 번호를 예약한다.**

        빼 두기만 하면 뒤에 나온 평문이 1번부터 받는다. 그러면
        `계약자: [이름1] 담당자: 김철수` 가 `담당자: [이름1]` 이 되고, **다른 두
        사람이 한 사람으로 적힌다.** 가린 글이 원문보다 더 틀린 말을 하는 셈이다.

        이미 쓰인 값이 무엇인지는 알 수 없으니 합칠 수는 없다. 할 수 있는 것은
        **겹치지 않게 하는 것**이고, 그쪽이 안전한 방향이다 — 한 사람이 두
        번호로 보이는 것은 읽는 사람이 알아차리지만, 두 사람이 한 번호로 보이는
        것은 알아차릴 길이 없다.
        """
        def swap(match: re.Match[str]) -> str:
            token = match.group(0)
            used = re.search(r"(\d+)\]$", token)
            if used:
                label = token[1 : used.start(1)]
                for code, name in LABELS.items():
                    if name == label:
                        self._next[code] = max(self._next[code], int(used.group(1)))
                        break
            self._kept.append(token)
            return f"\x00{len(self._kept) - 1}\x00"

        return MASK_RE.sub(swap, text)

    def restore(self, text: str) -> str:
        """자리 표시를 되돌린다. **범위를 벗어나면 그대로 둔다.**

        입력에서 자리 표시 글자를 미리 걷어내지만, 그 방어가 뚫렸을 때
        `IndexError` 로 수집이 죽는 것이 가장 나쁜 결말이다 — 한 줄 때문에
        그 회차의 **모든 줄**이 저장되지 않는다.
        """
        def swap(match: re.Match[str]) -> str:
            index = int(match.group(1))
            if 0 <= index < len(self._kept):
                return self._kept[index]
            return match.group(0)

        return _PLACEHOLDER_RE.sub(swap, text)


def _normalize(raw: str) -> str:
    """같은 값을 같게 본다 — `010-1234-5678` 과 `010 1234 5678` 은 한 사람이다."""
    return re.sub(r"[\s\-–—.()]", "", str(raw or "")).lower()


def mask_sensitive(text: str) -> MaskResult:
    """민감정보를 가린 글과 **유형별 횟수**를 돌려준다.

    순서가 계약이다. 긴 것부터 가려야 짧은 규칙이 그 조각을 다시 집지 않는다 —
    주민등록번호를 먼저 가리지 않으면 뒷자리 일곱 자리가 전화번호로 보인다.
    """
    original = str(text or "")
    if not original:
        return MaskResult("", {}, POLICY_REVISION)

    masker = _Masker()
    # **자리 표시 글자를 먼저 걷어낸다.**
    #
    # 변환된 첨부 본문에는 NUL 바이트가 들어온다(저장소에 그 전용 시험이 따로
    # 있다 — `test_derived_text_nul.py`). 그 글자가 남아 있으면 `restore` 가
    # 글쓴이가 넣은 숫자를 색인으로 읽고, 잘되면 수집이 죽고 못되면 다른 자리의
    # 토큰이 그 자리에 들어간다.
    body = masker.park(original.replace(SENTINEL, ""))

    def rrn(match: re.Match[str]) -> str:
        if not _is_rrn(match):
            return match.group(0)
        return masker.token(_rrn_type(match), match.group(0))

    body = _RRN_RE.sub(rrn, body)

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"[^0-9]", "", match.group(0))
        if not (13 <= len(digits) <= 19) or not _luhn_ok(digits):
            return match.group(0)
        return masker.token(CARD, match.group(0))

    body = _CARD_RE.sub(card, body)

    def account(match: re.Match[str]) -> str:
        value = match.group(1)
        if len(re.sub(r"[^0-9]", "", value)) < 10:
            return match.group(0)
        start = match.start(1)
        head = match.group(0)[: start - match.start()]
        return head + masker.token(ACCOUNT, value)

    body = _ACCOUNT_RE.sub(account, body)
    body = _EMAIL_RE.sub(lambda m: masker.token(EMAIL, m.group(0)), body)
    body = _PHONE_RE.sub(lambda m: masker.token(PHONE, m.group(0)), body)
    body = _ADDRESS_RE.sub(lambda m: masker.token(ADDRESS, m.group(0)), body)
    body = _NAME_RE.sub(
        lambda m: m.group("head") + masker.token(NAME, m.group("value")), body
    )

    return MaskResult(masker.restore(body), dict(masker.counts), POLICY_REVISION)


def is_masked(text: str) -> bool:
    """이 글에 가린 자리가 있나. 멱등성 확인과 전달 직전 검사에 쓴다."""
    return bool(MASK_RE.search(str(text or "")))


def contains_sensitive(text: str) -> bool:
    """가릴 것이 남아 있나. **전달 직전 마지막 관문**이 쓴다.

    모델이 평문을 돌려줘도 Slack·Canvas·QA 로그에는 가린 것만 나가야 한다.
    """
    return mask_sensitive(text).masked


def mask_lines(lines) -> tuple[list[str], MaskResult]:
    """여러 줄을 **한 문서로** 가린다.

    줄마다 따로 가리면 같은 사람이 줄마다 `[이름1]` 을 받아, 두 사람인지 한
    사람인지 알 수 없게 된다. 번호의 쓸모가 사라진다.
    """
    rows = [str(line or "") for line in (lines or ())]
    joined = "\n".join(rows)
    result = mask_sensitive(joined)
    return result.text.split("\n") if rows else [], result


def metadata(result: MaskResult) -> dict:
    """프론트매터에 적는 값. **가린 값도, 주변 원문도 들어가지 않는다.**"""
    return {
        "masked": result.masked,
        "mask_revision": result.revision,
        "mask_types": list(result.types),
        "mask_counts": {code: result.counts[code] for code in result.types},
    }


__all__ = [
    "ACCOUNT",
    "ADDRESS",
    "CARD",
    "EMAIL",
    "FOREIGN_ID",
    "LABELS",
    "MASK_RE",
    "NAME",
    "PHONE",
    "POLICY_REVISION",
    "RRN",
    "TYPES",
    "MaskResult",
    "contains_sensitive",
    "is_masked",
    "mask_lines",
    "mask_sensitive",
    "metadata",
]
