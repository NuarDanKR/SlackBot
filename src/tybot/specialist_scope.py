"""질문이 몇 개의 **범위**를 묻는지 세고, 검색 예산을 그 범위에 나눈다.

## 막으려는 일

「김해외동·평택고덕·송산서측 세 현장의 7·8·9월 기성을 비교해줘」 는 범위가
**아홉 개**다. 그런데 전문가의 검색 상한은 질문 하나당 고정이었다
(`MAX_SEARCH_CALLS = 2`).

그래서 이런 일이 났다.

| 벌어지는 일 | 결과 |
|---|---|
| 모델이 첫 현장부터 찾는다 | 검색 2회가 거기서 끝난다 |
| 나머지 여덟 범위 | **보지도 않는다** |
| 그 사실 | 어디에도 안 적힌다 |

답이 나오면 사람은 **아홉 범위를 다 본 답**으로 읽는다. 답이 안 나오면
「검색 한도에 닿았습니다」 한 줄뿐이라 **어디까지 봤는지** 알 수 없다.

## 이 모듈이 하는 일

| 한다 | 하지 않는다 |
|---|---|
| 질문에서 현장·기간 축을 센다 | 현장명·기간을 **지어내지 않는다** |
| 범위 수만큼 검색 예산을 늘린다(상한 안에서) | 상한 자체를 없애지 않는다 |
| 한 범위가 예산을 다 먹지 못하게 막는다 | 어느 범위가 중요한지 고르지 않는다 |
| **안 본 범위를 이름으로** 남긴다 | 안 본 것을 「없다」 로 바꾸지 않는다 |

## 못 읽으면 예전처럼 돈다

축을 하나도 못 찾으면 범위는 **하나**다. 그때 예산은 지금과 같고, 동작도 같다.
파싱이 애매한 질문에서 예산이 늘어나면 비용만 늘고 답은 안 좋아진다 —
**늘리는 쪽이 아니라 못 늘리는 쪽이 안전한 기본값**이다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 범위 수의 상한. 열 개를 넘기면 어차피 한 번의 답으로 묶을 수 없고,
# 예산만 늘어난다. 넘으면 **잘라 내고 그 사실을 남긴다.**
MAX_SCOPES = 12

#: 범위 하나에 주는 검색 횟수.
SEARCHES_PER_SCOPE = 1

#: 범위가 몇 개든 넘지 않는 검색 총량. 시간이 유한하기 때문이다.
MAX_TOTAL_SEARCHES = 8

#: 범위가 하나일 때의 기본값. **지금 동작과 같다.**
BASE_SEARCHES = 2

# --- 기간 ---------------------------------------------------------------------
#
# 기간은 표기가 좁아 정규식으로 안전하다. 현장명과 달리 **지어낼 여지가 적다.**
#
# ## 두 자리 달을 먼저 본다
#
# `0?[1-9]|1[0-2]` 는 `2026.10` 에서 `0?[1-9]` 가 먼저 걸려 **`2026.1` 로 잘린다.**
# 10·11·12월이 1월로 둔갑하면 범위 이름이 질문에 없는 기간이 되고, 「확인하지
# 못한 범위」 에 엉뚱한 달이 적힌다. 긴 쪽을 **먼저** 적는다.
_MONTH = r"(?:1[0-2]|0?[1-9])"

# ## 겹치면 먼저 잡은 쪽만 센다
#
# 패턴을 따로 돌려 모으면 `2026년 9월` 이 연-월 패턴과 달-만 패턴에 **두 번**
# 걸려 같은 기간이 범위 두 개가 된다. 범위 수가 부풀면 예산이 쪼개지고, 있지도
# 않은 범위가 「못 봤다」 로 남는다. 자리(span)가 겹치는 뒤 패턴은 버린다.
#
# 순서가 곧 우선순위다. **긴 표기가 앞**이다 — `7·8·9월` 을 달 하나로 줄이지
# 않으려면 목록 패턴이 연-월 패턴보다 앞에 있어야 한다.
#
# ## 분기·반기가 **달로 읽히면 안 된다**
#
# 연-월 패턴은 `월` 이 없어도 맞으므로 `2026년 3분기` 에서 `2026년 3` 을 가져간다.
# 그러면 3분기 질문이 **3월** 범위가 되고, 답변 끝의 범위 이름이 질문에 없는
# 기간을 가리킨다. 분기·반기를 **연-월보다 앞**에 둬 자리를 먼저 가져가게 한다.
_QUARTER = r"(?:(?<!\d)[1-4]\s*분기|(?<![A-Za-z0-9])(?:Q\s*[1-4]|[1-4]\s*Q)(?![A-Za-z0-9]))"
_HALF = r"(?:[상하]반기|(?<![A-Za-z0-9])(?:H\s*[12]|[12]\s*H)(?![A-Za-z0-9]))"

_PERIOD_PATTERNS = (
    # 7·8·9월 · 7, 8월 — 하나의 `월` 을 여러 달이 나눠 쓴다
    re.compile(rf"(?<!\d){_MONTH}\s*(?:[,·/]\s*{_MONTH}\s*)+월"),
    # 2026년 3분기 · 2026 Q3 · 2026-3Q · 2026년 상반기 · 2026 H1
    re.compile(rf"20\d{{2}}\s*[.\-/년]?\s*(?:{_QUARTER}|{_HALF})"),
    # 2026.09 · 2026-9 · 2026년 9월
    re.compile(rf"20\d{{2}}\s*[.\-/년]\s*{_MONTH}(?!\d)\s*월?"),
    # 9월 · 09월 (연도 없이)
    re.compile(rf"(?<!\d){_MONTH}\s*월(?!\s*말까지만)"),
    # 3분기 · Q3 · 상반기 · H1 (연도 없이)
    re.compile(rf"{_QUARTER}|{_HALF}"),
)

#: 달 목록 패턴이 잡은 덩어리를 달 하나씩으로 푼다.
_MONTH_IN_LIST = re.compile(rf"(?<!\d){_MONTH}(?!\d)")

# --- 현장 ---------------------------------------------------------------------
#
# **접미사로만 잡는다.** 「…현장」·「…공구」·「…사업장」 앞의 말만 현장 이름으로
# 본다. 접미사 없이 한국어 명사를 현장으로 추측하면 사람 이름·부서명이 현장이
# 되고, 그 순간 예산이 엉뚱한 축으로 쪼개진다.
_SITE_SUFFIX = r"(?:현장|공구|사업장|지구)"
_SITE_TOKEN = r"[가-힣A-Za-z0-9]+(?:\s*\d+-\d+)?"

# 「…송산서측 **세** 현장」 처럼 목록과 접미사 사이에 수량사가 끼는 말투가 흔하다.
# 이것을 목록의 마지막 이름으로 읽으면 앞의 이름들이 통째로 떨어져 나가
# **세 현장이 한 범위**가 된다. 수량사는 목록 **밖에서** 받아 버린다.
_COUNT_WORD = (
    r"(?:각|모든|전체|여러|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+)\s*(?:개|곳)?"
)
_SITE_LIST = re.compile(
    rf"((?:{_SITE_TOKEN}\s*(?:[,·/]|과|와|랑|이랑)\s*)*{_SITE_TOKEN})"
    rf"(?:\s+{_COUNT_WORD})?\s*{_SITE_SUFFIX}"
)
_SPLIT = re.compile(r"\s*(?:[,·/]|과|와|랑|이랑)\s*")

# 현장 이름으로 보기에 너무 일반적인 말. 「세 현장」 의 「세」 같은 것들이다.
_NOT_A_SITE = frozenset({
    "각", "모든", "전체", "해당", "그", "이", "저", "세", "두", "네", "다섯",
    "여러", "일부", "주요", "신규", "기존", "타", "우리", "저희",
})


def _names(scopes, limit: int = 8) -> str:
    """범위 이름을 사람이 읽을 길이로 묶는다. **질문에 적힌 말뿐이다.**"""
    shown = ", ".join(s.label for s in scopes[:limit])
    return shown + (f" 외 {len(scopes) - limit}개" if len(scopes) > limit else "")


def _grouped(scopes) -> str:
    """현장별로 묶어 **하나도 빠뜨리지 않고** 적는다.

    `_names` 처럼 「외 N개」 로 줄이면 사람이 **어느 범위를 다시 물어야 하는지**
    모른다. 상한에 걸려 빠진 범위가 아홉 개를 넘는 것은 흔한 일이고(현장 일곱 ×
    달 셋이면 벌써 아홉이다), 그때 줄이면 안내가 「많이 빠졌다」 밖에 안 된다.

    그렇다고 쌍을 전부 나열하면 길이가 현장 × 기간으로 는다. 빠진 범위는 대개
    같은 현장의 여러 기간이므로 **현장 하나에 기간을 묶어** 적는다 — 길이는
    현장 수에 비례하고, 식별은 그대로다.
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for scope in scopes:
        key = scope.site
        if key not in groups:
            groups[key] = []
            order.append(key)
        if scope.period:
            groups[key].append(scope.period)
    parts: list[str] = []
    for key in order:
        periods = groups[key]
        if key and periods:
            parts.append(f"{key}({', '.join(periods)})")
        elif key:
            parts.append(key)
        else:
            parts.extend(periods)
    return ", ".join(parts)


