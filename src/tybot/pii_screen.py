"""PII 판정 — **위험 단어 하나**가 아니라 **증거의 조합**으로 막는다.

설계: [`docs/design/pii-guardrail-and-canvas-artifacts.md`](../../docs/design/pii-guardrail-and-canvas-artifacts.md) §2

## 왜 바꿨나

예전 `writer.screen()` 은 `등기부등본` 이라는 **일반 명사**를 주민등록번호와 같은
줄에 놓고 봤다. 그래서 OCR 된 일정표에 "등기부등본 제출" 한 줄이 있으면 파일
전체가 `pii_refused` 가 됐다. 사람이 보기에 그 파일에는 개인정보가 없다.

그 오탐은 그냥 불편한 정도가 아니다 — **막힌 파일은 근거에서 사라지고**, 봇은
"자료가 없다" 고 답한다. 사람은 그것을 「봇이 파일을 못 읽는다」 로 읽는다.

## 무엇을 낮추지 않았나

**보안 수준 자체는 그대로다.**

- 직접 식별자(주민등록번호 형태)는 coverage 와 무관하게 **즉시 파일 전체 차단**.
- 실제 고위험 문서(등기부등본·개인정보 명단)는 계속 차단한다.

낮춘 것은 하나뿐이다 — 「위험 단어가 한 번 나왔다」 만으로 막던 것.

## 판정은 결정적이다

**LLM 에게 PII 여부를 묻지 않는다.** 같은 입력은 언제나 같은 결과여야 한다.
확률적 판정을 쓰면 어제 통과한 파일이 오늘 막히고, 그 차이를 설명할 수 없다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# --- 판정 코드 ---------------------------------------------------------------
#
# 코드는 **비민감 값**이다. 로그·감사·콘솔에 이 코드와 파일 좌표만 나가고 원문은
# 절대 복제하지 않는다(§2.4).
RRN = "resident-registration-number"
DOC_REGISTRY = "document-class-registry"
DOC_ROSTER = "document-class-personal-roster"
TERM_MENTIONED = "sensitive-term-mentioned"
TERM_PARTIAL = "sensitive-term-partial-coverage"

Severity = Literal["notice", "block"]


@dataclass(frozen=True)
class Finding:
    """판정 근거 한 줄. **본문을 담지 않는다** — 코드와 횟수뿐이다."""

    code: str
    label: str
    severity: Severity
    count: int = 1


@dataclass(frozen=True)
class ScreenResult:
    blocked: bool
    block_code: str = ""
    findings: tuple[Finding, ...] = ()

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    @property
    def state(self) -> str:
        """metadata `screen_result` 값."""
        if self.blocked:
            return "blocked"
        return "passed_with_notice" if self.findings else "passed"

    @property
    def reason(self) -> str:
        """기존 `pii_refused` 사유 문자열과 같은 자리에 쓰는 사람용 라벨."""
        if not self.blocked:
            return ""
        return next(
            (f.label for f in self.findings if f.code == self.block_code),
            self.block_code,
        )


# --- 직접 식별자 --------------------------------------------------------------
#
# 주민등록번호 **형태**. 체크섬은 쓰지 않는다 — 2020-10 이후 발급 번호는 검증식이
# 없어서, 체크섬으로 거르면 최근 번호가 통과한다.
#
# 대신 **생년월일 자리의 타당성**을 본다. 월·일 자리가 유효하지 않은 견본 번호를
# 실제 번호로 세면 회계 문서의 일련번호까지 막힌다.
_RRN_RE = re.compile(r"(?<![0-9])(\d{2})(\d{2})(\d{2})\s*[-–—]\s*([1-8])\d{6}(?![0-9])")


def _looks_like_rrn(match: re.Match[str]) -> bool:
    month = int(match.group(2))
    day = int(match.group(3))
    return 1 <= month <= 12 and 1 <= day <= 31


def direct_identifiers(text: str) -> int:
    """주민등록번호 형태의 개수. **한 건이라도 있으면 차단**이다."""
    return sum(1 for m in _RRN_RE.finditer(text or "") if _looks_like_rrn(m))


# --- 고위험 문서 분류 ---------------------------------------------------------
#
# 「이 파일이 그 문서인가」 를 본다. 「그 단어가 나왔나」 가 아니다.


@dataclass(frozen=True)
class DocumentClass:
    """고위험 문서 한 종류."""

    code: str
    label: str
    # 파일명·문서 제목에 이 말이 있으면 문서 자체일 가능성이 크다.
    name_terms: tuple[str, ...]
    # 그 문서에만 나오는 **양식 표식**. 일반 문장에 우연히 섞이지 않는 말이어야 한다.
    markers: tuple[re.Pattern[str], ...]
    # 본문 어디든 나오면 「언급」 으로 남기는 말(차단은 아니다).
    mention: re.Pattern[str] | None = None


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


DOCUMENT_CLASSES: tuple[DocumentClass, ...] = (
    DocumentClass(
        code=DOC_REGISTRY,
        label="등기부등본",
        name_terms=("등기부등본", "등기사항전부증명서", "등기사항증명서"),
        markers=(
            _p(r"표제부"),
            _p(r"갑\s*구"),
            _p(r"을\s*구"),
            _p(r"순위\s*번호"),
            _p(r"등기\s*목적"),
            _p(r"소재지번"),
            _p(r"권리자\s*및\s*기타사항"),
            _p(r"고유번호\s*[0-9]{4}\s*-\s*[0-9]{4}"),
            _p(r"열람일시"),
        ),
        mention=_p(r"등기부\s*등본|등기사항(전부)?증명서"),
    ),
    DocumentClass(
        code=DOC_ROSTER,
        label="개인 인적사항 명단",
        name_terms=("계약자명단", "계약자 명단", "입주자명단", "인적사항", "연락처명단"),
        markers=(
            _p(r"생년\s*월일"),
            _p(r"주민(등록)?번호"),
            _p(r"휴대(전화|폰)"),
            _p(r"(^|[^가-힣])연락처"),
            _p(r"세대주"),
            _p(r"동\s*호수"),
            _p(r"자택\s*주소"),
        ),
        # `주민번호` 를 **언급만** 한 문장("주민번호는 수집하지 않음")도 여기서
        # notice 로 남는다. 차단이 아니다 — 하지만 사람이 나중에 그 파일을 다시
        # 볼 근거는 된다.
        mention=_p(r"계약자\s*명단|입주자\s*명단|인적\s*사항|주민(등록)?번호"),
    ),
)

# --- 점수 --------------------------------------------------------------------
#
# 이 숫자들은 **fixture 로 고정한다**(`tests/test_pii_screen.py`). 오탐이 나온다고
# 점수만 낮추면 실제 등기부등본이 통과한다 — 그건 고친 게 아니라 끈 것이다.
SCORE_NAME_MATCH = 3      # 파일명·문서 제목이 고위험 문서명과 일치
SCORE_MARKER = 1          # 고유 필드 표식 하나
MAX_MARKER_SCORE = 4      # 표식 점수 상한
SCORE_REPEATED = 2        # 같은 표식이 여러 행에서 반복
BLOCK_THRESHOLD = 5       # 합계 이 값 이상이면 문서 분류 차단


def _name_hit(document: DocumentClass, filename: str, head: str) -> bool:
    """파일명이나 **첫 머리글**이 문서명과 일치하는가.

    본문 중간의 "등기부등본 제출" 은 여기 걸리지 않는다. 그게 핵심이다.
    """
    haystack = f"{filename} {head}".replace(" ", "")
    return any(term.replace(" ", "") in haystack for term in document.name_terms)


def _marker_stats(document: DocumentClass, lines: list[str]) -> tuple[int, bool]:
    """(서로 다른 표식 수, 같은 표식이 여러 행에 반복됐는가)."""
    distinct = 0
    repeated = False
    for pattern in document.markers:
        hits = sum(1 for line in lines if pattern.search(line))
        if hits:
            distinct += 1
        if hits >= 2:
            repeated = True
    return distinct, repeated


def _score(document: DocumentClass, filename: str, text: str) -> int:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    head = "\n".join(lines[:5])
    total = SCORE_NAME_MATCH if _name_hit(document, filename, head) else 0
    distinct, repeated = _marker_stats(document, lines)
    total += min(distinct * SCORE_MARKER, MAX_MARKER_SCORE)
    if repeated:
        total += SCORE_REPEATED
    return total


def screen_document(
    text: str,
    *,
    filename: str = "",
    coverage_state: str = "",
) -> ScreenResult:
    """첨부 한 건의 판정. 파일명과 **추출문 전체**를 함께 본다.

    `coverage_state` 는 변환이 얼마나 읽혔는지다(`succeeded`/`partial`/`unknown`).
    **부분만 읽힌 파일에서 「위험 단어만 있었다」 는 안심할 근거가 못 된다** —
    못 읽은 쪽에 진짜가 있을 수 있다. 그래서 통과시키되 표시를 남긴다(§2.3).
    """
    body = text or ""
    findings: list[Finding] = []

    count = direct_identifiers(body)
    if count:
        # 직접 식별자는 **coverage 와 무관하게** 차단이다. 얼마나 읽었든 이미
        # 읽은 곳에 있다.
        findings.append(Finding(RRN, "주민등록번호 형식", "block", count))
        return ScreenResult(True, RRN, tuple(findings))

    for document in DOCUMENT_CLASSES:
        total = _score(document, filename, body)
        if total >= BLOCK_THRESHOLD:
            findings.append(Finding(document.code, document.label, "block", total))
            return ScreenResult(True, document.code, tuple(findings))
        if document.mention and document.mention.search(body):
            findings.append(
                Finding(TERM_MENTIONED, f"{document.label} 언급", "notice", total)
            )

    if findings and coverage_state == "partial":
        findings.append(
            Finding(TERM_PARTIAL, "일부만 읽힌 파일의 민감 용어", "notice")
        )
    return ScreenResult(False, "", tuple(findings))


def screen_line(text: str) -> str | None:
    """줄 단위 검사. **직접 식별자만** 본다.

    사람이 "등기부등본 제출 예정" 이라고 **말한 것**까지 버리면 대화가 사라진다.
    문서인지 아닌지는 줄 하나로 알 수 없다 — 그 판정은 `screen_document()` 몫이다.
    """
    return "주민등록번호 형식" if direct_identifiers(text) else None


@dataclass
class ScreenMetadata:
    """metadata 에 남기는 값. **본문은 하나도 들어가지 않는다.**"""

    version: int = 2
    result: str = "passed"
    codes: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, result: ScreenResult) -> ScreenMetadata:
        return cls(version=2, result=result.state, codes=result.codes)

    def to_json(self) -> dict:
        return {
            "screen_version": self.version,
            "screen_result": self.result,
            "screen_codes": list(self.codes),
        }


__all__ = [
    "BLOCK_THRESHOLD",
    "DOCUMENT_CLASSES",
    "DOC_REGISTRY",
    "DOC_ROSTER",
    "RRN",
    "TERM_MENTIONED",
    "TERM_PARTIAL",
    "DocumentClass",
    "Finding",
    "ScreenMetadata",
    "ScreenResult",
    "direct_identifiers",
    "screen_document",
    "screen_line",
]
