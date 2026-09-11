"""보고서 여러 건을 종합할 때 — 문서마다 근거 몫을 보장한다.

설계: [`docs/design/document-pipeline-trace-and-report-summary.md`](../../docs/design/document-pipeline-trace-and-report-summary.md) §10·§11·§13

## 왜 채널당 자르기로는 안 되나
기간 요약은 채널마다 **최근 60줄**을 고른다. 그런데 첨부 하나가 수백·수천 줄이다
(2026-09-11 실측: `현장이력카드_낙동강.xlsx` 747줄). 그러면 이렇게 된다.

- 한 보고서의 **꼬리 60줄**만 남는다 — 표의 합계만 남고 제목·머리글이 사라진다
- 다른 보고서는 첨부 표시까지 통째로 밀려난다
- 질문과의 관련도가 아니라 **업로드 순서**가 결과를 정한다

그래서 「11건의 주간보고를 종합해줘」 가 「마지막에 올라온 한 건의 꼬리」 가 된다.

## 문서 단위로 나눈다
후보 문서를 먼저 확정하고, 각 문서에 **최소 몫**을 보장한 뒤 남은 예산을 관련도로
배분한다. 한 문서가 전체를 독점하지 못하게 상한도 둔다.

## 빠진 것을 빠졌다고 말한다
일부 문서가 근거에 못 들어갔으면 「전체를 종합했다」 고 하지 않는다. 대상·확인·실패·
미확인 건수를 답변에 적는다(§13). 그게 없으면 불완전한 종합이 완전한 것처럼 보인다.

## 파일명으로 내용을 지어내지 않는다
파일명이 후보 선정의 근거는 되지만, 내용을 못 읽은 문서는 **「파일은 있으나 내용을
확인하지 못했다」** 로 남는다. 「주간보고가 있다」 에서 「주간보고에 이렇게 적혀 있다」
로 넘어가지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# `archive/files.py` 가 쓰는 표시. 파일명에 `]` 가 들어가므로 **접두사로** 자른다 —
# 정규식으로 라벨을 잡으면 `[주간업무보고] …hwp` 의 첫 `]` 에서 끊긴다.
LISTED_PREFIX = "[첨부:"
EXTRACT_PREFIXES = ("[첨부추출:", "[첨부본문:")

# 표의 머리글·합계로 보이는 줄. 표는 **머리와 꼬리가 둘 다** 필요하다 — 사람이 묻는
# 값은 대개 합계인데, 머리글이 없으면 그 숫자가 무엇인지 알 수 없다.
HEAD_HINT_RE = re.compile(r"(구분|항목|공종|현장|기준|일자|번호|No\.|계약|내용|비고)")
TAIL_HINT_RE = re.compile(r"(합계|총계|소계|누계|계\s*$|총\s*액)")
NUMBER_RE = re.compile(r"\d[\d,.]*\s*(억|만|천|원|%|건|일|개월)?")

# 기본 예산(§11). 실측 후 조정하되, **최소 몫은 0이 될 수 없다** — 0이면 그 문서는
# 후보에 있었는데 근거가 없는 상태가 되고, 그건 빠진 것과 같다.
MAX_DOCUMENTS = 20
MIN_LINES_PER_DOC = 8
MAX_LINES_PER_DOC = 40
MAX_TOTAL_LINES = 400


@dataclass
class DocumentEvidence:
    """후보 문서 하나. 첨부 하나 또는 채널의 일반 대화 묶음."""

    key: str                 # 워크스페이스/채널/이름
    workspace: str
    channel: str
    title: str               # 파일명. 일반 대화면 채널명
    is_attachment: bool
    source_link: str = ""    # 원본 파일 링크(있을 때)
    lines: list = field(default_factory=list)   # RawLine 또는 그와 같은 모양
    listed_only: bool = False   # 첨부 표시는 있는데 본문이 없다

    @property
    def extracted_lines(self) -> int:
        return len(self.lines)

    @property
    def unread(self) -> bool:
        """파일은 있는데 내용을 확인하지 못했다.

        「자료가 없다」 와 **다른 사실**이다. 섞으면 사람이 없는 자료를 찾아 나선다.
        """
        return self.is_attachment and not self.lines


def _text_of(line) -> str:
    return str(getattr(line, "text", line) or "")


# 파일명에 `]` 와 ` (` 가 **둘 다** 들어간다 — 실측 이름이 그렇다.
#
#   [주간업무보고] 2026.09.10_방글라데시 차토그람 하수도.hwp
#   [주간보고]광명자원회수시설 (26년09월2주차).hwpx
#
# 그래서 첫 `]` 나 첫 ` (` 에서 자르면 이름이 잘리고, 같은 파일이 여러 문서로
# 쪼개진다. 확장자를 기준으로 삼는다 — 이름은 항상 `.확장자` 로 끝난다.
_EXT = r"\.[A-Za-z0-9]{1,8}"
BODY_RE = re.compile(rf"^\[첨부(?:추출|본문):(?P<name>.+?{_EXT})\]\s")
LISTED_RE = re.compile(rf"^\[첨부:[^\]]*\]\s(?P<name>.+?{_EXT})\s\(")


def _attachment_name(text: str) -> tuple[str, bool] | None:
    """첨부 줄이면 (파일명, 본문줄인가). 아니면 `None`."""
    m = BODY_RE.match(text)
    if m:
        return m.group("name"), True
    m = LISTED_RE.match(text)
    if m:
        return m.group("name"), False

    # 확장자가 없는 이름. 드물지만 있으면 첫 구분자로 되짚는다 — 완벽하지 않아도
    # 「못 찾음」 보다는 낫다.
    for prefix in EXTRACT_PREFIXES:
        if text.startswith(prefix):
            name, sep, _ = text[len(prefix):].partition("] ")
            if sep and name.strip():
                return name.strip(), True
    if text.startswith(LISTED_PREFIX):
        _, sep, after = text[len(LISTED_PREFIX):].partition("] ")
        name, _, _tail = after.partition(" (") if sep else ("", "", "")
        if name.strip():
            return name.strip(), False
    return None


def _link_of(text: str) -> str:
    """첨부 목록 줄에 붙은 원본 링크. 없으면 빈 문자열."""
    start = text.find("<")
    if start < 0:
        return ""
    end = text.find(">", start)
    if end < 0:
        return ""
    return text[start + 1:end].split("|", 1)[0]


def group_documents(hits_or_docs) -> list[DocumentEvidence]:
    """원문 줄을 **문서 단위**로 묶는다.

    `hits_or_docs` 는 `(doc, lines)` 쌍의 반복이다. 한 채널 안에서 첨부는 파일명마다,
    일반 대화는 채널마다 하나로 묶인다 — 대화를 버리면 「누가 무슨 말을 했나」 가
    사라지고, 그건 문서 수치와 구별해야 하는 다른 근거다(원칙 7).
    """
    out: dict[str, DocumentEvidence] = {}
    order: list[str] = []

    def _slot(doc, title: str, *, attachment: bool) -> DocumentEvidence:
        key = f"{doc.workspace}/{doc.channel}/{title}"
        if key not in out:
            out[key] = DocumentEvidence(
                key=key,
                workspace=doc.workspace,
                channel=doc.channel,
                title=title,
                is_attachment=attachment,
            )
            order.append(key)
        return out[key]

    for doc, lines in hits_or_docs:
        for line in lines:
            text = _text_of(line).strip()
            found = _attachment_name(text)
            if found is None:
                _slot(doc, doc.channel, attachment=False).lines.append(line)
                continue
            name, is_body = found
            slot = _slot(doc, name, attachment=True)
            if is_body:
                slot.lines.append(line)
            else:
                slot.listed_only = True
                slot.source_link = slot.source_link or _link_of(text)

    for item in out.values():
        # 본문이 들어온 첨부는 더 이상 「표시만 있는」 문서가 아니다.
        if item.lines:
            item.listed_only = False
    return [out[k] for k in order]


def _relevance(line, terms: list[str]) -> int:
    text = _text_of(line)
    return sum(1 for term in terms if term and term in text)


def pick_lines(item: DocumentEvidence, terms: list[str], budget: int) -> list:
    """문서 하나에서 근거 줄을 고른다. **꼬리를 자르지 않는다.**

    표는 머리(무슨 값인지)와 꼬리(합계)가 둘 다 필요하다. 뒤에서 N줄만 떼면 합계는
    남고 머리글이 사라져, 숫자가 무엇인지 모르는 근거가 된다.

    고르는 순서: 질문과 겹치는 줄 → 숫자가 있는 줄 → 머리글·합계로 보이는 줄 →
    나머지는 앞에서. 마지막에 **원문 순서로 되돌린다** — 순서가 섞이면 표가 표로
    읽히지 않는다.
    """
    if budget <= 0 or not item.lines:
        return []
    if len(item.lines) <= budget:
        return list(item.lines)

    scored: list[tuple[int, int]] = []
    for i, line in enumerate(item.lines):
        text = _text_of(line)
        score = _relevance(line, terms) * 4
        if NUMBER_RE.search(text):
            score += 2
        if HEAD_HINT_RE.search(text) or TAIL_HINT_RE.search(text):
            score += 2
        # 첫 줄은 제목·머리글, 마지막 줄은 합계다. 둘은 **일반 행보다 세야 한다** —
        # 숫자가 든 평범한 행이 머리글을 밀어내면, 남은 숫자가 무엇인지 알 수 없다.
        if i == 0 or i == len(item.lines) - 1:
            score += 4
        scored.append((score, i))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    keep = sorted(i for _, i in scored[:budget])
    return [item.lines[i] for i in keep]


@dataclass
class Allocation:
    """무엇을 근거로 썼고 무엇이 빠졌는가. 답변의 「확인 범위」 가 여기서 나온다."""

    selected: list[tuple[DocumentEvidence, list]] = field(default_factory=list)
    candidates: int = 0
    unread: list[DocumentEvidence] = field(default_factory=list)
    dropped: list[DocumentEvidence] = field(default_factory=list)

    @property
    def used(self) -> int:
        return len(self.selected)

    @property
    def complete(self) -> bool:
        """전부 반영했는가. 아니면 「전체를 종합했다」 고 말하면 안 된다."""
        return not self.unread and not self.dropped


def allocate(
    documents: list[DocumentEvidence],
    terms: list[str] | None = None,
    *,
    max_documents: int = MAX_DOCUMENTS,
    min_lines: int = MIN_LINES_PER_DOC,
    max_lines: int = MAX_LINES_PER_DOC,
    total_lines: int = MAX_TOTAL_LINES,
) -> Allocation:
    """문서마다 최소 몫을 보장하고 남은 예산을 관련도로 나눈다.

    업로드 순서나 파일 크기가 결과를 정하지 못하게 한다 — 그게 채널당 자르기의
    실제 동작이었다.
    """
    terms = [t for t in (terms or []) if t]
    got = Allocation(candidates=len(documents))

    readable = [d for d in documents if d.lines]
    got.unread = [d for d in documents if d.unread]

    # 관련도 높은 순으로 자리를 준다. 자리가 모자라면 **버린 것을 남긴다.**
    ranked = sorted(
        readable,
        key=lambda d: (-sum(_relevance(ln, terms) for ln in d.lines), -d.extracted_lines),
    )
    chosen, got.dropped = ranked[:max_documents], ranked[max_documents:]
    if not chosen:
        return got

    # 1) 최소 몫. 전체 예산이 모자라면 문서 수에 맞춰 줄이되 1줄 아래로는 안 간다 —
    #    0줄이면 후보에 있었는데 근거가 없는 상태가 되고, 그건 빠진 것과 같다.
    share = max(1, min(min_lines, total_lines // len(chosen)))
    budgets = {d.key: min(share, d.extracted_lines, max_lines) for d in chosen}

    # 2) 남은 예산은 관련도 순으로 더 준다.
    left = total_lines - sum(budgets.values())
    for item in ranked[:max_documents]:
        if left <= 0:
            break
        room = min(max_lines, item.extracted_lines) - budgets[item.key]
        if room <= 0:
            continue
        add = min(room, left)
        budgets[item.key] += add
        left -= add

    # 원래 순서로 돌려준다. 관련도 순으로 내보내면 시간 흐름이 섞인다.
    by_key = {d.key: d for d in chosen}
    got.selected = [
        (by_key[d.key], pick_lines(by_key[d.key], terms, budgets[d.key]))
        for d in documents
        if d.key in by_key
    ]
    return got


def coverage_line(got: Allocation) -> str:
    """답변에 붙일 「확인 범위」 한 줄.

    일부가 빠졌는데 「전체를 종합했다」 고 말하지 않기 위한 것이다(§13).
    """
    parts = [f"대상 {got.candidates}건", f"내용 확인 {got.used}건"]
    if got.unread:
        parts.append(f"내용 미확인 {len(got.unread)}건")
    if got.dropped:
        parts.append(f"분량 초과로 제외 {len(got.dropped)}건")
    return " / ".join(parts)


def coverage_block(got: Allocation) -> str:
    """확인 범위와 확인하지 못한 파일 목록. 빠진 것이 없으면 한 줄로 끝낸다."""
    lines = ["*확인 범위*", f"- {coverage_line(got)}"]
    missing = [*got.unread, *got.dropped]
    if missing:
        names = ", ".join(d.title for d in missing[:10])
        if len(missing) > 10:
            names += f" 외 {len(missing) - 10}건"
        # 「자료가 없다」 가 아니라 「읽지 못했다」 다. 이 구별이 이 줄의 요점이다.
        lines.append(f"- 내용을 확인하지 못한 파일: {names}")
    return "\n".join(lines)