# --- 같은 기간의 다른 표기 ------------------------------------------------------
#
# 질문이 「2026년 9월」 이라고 적혀 있어도 모델은 `2026-09` 로 찾는다. 글자만
# 맞춰 보면 그 검색은 **어느 범위도 아닌 것**이 되고, 그러면 범위별 상한이 안
# 걸리고 그 범위는 찾았는데도 「확인하지 못한 범위」 에 남는다.
_YEAR_PREFIX = re.compile(r"^20(\d{2})[.\-/년]?")
_KEY_PATTERNS = (
    ("m", re.compile(r"^(\d{1,2})월?$")),
    ("q", re.compile(r"^(?:([1-4])분기|Q([1-4])|([1-4])Q)$")),
    ("h", re.compile(r"^(?:([상하])반기|H([12])|([12])H)$")),
)
_HALF_INDEX = {"상": 1, "하": 2}


def _period_key(period: str) -> tuple:
    """기간 표기를 **비교할 수 있는 모양**으로. 못 읽으면 글자 그대로.

    `(종류, 연도|None, 번호)` 다. `2026년 3분기`·`2026 Q3`·`2026-3Q` 가 모두
    `("q", 26, 3)` 이 된다 — 사람이 쓰는 표기는 여러 가지인데 범위는 하나다.
    """
    said = (period or "").strip().upper().replace(" ", "")
    year: int | None = None
    prefix = _YEAR_PREFIX.match(said)
    if prefix:
        year, said = int(prefix.group(1)), said[prefix.end():]
    for kind, pattern in _KEY_PATTERNS:
        hit = pattern.match(said)
        if not hit:
            continue
        value = next(g for g in hit.groups() if g)
        return (kind, year, _HALF_INDEX.get(value, 0) or int(value))
    # 연도만 떼고 못 읽었으면 **원문 그대로** 둔다. 모르는 표기를 숫자로
    # 바꿔 두면 엉뚱한 기간과 같아진다.
    return ("t", year, (period or "").strip().replace(" ", ""))


