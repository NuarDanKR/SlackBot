"""전문 봇용 도구 — 우리 아카이브 위에서, 우리 권한으로.

설계: [`docs/design/specialist-deployment.md`](../../docs/design/specialist-deployment.md)
가져온 것: `ref/hermes` 스냅샷의 도구 설명문과 검색 규칙 (2026-09-11 커밋 `20746a9`)

## 왜 우리가 구현하나

Hermes 의 값은 모델이 아니라 **찾는 방식**에 있다 — 검색하고, 읽고, 모자라면
다시 검색한다. 그 루프를 프롬프트 한 장으로는 못 옮긴다.

그래서 도구 자체는 우리가 만들고, **설명문과 규칙만 가져온다.** 설명문이 곧
규칙이다. 0건이면 낱말별 건수를 바탕으로 의미를 유지하는 표현으로 다시 찾되,
코드가 세는 호출 예산과 반복 제한 안에서만 탐색한다.

## 권한이 한 곳에 남는다

도구는 전부 `RequestContext` 를 받아 `store.search(query, ctx)` ·
`store.visible_docs(ctx)` 를 통과한 것만 돌려준다. 그 두 함수가 우리 ACL 의
유일한 구현이다.

**전문 봇이 아카이브 파일을 직접 읽게 하면 그 판정을 그쪽이 다시 구현해야 하고,
둘이 갈리면 오류 없이 새어 나간다.** 갈린 답은 보이면 안 되는 내용에 우리
출처가 붙은 모습으로 나타나서, 추적조차 안 된다.

## 무엇을 열었는지 기억한다

`Touched` 가 도구가 돌려준 문서를 모은다. 출처는 마스터가 붙이는데, 그러려면
**무엇이 실제로 근거가 됐는지** 알아야 한다. 모델이 본문에 적은 것을 믿고
출처를 만들면 그게 곧 환각이다.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .access import RequestContext
from .gateway.base import ToolSpec

log = logging.getLogger("tybot.specialist_tools")

# 한 번의 도구 호출이 돌려줄 최대 글자. 넘으면 자른다 — 자르는 것이 나은 이유는,
# 컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나가기 때문이다.
MAX_TOOL_CHARS = 30_000
MAX_SEARCH_HITS = 30
MAX_LIVE_MESSAGES = 50

# 라이브 조회로 가져온 줄에 붙는 표시.
#
# **아카이브에 없는 것을 근거로 쓴다는 사실을 답변 경로가 알아야 한다.**
# 출처를 아카이브 문서로 붙이면 그 문서에는 없는 내용이 되고, 사람이 확인하러
# 갔을 때 찾지 못한다. 그 순간 출처는 신뢰를 만드는 것이 아니라 깎는다.
LIVE_MARK = "[실시간]"

DOCUMENT_PREFIXES = (
    "[첨부추출:",
    "[첨부본문:",
    "[캔버스:수집]",
    "[캔버스본문:",
)


# --- 예산 (설계: hermes-integration-fidelity.md §3-C) -------------------------
#
# **찾는 것을 막지 않되, 끝없이 찾게 두지도 않는다.**
#
# 예전 프롬프트는 "검색 결과가 없다고 낱말을 바꿔 다시 부르지 마세요" 로 재검색을
# 막았다. 그 규칙이 검색 실패와 자료 부재를 같은 것으로 만들었다 — 사람이 쓴 말과
# 문서에 적힌 말이 다르기만 해도 없다고 답했다.
#
# 그래서 규칙이 아니라 **예산**으로 묶는다. 다시 찾는 것은 허용하고, 얼마나
# 쓸 수 있는지는 코드가 센다. 예산이 끝나면 그 사실을 모델에게 문자열로 알려
# 「지금까지 읽은 것으로 답하라」 로 보낸다.
#
# 그리고 **예산 소진과 자료 없음을 구별해서 밖으로 내보낸다.** 둘을 같은 답으로
# 내면 "없다" 가 사실이 아닌 경우가 섞이고, 사용자는 그걸 알 방법이 없다.
MAX_TOOL_CALLS = 12
MAX_TOOL_CALLS_PER_TOOL = 6
MAX_READ_CHARS = 120_000
MAX_TOOL_SECONDS = 45.0

BUDGET_EXHAUSTED = "budget-exhausted"


@dataclass
class ToolBudget:
    """한 질문이 쓸 수 있는 도구 예산. **요청마다 새로 만든다.**"""

    max_calls: int = MAX_TOOL_CALLS
    max_per_tool: int = MAX_TOOL_CALLS_PER_TOOL
    max_chars: int = MAX_READ_CHARS
    max_seconds: float = MAX_TOOL_SECONDS
    calls: int = 0
    chars: int = 0
    per_tool: dict = field(default_factory=dict)
    started: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.started:
            import time

            self.started = time.monotonic()

    @property
    def exhausted(self) -> bool:
        return bool(self.reason)

    @property
    def elapsed(self) -> float:
        import time

        return time.monotonic() - self.started

    def refuse(self, name: str) -> str:
        """이 호출을 거절해야 하면 모델에게 보일 문자열. 통과면 빈 문자열."""
        if self.calls >= self.max_calls:
            self.reason = "calls"
        elif self.per_tool.get(name, 0) >= self.max_per_tool:
            # 한 도구만 계속 부르는 것은 대개 같은 자리를 맴도는 것이다.
            # 전체 예산을 끝내지는 않는다 — 다른 도구는 아직 쓸 수 있다.
            return (
                f"({name} 호출 한도 {self.max_per_tool}회에 닿았습니다. "
                "다른 도구를 쓰거나 지금까지 읽은 것으로 답하세요.)"
            )
        elif self.chars >= self.max_chars:
            self.reason = "chars"
        elif self.elapsed >= self.max_seconds:
            self.reason = "time"
        if self.reason:
            return (
                "(검색 예산을 다 썼습니다. 더 찾지 말고 지금까지 읽은 것으로 답하세요. "
                "자료가 없다고 단정하지 말고, 어디까지 찾아봤는지 한 줄 적으세요.)"
            )
        return ""

    def spend(self, name: str, text: str) -> None:
        self.calls += 1
        self.per_tool[name] = self.per_tool.get(name, 0) + 1
        self.chars += len(text or "")

    def summary(self) -> str:
        """로그 한 줄. 업무 내용은 담지 않는다."""
        per = ",".join(f"{k}={v}" for k, v in sorted(self.per_tool.items()))
        return (
            f"tool_calls={self.calls} chars={self.chars} "
            f"elapsed_ms={int(self.elapsed * 1000)} per_tool={per or '-'} "
            f"budget={self.reason or 'ok'}"
        )


@dataclass
class Touched:
    """도구가 실제로 돌려준 것. 출처는 이것으로 만든다."""

    documents: list = field(default_factory=list)
    # 모델에게 실제로 전달한 아카이브 줄. 문서만 기억하면 `근거 보기`가
    # 답변 당시 줄이 아니라 마스터의 최초 검색 결과를 다시 열게 된다.
    evidence_hits: list = field(default_factory=list)
    live_permalinks: list[str] = field(default_factory=list)
    # `(workspace, channel_id, message_ts)`. URL은 출처 표시용이고 좌표는
    # 클릭 시 현재 ACL로 다시 열기 위한 값이다.
    live_messages: list[tuple[str, str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def record_doc(self, doc) -> None:
        if doc is not None and doc not in self.documents:
            self.documents.append(doc)

    def record_line(self, doc, line) -> None:
        """모델에게 돌려준 원문 줄을 문서와 함께 기억한다."""
        if doc is None or line is None:
            return
        from .archive.store import SearchHit

        self.record_doc(doc)
        hit = SearchHit(doc=doc, line=line, score=0)
        if hit not in self.evidence_hits:
            self.evidence_hits.append(hit)

    def record_live(self, workspace: str, channel_id: str, message_ts: str) -> None:
        key = (workspace.strip(), channel_id.strip(), message_ts.strip())
        if all(key) and key not in self.live_messages:
            self.live_messages.append(key)

    @property
    def used_live(self) -> bool:
        return bool(self.live_permalinks)


def _clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(이하 생략, {len(text):,}자 중 {limit:,}자)"


# --- 설명문 ------------------------------------------------------------------
#
# `ref/hermes` 의 도구 설명을 옮겼다. 검색 규칙(부분 일치 폴백, 낱말별 건수를
# 보고 판단)은 그 팀이 실측으로 다듬은 것이라 문구를 살린다.
SEARCH_DESCRIPTION = (
    "아카이브 전문 검색. 대화와 문서 본문을 한 번에 훑는다. "
    "공백으로 구분한 낱말이 모두 들어 있는 것을 먼저 찾고, 없으면 일부만 맞은 것을 "
    "겹친 개수 순으로 함께 준다 — 그때는 제목 줄에 `(2/3 낱말)` 처럼 몇 개를 맞췄는지가 "
    "붙는다. 결과가 없으면 낱말별 건수를 보고 동의어·문서명·조직명처럼 의미를 "
    "유지하는 표현으로 다시 찾을 수 있다. 재검색도 호출 예산에 포함되므로 같은 "
    "검색을 반복하지 말 것. 무엇을 찾든 여기서 시작한다."
)

READ_CHANNEL_DESCRIPTION = (
    "채널 하나의 원문을 최근 순으로 읽는다. 검색이 어느 채널인지 알려 준 뒤에 쓴다. "
    "검색 결과 몇 줄로 판단이 안 될 때 앞뒤 맥락을 보는 용도다."
)

READ_DOCUMENT_DESCRIPTION = (
    "문서 하나의 본문을 읽는다. 첨부에서 뽑은 표·보고서 본문이 여기 들어 있다. "
    "검색이 문서 이름을 알려 준 뒤에 쓴다."
)

FETCH_RECENT_DESCRIPTION = (
    "아직 아카이브에 들어오지 않은 **오늘 대화와 현재 채널 Canvas**를 Slack 에서 직접 가져온다. "
    "수집은 주기적으로 돌기 때문에 방금 오간 이야기는 검색에 안 잡힌다. "
    "「방금」·「오늘」·「지금」 처럼 시점이 아주 최근일 때만 쓴다 — "
    "그 밖에는 검색이 더 정확하고 싸다. "
    "여기서 온 줄은 " + LIVE_MARK + " 로 표시되며, 출처가 아카이브 문서가 아니라 "
    "Slack 메시지 링크로 붙는다."
)


def specs(*, live: bool) -> list[ToolSpec]:
    """모델에게 줄 도구 목록. `live=False` 면 실시간 조회를 빼고 준다."""
    out = [
        ToolSpec(
            name="search",
            description=SEARCH_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": '검색 낱말들. 예: "대출 만기 연장 협의"',
                    },
                    "where": {
                        "type": "string",
                        "description": "채널로 좁힐 때만. 예: “팀-전산”",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="read_channel",
            description=READ_CHANNEL_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "채널 이름"},
                    "limit": {
                        "type": "integer",
                        "description": "읽을 줄 수. 기본 80, 최대 400",
                    },
                },
                "required": ["channel"],
            },
        ),
        ToolSpec(
            name="read_document",
            description=READ_DOCUMENT_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "문서 또는 첨부 이름"},
                },
                "required": ["name"],
            },
        ),
    ]
    if live:
        out.append(
            ToolSpec(
                name="fetch_recent_slack",
                description=FETCH_RECENT_DESCRIPTION,
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": {
                            "type": "string",
                            "description": "채널 이름. 비우면 질문이 온 채널",
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"가져올 메시지 수. 기본 20, 최대 {MAX_LIVE_MESSAGES}",
                        },
                    },
                },
            )
        )
    return out


# --- 실행 --------------------------------------------------------------------
@dataclass
class ToolBox:
    """한 요청의 도구 묶음.

    **요청마다 새로 만든다.** `ctx` 를 안에 가둬 두어야, 도구를 부르는 쪽이
    누구 권한으로 읽는지를 인자로 바꿀 수 없다. 인자로 받으면 그 자리가
    곧 권한 우회다.
    """

    store: object
    ctx: RequestContext
    touched: Touched = field(default_factory=Touched)
    # 실시간 조회. 없으면 그 도구를 주지 않는다.
    live_fetch: Callable[[str, int], list[dict]] | None = None
    here: str = ""
    # 이 요청이 쓸 수 있는 도구 예산. **요청마다 새 것**이라, 앞 질문이 다 쓴
    # 예산이 다음 질문을 막지 않는다.
    budget: ToolBudget = field(default_factory=ToolBudget)

    def run(self, name: str, args: dict) -> str:
        """도구 하나 실행. **예외를 밖으로 내지 않는다.**

        도구가 터지면 모델은 그것을 모른 채 답을 만든다. 오류도 문자열로
        돌려줘야 모델이 "그건 안 됐구나" 를 알고 다른 길을 찾는다.

        예산이 끝났으면 실행하지 않고 그 사실을 돌려준다 — 같은 이유로,
        예외가 아니라 문자열이다.
        """
        refusal = self.budget.refuse(name)
        if refusal:
            log.info("도구 예산 거절 name=%s %s", name, self.budget.summary())
            return refusal
        self.touched.calls.append(name)
        out = self._dispatch(name, args)
        self.budget.spend(name, out)
        return out

    def _dispatch(self, name: str, args: dict) -> str:
        try:
            if name == "search":
                return self._search(args)
            if name == "read_channel":
                return self._read_channel(args)
            if name == "read_document":
                return self._read_document(args)
            if name == "fetch_recent_slack":
                return self._fetch_recent(args)
        except Exception as exc:  # noqa: BLE001 - 도구 실패가 답변을 끊으면 안 된다
            log.warning("도구 실패 name=%s: %s", name, type(exc).__name__)
            return f"(도구 오류: {name})"
        return f"(알 수 없는 도구: {name})"

    # -- 검색 ------------------------------------------------------------
    def _channels_matching(self, name: str) -> set[str] | None:
        """권한 안에서 채널 이름을 정확 또는 유일 부분 일치로 푼다.

        일자별 원문은 같은 채널 문서가 여러 개다. 문서 하나를 고르지 않고 채널명
        집합을 돌려줘야 모든 날짜가 검색 범위에 남는다. 모호하면 ``None``이다.
        """
        wanted = name.strip().lstrip("#")
        if not wanted:
            return set()
        names = {
            str(doc.channel or "")
            for doc in self.store.visible_docs(self.ctx)
            if str(doc.channel or "")
        }
        exact = {channel for channel in names if channel.lstrip("#") == wanted}
        if exact:
            return exact
        partial = {channel for channel in names if wanted in channel.lstrip("#")}
        return partial if len(partial) == 1 else None

    def _search(self, args: dict) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "(검색어가 비었습니다)"
        where = str(args.get("where") or "").strip()
        scoped_channels: frozenset[str] | None = None
        if where:
            channels = self._channels_matching(where)
            if channels is None:
                return (
                    f"(채널 범위 「{where}」가 없거나 여러 채널과 일치합니다. "
                    "검색 결과의 정확한 채널 이름으로 다시 지정하세요.)"
                )
            scoped_channels = frozenset(channels)
            hits = self.store.search(
                query,
                self.ctx,
                limit=MAX_SEARCH_HITS,
                channels=scoped_channels,
            )
        else:
            hits = self.store.search(query, self.ctx, limit=MAX_SEARCH_HITS)
        if not hits:
            # **낱말별 건수를 준다.** 없다고만 하면 모델이 낱말을 바꿔 다시 부르고,
            # 그게 가장 흔한 낭비다(Hermes 실측).
            counts = []
            for word in query.split()[:5]:
                found = self.store.search(
                    word,
                    self.ctx,
                    limit=MAX_SEARCH_HITS,
                    channels=scoped_channels,
                )
                counts.append(f"{word} {len(found)}건")
            return (
                f"「{query}」 로 찾은 것이 없습니다.\n"
                f"낱말별: {' · '.join(counts) if counts else '-'}\n"
                "위 건수를 보고 의미를 유지하는 동의어·문서명으로 다시 찾거나, "
                "관련 채널과 문서를 열어 보세요. 같은 검색은 반복하지 마세요."
            )
        from .search_index import tokens_of

        terms = list(dict.fromkeys(tokens_of(query)))
        grouped = [("문서·첨부", []), ("사람 대화", [])]
        for hit in hits:
            text = str(hit.line.text or "")
            target = grouped[0][1] if text.startswith(DOCUMENT_PREFIXES) else grouped[1][1]
            hay = f"{hit.line.speaker} {text}".lower()
            matched = sum(1 for term in terms if term in hay)
            partial = (
                f" ({matched}/{len(terms)} 낱말)"
                if terms and matched < len(terms)
                else ""
            )
            target.append(
                (hit, f"[{hit.line.ts}] ({hit.doc.channel}){partial} "
                 f"{hit.line.speaker}: {text}")
            )

        lines: list[str] = []
        used = 0

        def append_line(rendered: str) -> int:
            nonlocal used
            start = used + (1 if lines else 0)
            lines.append(rendered)
            used = start + len(rendered)
            return start

        for label, items in grouped:
            if not items:
                continue
            if lines:
                append_line("")
            append_line(f"## {label} {len(items)}건")
            for hit, rendered in items:
                if append_line(rendered) < MAX_TOOL_CHARS:
                    self.touched.record_line(hit.doc, hit.line)
        return _clip("\n".join(lines), MAX_TOOL_CHARS)

    # -- 읽기 ------------------------------------------------------------
    def _pick_channel(self, name: str):
        """이름으로 채널 문서를 고른다. **`visible_docs` 만 본다.**"""
        wanted = name.strip().lstrip("#")
        if not wanted:
            return None
        docs = self.store.visible_docs(self.ctx)
        exact = [d for d in docs if d.channel.lstrip("#") == wanted]
        if exact:
            return exact[0]
        partial = [d for d in docs if wanted in d.channel]
        return partial[0] if len(partial) == 1 else None

    def _read_channel(self, args: dict) -> str:
        doc = self._pick_channel(str(args.get("channel") or ""))
        if doc is None:
            return "(그 이름의 채널을 찾지 못했거나 열람 권한이 없습니다)"
        limit = max(1, min(int(args.get("limit") or 80), 400))
        rows = doc.raw_lines[-limit:]
        rendered = [
            f"[{line.ts}] {line.speaker}: {line.text}" for line in rows
        ]
        head = f"# {doc.channel} (최근 {len(rows)}줄)\n"
        used = len(head)
        for line, text in zip(rows, rendered, strict=False):
            start = used + (1 if used > len(head) else 0)
            if start >= MAX_TOOL_CHARS:
                break
            self.touched.record_line(doc, line)
            used = start + len(text)
        return _clip(head + "\n".join(rendered))

    def _read_document(self, args: dict) -> str:
        """문서·첨부 본문. 첨부 변환본은 `[첨부추출:이름]` 줄로 원문에 들어 있다."""
        wanted = str(args.get("name") or "").strip()
        if not wanted:
            return "(문서 이름이 비었습니다)"
        rows: list[str] = []
        used = 0
        for doc in self.store.visible_docs(self.ctx):
            matched = [line for line in doc.raw_lines if wanted in (line.text or "")]
            if matched:
                section = f"# {doc.channel}\n" + "\n".join(
                    line.text for line in matched
                )
                section_start = used + (2 if rows else 0)
                line_start = section_start + len(f"# {doc.channel}\n")
                for line in matched:
                    if line_start < MAX_TOOL_CHARS:
                        self.touched.record_line(doc, line)
                    line_start += len(line.text) + 1
                rows.append(section)
                used = section_start + len(section)
        if not rows:
            return "(그 문서를 찾지 못했거나 열람 권한이 없습니다)"
        return _clip("\n\n".join(rows))

    # -- 실시간 ----------------------------------------------------------
    def _fetch_recent(self, args: dict) -> str:
        """아카이브에 아직 없는 최근 대화.

        **권한을 아카이브와 같은 기준으로 본다.** 채널 이름을 받되, 그 채널이
        `visible_docs` 에 있는(= 요청자가 볼 수 있는) 채널일 때만 가져온다.
        Slack 을 직접 물으면 아카이브 ACL 을 우회하게 되고, 그건 이 도구를
        추가하면서 가장 쉽게 생기는 구멍이다.
        """
        if self.live_fetch is None:
            return "(실시간 조회를 쓸 수 없습니다)"
        name = str(args.get("channel") or self.here or "").strip()
        doc = self._pick_channel(name)
        if doc is None:
            return "(그 채널을 찾지 못했거나 열람 권한이 없습니다)"
        limit = max(1, min(int(args.get("limit") or 20), MAX_LIVE_MESSAGES))
        messages = self.live_fetch(doc.channel_id or doc.channel, limit) or []
        rows: list[str] = []
        head = f"# {doc.channel} — 아카이브에 아직 없는 최근 대화·Canvas "
        for message in messages:
            # **봇 발언을 싣지 않는다.** 실으면 우리 답이 다음 답의 근거가 되고,
            # 한 번 잘못 말한 숫자가 그대로 굳는다(원칙 1).
            if message.get("is_bot"):
                continue
            rendered = (
                f"{LIVE_MARK} [{message.get('ts', '')}] "
                f"{message.get('speaker', '?')}: {message.get('text', '')}"
            )
            # 최종 머리글에는 건수가 들어가지만 자릿수 차이는 작다. 보수적으로
            # 머리글과 현재까지의 줄이 상한 안일 때만 좌표를 남긴다.
            projected = len(head) + 12 + sum(len(row) + 1 for row in rows) + len(rendered)
            if projected <= MAX_TOOL_CHARS:
                link = str(message.get("permalink") or "")
                if link:
                    self.touched.live_permalinks.append(link)
                self.touched.record_live(
                    self.ctx.workspace,
                    str(getattr(doc, "channel_id", "") or ""),
                    str(message.get("ts") or ""),
                )
            rows.append(rendered)
        if not rows:
            return f"({doc.channel} 에 아직 아카이브에 없는 새 대화가 없습니다)"
        return _clip(
            f"# {doc.channel} — 아카이브에 아직 없는 최근 대화·Canvas {len(rows)}건\n"
            + "\n".join(rows)
        )


__all__ = [
    "LIVE_MARK",
    "MAX_LIVE_MESSAGES",
    "MAX_SEARCH_HITS",
    "MAX_TOOL_CHARS",
    "ToolBox",
    "Touched",
    "specs",
]