def _same_period(key: tuple, other: tuple) -> bool:
    """두 기간 표기가 같은 기간인가.

    종류가 다르면 다른 기간이다 — 3분기와 3월은 같지 않다.

    연도가 **한쪽에만** 있으면 번호만 보고 같다고 본다. 질문이 「3분기」 라고만
    했는데 모델이 `2026 Q3` 로 찾은 경우다. 이 때문에 여러 해의 같은 분기가
    동시에 걸릴 수 있지만, 그러면 `scope_of` 가 범위를 못 고르고 **막지도
    세지도 않는다** — 넘겨 세는 쪽이 아니라 덜 세는 쪽으로 끝난다.
    """
    if key[0] != other[0]:
        return False
    if key[2] != other[2]:
        return False
    return key[1] is None or other[1] is None or key[1] == other[1]


@dataclass(frozen=True)
class Scope:
    """물어본 범위 하나. **질문에 적힌 말만** 들고 있다."""

    site: str = ""
    period: str = ""

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.site, self.period) if part) or "전체"

    def matches(self, text: str) -> bool:
        """이 검색어가 이 범위를 가리키나. **있는 축이 전부 걸려야** 그 범위다.

        축 하나만 맞아도 참으로 두면 「김해외동」 한 단어가 그 현장의 모든 기간
        범위에 동시에 걸린다. 그러면 어느 범위를 찾은 것인지 못 고르고, 범위별
        상한이 한 번도 걸리지 않는다.

        현장은 **글자 그대로** 본다 — 이름은 질문에 적힌 말이 전부다. 기간은
        표기가 갈리므로(`2026년 9월` ↔ `2026-09`) 모양을 맞춰 본다.
        """
        said = text or ""
        if not (self.site or self.period):
            return False
        if self.site and self.site not in said:
            return False
        if not self.period:
            return True
        if self.period in said:
            return True
        key = _period_key(self.period)
        return any(_same_period(key, _period_key(p)) for p in _periods(said))


def _periods(question: str) -> list[str]:
    """질문에 적힌 기간. **자리가 겹치는 것은 한 번만** 센다."""
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for index, pattern in enumerate(_PERIOD_PATTERNS):
        for match in pattern.finditer(question):
            start, end = match.span()
            if any(start < used_end and used_start < end for used_start, used_end in taken):
                # 앞선(더 구체적인) 패턴이 이미 가져간 자리다.
                continue
            taken.append((start, end))
            text = " ".join(match.group(0).split())
            if index == 0:
                # 「7·8·9월」 → 7월 · 8월 · 9월. 원문에 적힌 달만 푼다.
                for month in _MONTH_IN_LIST.findall(text):
                    hits.append((start, f"{month}월"))
            else:
                hits.append((start, text))
    found: list[str] = []
    for _, value in sorted(hits, key=lambda item: item[0]):
        if value not in found:
            found.append(value)
    return found


def _sites(question: str) -> list[str]:
    found: list[str] = []
    for match in _SITE_LIST.finditer(question):
        for raw in _SPLIT.split(match.group(1)):
            name = " ".join(raw.split())
            if not name or name in _NOT_A_SITE or name in found:
                continue
            if len(name) < 2:
                # 한 글자는 현장 이름으로 보지 않는다. 조사·수식어가 섞인다.
                continue
            found.append(name)
    return found


@dataclass
class ScopePlan:
    """이 질문의 범위와 **검색 예산 배분.**

    모델이 아니라 코드가 센다. 모델에게 「범위마다 한 번씩 찾아라」 라고 적어
    두기만 하면 안 지켜지고, 안 지켜진 것이 어디에도 안 남는다.
    """

    scopes: tuple[Scope, ...] = ()
    #: 범위 수가 상한을 넘어 잘렸나. **잘린 것을 「없다」 로 적지 않는다.**
    truncated: int = 0
    #: 상한에 걸려 **이번 회차에서 빠진** 범위. 모델에게는 안 보이지만
    #: 답변 끝의 미확인 안내에는 이름으로 적힌다 — 「찾아봤는데 없었다」 와
    #: 「상한에 걸려 아예 안 봤다」 가 사람에게 같아 보이면 안 된다.
    dropped: tuple[Scope, ...] = ()
    total_searches: int = BASE_SEARCHES
    per_scope: int = BASE_SEARCHES
    used: int = 0
    scope_used: dict[str, int] = field(default_factory=dict)
    searched: set[str] = field(default_factory=set)

    @property
    def multi(self) -> bool:
        """범위가 여럿인가. 하나면 지금까지와 똑같이 돈다."""
        return len(self.scopes) > 1

    @property
    def uncovered(self) -> tuple[Scope, ...]:
        """**검색을 한 번도 돌리지 않은** 범위.

        「찾아봤는데 없었다」 와 다르다. 저쪽은 사실이고 이쪽은 **모르는 것**이다.

        범위가 하나뿐이면 빈 값이다. 그 질문은 예산을 다투지 않으므로 나눌 것이
        없고, 거기까지 「확인 못 함」 을 붙이면 **모든 답에 붙는 상투어**가 된다 —
        상투어가 되는 순간 진짜 미확인도 안 읽힌다.
        """
        if not self.multi:
            return ()
        # 상한에 걸려 빠진 범위(`dropped`)는 여기 넣지 않는다. 이 값은 모델에게
        # 「다음은 여기를 보라」 로도 나가는데, 이번 회차에서 일부러 뺀 범위를
        # 거기 적으면 상한이 상한이 아니게 된다. 사람에게 알리는 자리는
        # `coverage_note()` 다.
        return tuple(s for s in self.scopes if s.label not in self.searched)

    def scope_of(self, query: str) -> Scope | None:
        """이 검색어가 어느 범위인가. 못 고르면 `None` — 그때는 안 막는다."""
        hits = [s for s in self.scopes if s.matches(query)]
        return hits[0] if len(hits) == 1 else None

    def refuse(self, query: str) -> str:
        """이 검색을 막아야 하면 모델에게 보일 문구. 통과면 빈 문자열."""
        if self.used >= self.total_searches:
            left = self.uncovered
            tail = (
                " 아직 못 본 범위: " + ", ".join(s.label for s in left[:8])
                if left else ""
            )
            return (
                f"(검색 {self.total_searches}회를 다 썼습니다. 더 찾지 말고 "
                f"지금까지 읽은 것으로 답하세요.{tail})"
            )
        if not self.multi:
            return ""
        scope = self.scope_of(query)
        if scope is None:
            return ""
        if self.scope_used.get(scope.label, 0) >= self.per_scope:
            left = [s for s in self.uncovered if s.label != scope.label]
            tail = (
                " 아직 안 본 범위부터 보세요: " + ", ".join(s.label for s in left[:8])
                if left else ""
            )
            return (
                f"(「{scope.label}」 범위는 검색 {self.per_scope}회를 다 썼습니다."
                f"{tail})"
            )
        return ""

    def spend(self, query: str) -> None:
        """검색 한 번을 기록한다. **실제로 돌린 것만** 부른다."""
        self.used += 1
        scope = self.scope_of(query)
        if scope is None:
            return
        self.scope_used[scope.label] = self.scope_used.get(scope.label, 0) + 1
        self.searched.add(scope.label)

    def briefing(self) -> str:
        """모델에게 보일 범위·예산 안내. **범위 이름은 질문에서 온 것뿐이다.**"""
        if not self.multi:
            return ""
        names = ", ".join(s.label for s in self.scopes)
        more = f" (외 {self.truncated}개는 이번 회차에서 제외)" if self.truncated else ""
        return (
            f"이 질문은 범위가 {len(self.scopes)}개입니다: {names}{more}\n"
            f"검색은 모두 {self.total_searches}회, 한 범위에 {self.per_scope}회까지 "
            "쓸 수 있습니다. 한 범위에 몰아 쓰지 말고 범위마다 나눠 찾으세요.\n"
            "**끝까지 못 본 범위는 답변 끝에 그대로 적으세요.** "
            "못 본 범위를 「자료가 없다」 고 쓰지 마세요 — 둘은 다릅니다."
        )

    def coverage_note(self) -> str:
        """답변에 붙일 한 줄. 못 본 범위가 없으면 빈 문자열.

        **두 가지를 가른다.** 찾을 기회가 있었는데 못 본 범위와, 범위 상한에
        걸려 이번 회차에 아예 들어오지 못한 범위는 사람이 할 일이 다르다 —
        뒤쪽은 질문을 나눠 다시 물어야 한다. 수만 적으면 그 말을 못 한다.
        """
        parts = []
        if self.uncovered:
            # 미확인은 범위 상한 안쪽이라 길어야 `MAX_SCOPES` 개다. **줄이지 않는다.**
            parts.append(f"확인하지 못한 범위: {_names(self.uncovered, MAX_SCOPES)}")
        if self.dropped:
            parts.append(
                f"범위 상한({MAX_SCOPES}개)으로 이번 답변에서 제외: "
                f"{_grouped(self.dropped)} — 나눠서 다시 물어 주세요"
            )
        if not parts:
            return ""
        return "※ " + " / ".join(parts) + " (자료가 없다는 뜻이 아닙니다)"


def plan(question: str, *, limit: int | None = None) -> ScopePlan:
    """질문 → 범위와 예산. **못 읽으면 범위 하나**로 둔다.

    `limit` 은 도구상자가 실제로 실행해 주는 검색 횟수(`ToolBudget.max_per_tool`)다.
    **허용과 실행이 다르면 안 된다** — 8회를 쓰라고 안내하고 6회에서 거절당하면
    모델은 남은 두 범위를 못 찾고, 그 거절은 「찾아봤다」 와 구분되지 않는다.
    """
    sites = _sites(question or "")
    periods = _periods(question or "")
    scopes: list[Scope] = []
    if sites and periods:
        scopes = [Scope(site=s, period=p) for s in sites for p in periods]
    elif sites:
        scopes = [Scope(site=s) for s in sites]
    elif periods:
        scopes = [Scope(period=p) for p in periods]

    truncated = max(0, len(scopes) - MAX_SCOPES)
    dropped = tuple(scopes[MAX_SCOPES:])
    scopes = scopes[:MAX_SCOPES]
    ceiling = MAX_TOTAL_SEARCHES if limit is None else min(MAX_TOTAL_SEARCHES, max(1, limit))
    base = min(BASE_SEARCHES, ceiling)
    if len(scopes) <= 1:
        # 축을 못 찾았거나 하나뿐이다. **예산을 늘리지 않는다.**
        return ScopePlan(
            scopes=tuple(scopes), truncated=truncated, dropped=dropped,
            total_searches=base, per_scope=base,
        )
    total = min(ceiling, max(base, len(scopes) * SEARCHES_PER_SCOPE))
    # 범위가 예산보다 많으면 한 범위에 한 번씩이고, 그래도 남는 범위는
    # **못 본 범위로 남는다.** 억지로 나누지 않는다.
    per_scope = max(1, total // len(scopes))
    return ScopePlan(
        scopes=tuple(scopes), truncated=truncated, dropped=dropped,
        total_searches=total, per_scope=per_scope,
    )


__all__ = [
    "BASE_SEARCHES",
    "MAX_SCOPES",
    "MAX_TOTAL_SEARCHES",
    "SEARCHES_PER_SCOPE",
    "Scope",
    "ScopePlan",
    "plan",
]
