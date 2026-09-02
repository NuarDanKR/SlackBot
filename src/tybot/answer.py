"""질의응답 파이프라인 — 환각방지 4겹을 코드로 강제한다.

순서(바꾸지 말 것): 권한 필터 → 원문 검색 → (0건이면 목록만, LLM 호출 안 함) → LLM → 출처 부착 → 로깅.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import documents
from .access import RequestContext
from .archive.store import ArchiveStore, SearchHit
from .attachment_review import find_approved
from .gateway.base import Message, Sensitivity
from .gateway.cost import CostLimitExceeded
from .gateway.router import ModelNotAllowed, Router, UnknownModel
from .intent import (
    DEFAULT_DAYS,
    Intent,
    classify,
    parse_period,
    plan,
)

logger = logging.getLogger("tybot.answer")

SYSTEM_PROMPT = """그럴듯하게 지어내는 것은 모른다고 하는 것보다 나쁘다.

너는 태영건설 사내 아카이브 봇 'TYBot'이다. 아래 <원문> 블록에 실제로 적힌 내용만 근거로 답한다.
규칙:
1. <원문>에 없는 사실은 절대 추가하지 않는다. 일반 상식·추측·외부 지식 금지.
2. 금액·날짜·기관명·사람 이름은 원문 그대로 옮긴다. 반올림·환산·추론 금지.
3. 사람 발언과 문서 내용은 구분해서 쓴다. 숫자가 엇갈리면 두 시점을 함께 표기한다.
4. <원문>으로 답할 수 없으면 "아카이브에서 근거를 찾지 못했습니다"라고만 답한다.
5. 답변은 한국어, 간결하게. 출처 줄은 시스템이 붙이므로 네가 쓰지 않는다.
6. 출력은 Slack 메시지다. `#` 제목과 `**굵게**`는 Slack에서 글자 그대로 보이니 쓰지 않는다.
   굵게는 별표 하나(*굵게*), 목록은 `• `, 구분선은 쓰지 않는다.
"""

SUMMARY_PROMPT = """그럴듯하게 지어내는 것은 모른다고 하는 것보다 나쁘다.

너는 태영건설 사내 아카이브 봇이다. 아래 <원문>은 여러 채널의 실제 대화 기록이다.
이걸로 진행 상황을 정리한다.
규칙:
1. <원문>에 없는 사실·추측·전망을 절대 추가하지 않는다. 진척률·완료 여부를 임의 판단하지 않는다.
2. 금액·날짜·기관명·사람 이름은 원문 그대로. 반올림·환산 금지.
3. **채널별로 묶어서** 정리한다. 각 항목은 `- 내용 (발언자, 날짜)` 형식.
4. 결정된 것 / 진행 중 / 미해결·대기 를 구분한다. 원문에서 판단이 안 되면 그 구분을 비운다.
5. 원문이 빈약하면 "이 기간 원문이 N줄뿐이라 정리가 제한적입니다"를 먼저 밝힌다.
6. 한국어, 간결. 출처 줄은 시스템이 붙이므로 쓰지 않는다.
7. 출력은 Slack 메시지다. `#` 제목·`**굵게**`·`---` 구분선은 Slack에서 글자 그대로 보이니 금지.
   채널 이름은 `*#채널명*`, 하위 항목은 `• `, 들여쓰기는 공백 2칸으로만 표현한다.
8. 질문이 아카이브 내용과 무관하면(봇 설정·연결 상태 등) 정리하지 말고 "아카이브 원문으로 답할 수 있는 질문이 아닙니다"라고만 답한다.
"""

ADVICE_PROMPT = """너는 태영건설 사내 Slack 아카이브 봇 'TYBot'이다.
지금은 **사실 조회가 아니라 업무 판단·권고** 요청을 받았다.

지켜야 할 경계:
1. **사내 사실**(금액·날짜·조직·인원·결정사항·현장 상태)은 <원문>에 적힌 것만 말한다.
   원문에 없으면 "아카이브에 근거가 없다"고 밝히고, 절대 추정치나 예시를 사실처럼 쓰지 않는다.
2. **일반 원칙·장단점·권고**는 네 지식으로 제시해도 된다. 단 그것이 일반적 판단임이 드러나게 쓴다.
3. <원문>에 관련 내용이 있으면 그것을 우선 근거로 삼고, 우리 상황에 맞춰 판단한다.
4. 사실과 판단을 섞어 쓰지 않는다. 무엇이 원문 근거이고 무엇이 일반 판단인지 구분된 문장으로.

형식(Slack mrkdwn — `#` 제목, `**굵게**`, `---` 금지. 굵게는 *별표 하나*):
• 결론 한 줄부터 시작한다.
• 선택지가 둘 이상이면 각각 장단점을 2~3개씩. 각 항목은 한 줄.
• 마지막에 *권고*: 어떤 조건이면 어느 쪽인지 명시한다.
• 판단의 전제나 확인이 필요한 사항이 있으면 한 줄로 덧붙인다.
한국어, 간결하게. 출처 줄은 시스템이 붙이므로 쓰지 않는다."""

MODEL_FLAG_RE = re.compile(r"--model=([A-Za-z0-9._\-]+)")
# 아카이브 범위 자체를 묻는 표현. 분류기가 out_of_scope 로 잘못 보내도 여기서 되돌린다.
ARCHIVE_SCOPE_RE = re.compile(
    r"(워크스페이스|채널|아카이브|자료|문서|기록|대화|수집|공유|본부|팀|현장|프로젝트)"
)
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _mentioned_workspaces(question: str) -> frozenset[str]:
    """질문에 명시된 워크스페이스 키·표시 이름을 비시크릿 환경설정에서 찾는다."""
    import os

    from .workspaces import env_suffix

    found: set[str] = set()
    keys = [key.strip() for key in (os.getenv("WORKSPACES") or "").split(",") if key.strip()]
    for key in keys:
        label = (os.getenv(f"WORKSPACE_LABEL_{env_suffix(key)}") or "").strip()
        if label and label in question:
            found.add(key)
            continue
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(key)}(?![A-Za-z0-9-])", question, re.I):
            found.add(key)
    return frozenset(found)


def _euro(word: str) -> str:
    """받침에 맞는 조사(으로/로). 한 글자 차이지만 매 답변에 보이는 문구다."""
    ch = (word or "").strip()[-1:]
    if not ch or not ("가" <= ch <= "힣"):
        return "로"
    jong = (ord(ch) - 0xAC00) % 28
    # 받침이 없거나 ㄹ 이면 '로'.
    return "로" if jong in (0, 8) else "으로"


@dataclass
class Answer:
    text: str
    citations: list[str]
    model: str | None
    cost_usd: float
    hit_count: int
    reason: str  # answered | advice | no_hits | no_access | smalltalk | out_of_scope | error
    # 사용자에게 보여줄 근거 요약용 검색어. 새로 저장하는 값이 아니라 이미 쓴 값이다.
    terms: list[str] = field(default_factory=list)

    @property
    def doc_count(self) -> int:
        return len(dict.fromkeys(self.citations))

    def evidence_note(self) -> str:
        """무엇으로 검색해 몇 건 중 몇 줄을 썼는지 한 줄.

        사내 피드백: "봇이 어떤 과정을 거쳐 이 답을 냈는지 알 수 없다." 값은 이미
        전부 가지고 있었고 **표시만 하지 않았다.** 이 줄이 "믿을 수 있나" 를
        "이 답이 맞나" 로 바꾼다 — 후자는 사람이 검증할 수 있는 질문이다.
        """
        if self.reason not in ("answered", "advice"):
            return ""
        bits: list[str] = []
        if self.terms:
            query = " ".join(self.terms)
            bits.append(f"「{query}」{_euro(query)} 검색")
        if self.doc_count:
            bits.append(f"문서 {self.doc_count}건")
        if self.hit_count:
            bits.append(f"원문 {self.hit_count}줄 사용")
        elif self.reason == "advice":
            # 근거가 0건인 판단은 그 사실이 가장 중요한 정보다.
            bits.append("아카이브 근거 없음")
        if self.model:
            bits.append(self.model)
        return f"_근거: {' · '.join(bits)}_" if bits else ""

    def to_slack(self) -> str:
        # Slack 에는 표 문법이 없다. 모델이 마크다운 표를 뱉으면 파이프가 그대로 보이고
        # 열이 어긋난다 - 프롬프트로 금지해도 새는 경우가 있어 여기서 다시 그린다.
        from .evidence_view import fix_markdown_tables

        parts = [fix_markdown_tables(self.text)]
        note = self.evidence_note()
        if note:
            parts.append(note)
        if self.citations:
            srcs = "\n".join(f"• {c}" for c in dict.fromkeys(self.citations))
            parts.append(f"출처:\n{srcs}")
        return "\n\n".join(parts)


def parse_model_flag(text: str) -> tuple[str | None, str]:
    """`--model=xxx 질문...` → (모델, 질문)."""
    m = MODEL_FLAG_RE.search(text)
    if not m:
        return None, text.strip()
    return m.group(1), (text[: m.start()] + text[m.end() :]).strip()


# 원문 줄에 남는 첨부 표시. Slack 원본 링크가 있으면 함께 보존한다.
ATTACHMENT_RE = re.compile(
    r"^\[첨부:[^\]]*\]\s*(?P<name>.+?)\s*\([^)]*\)"
    r"(?:\s*·\s*<(?P<url>[^>|]+)\|[^>]+>)?\s*$"
)
EXTRACTED_ATTACHMENT_RE = re.compile(r"^\[첨부(?:본문|추출):(?P<name>[^\]]+)\]")


def _attachment_names(hits: list[SearchHit]) -> list[tuple[str, str, str]]:
    """검색에 걸린 줄에서 (워크스페이스, 채널ID, 파일명)을 뽑는다.

    답변 근거로 이미 고른 문서의 첨부만 대상이다 - 검색과 무관한 파일을 원본으로
    올려보내지 않는다.
    """
    out: list[tuple[str, str, str]] = []
    for h in hits:
        m = ATTACHMENT_RE.match((h.line.text or "").strip())
        if not m:
            continue
        channel_id = h.doc.channel_id or ""
        if not channel_id:
            continue
        key = (h.doc.workspace, channel_id, m.group("name"))
        if key not in out:
            out.append(key)
    return out


def _attachment_source_links(hits: list[SearchHit]) -> list[str]:
    """추출문을 근거로 쓴 답변에 같은 문서의 Slack 원본 링크를 붙인다."""
    links: list[str] = []
    for hit in hits:
        hit_text = (hit.line.text or "").strip()
        direct = ATTACHMENT_RE.match(hit_text)
        if direct and direct.group("url"):
            citation = f"📎<{direct.group('url')}|{direct.group('name')} 원본>"
            if citation not in links:
                links.append(citation)
            continue
        extracted = EXTRACTED_ATTACHMENT_RE.match(hit_text)
        if not extracted:
            continue
        name = extracted.group("name")
        for line in hit.doc.raw_lines:
            marker = ATTACHMENT_RE.match((line.text or "").strip())
            if marker and marker.group("name") == name and marker.group("url"):
                citation = f"📎<{marker.group('url')}|{name} 원본>"
                if citation not in links:
                    links.append(citation)
                break
    return links


def _originals(store: ArchiveStore, hits: list[SearchHit]) -> documents.Attached:
    """검색에 걸린 첨부 중 **승인된** 원본만 모은다.

    승인 게이트가 유일한 안전장치다: 수집 단계 PII 거절은 텍스트 기반이라 스캔본에
    작동하지 않는다. 사람이 한 번 본 것만 벤더로 나간다.
    """
    approved = []
    for workspace, channel_id, name in _attachment_names(hits):
        item = find_approved(
            store.root, workspace=workspace, channel_id=channel_id, name=name
        )
        if item is not None:
            approved.append(item)
    return documents.collect(approved)


def _evidence_block(hits: list[SearchHit]) -> str:
    return "\n".join(
        f"[{h.line.ts}] ({h.doc.channel}) {h.line.speaker}: {h.line.text}" for h in hits
    )


class AnswerEngine:
    def __init__(
        self,
        store: ArchiveStore,
        router: Router,
        *,
        sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL,
        max_hits: int = 20,
        max_lines_per_channel: int = 60,
    ) -> None:
        self._store = store
        self._router = router
        self._sensitivity = sensitivity
        self._max_hits = max_hits
        self._max_lines_per_channel = max_lines_per_channel

    @classmethod
    def from_env(cls, archive_dir: str | Path, **kw) -> AnswerEngine:
        import os

        from .config import cost_state_path

        router = Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
            cost_state_path=cost_state_path(),
        )
        return cls(ArchiveStore(archive_dir), router, **kw)

    def model_info(self) -> str:
        return self._router.default_model

    def spent_today(self) -> float:
        return self._router.spent_today

    def summarize(
        self,
        ctx: RequestContext,
        *,
        days: int = 7,
        model: str | None = None,
        workspace_filter: frozenset[str] | None = None,
        question: str | None = None,
    ) -> Answer:
        """기간 요약 — 권한 내 전 채널의 최근 원문을 채널별로 정리한다.

        검색이 아니라 기간 스캔이므로, 근거는 여전히 원문 라인 그대로만 넣는다.
        """
        import datetime as _dt

        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        blocks: list[str] = []
        citations: list[str] = []
        total = 0
        visible_docs = [
            doc
            for doc in self._store.visible_docs(ctx)
            if not workspace_filter or doc.workspace in workspace_filter
        ]
        for doc in visible_docs:
            recent = [ln for ln in doc.raw_lines if TS_RE.match(ln.ts) and ln.ts[:10] >= cutoff]
            if not recent:
                continue
            recent = recent[-self._max_lines_per_channel :]
            total += len(recent)
            body = "\n".join(f"[{ln.ts}] {ln.speaker}: {ln.text}" for ln in recent)
            # 다른 워크스페이스 자료임을 근거와 출처 양쪽에 밝힌다.
            ws_tag = "" if doc.workspace == ctx.workspace else f"[{doc.workspace}] "
            blocks.append(f"### {ws_tag}채널 {doc.channel}\n{body}")
            date = recent[-1].ts.split()[0]
            citations.append(f"{ws_tag}{doc.channel}, 📄{doc.path.name}({date})")

        if not blocks:
            titles = [doc.channel for doc in visible_docs]
            if not titles:
                return Answer(
                    "열람 권한 범위에 아카이브된 문서가 없습니다. 채널에 봇을 초대하고 수집을 기다려 주세요.",
                    [], None, 0.0, 0, "no_access",
                )
            hint = ""
            if ctx.readable_workspaces:
                names = ", ".join(sorted(ctx.readable_workspaces))
                hint = (
                    f"\n\n참고: 다른 워크스페이스({names}) 자료는 상위(root) 워크스페이스로서 "
                    "전량 조회됩니다."
                    if ctx.is_root
                    else f"\n\n참고: 다른 워크스페이스({names}) 자료는 그쪽에서 "
                    "`share_with` 로 넘긴 문서만 조회됩니다."
                )
            return Answer(
                f"최근 {days}일 원문이 없습니다. 아카이브된 문서: "
                + ", ".join(titles[:20])
                + hint,
                [], None, 0.0, 0, "no_hits",
            )

        messages = [
            Message("system", SUMMARY_PROMPT),
            Message(
                "user",
                "<원문>\n"
                + "\n\n".join(blocks)
                + f"\n</원문>\n\n질문: {question or f'최근 {days}일 진행 상황을 정리해 주세요.'}",
            ),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=2048
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, total, "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, total, "error")

        logger.info(
            "summary ws=%s days=%d channels=%d lines=%d model=%s cost=$%.4f",
            ctx.workspace, days, len(blocks), total, resp.model, resp.cost_usd,
        )
        return Answer(resp.text.strip(), citations, resp.model, resp.cost_usd, total, "answered")

    def advise(
        self, question: str, ctx: RequestContext, *, terms: list[str] | None = None
    ) -> Answer:
        """판단·권고 요청 — 사내 사실은 원문만, 일반 판단은 LLM 지식 허용(라벨 부착).

        "출처 없으면 답하지 않는다"는 **사실 조회**의 규칙이다. 판단 요청에 그 규칙을 적용하면
        답을 못 하고, 반대로 라벨 없이 답하면 판단이 사내 사실로 오독된다. 그래서 둘을 분리한다.
        """
        model, q = parse_model_flag(question)
        query = " ".join(terms) if terms else q
        hits = self._store.search(query, ctx, limit=self._max_hits) if query else []

        evidence = (
            f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n"
            if hits
            else "<원문>\n(관련 원문 없음)\n</원문>\n\n"
        )
        messages = [
            Message("system", ADVICE_PROMPT),
            Message("user", f"{evidence}질문: {q}"),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=1500
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, len(hits), "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, 0, "error")

        # 라벨은 코드가 붙인다 — LLM 이 빼먹을 수 있는 것을 원칙에 맡기지 않는다.
        if hits:
            head = f"💬 판단 요청으로 답합니다. 아카이브 원문 {len(hits)}건을 근거로 참고했습니다.\n\n"
            citations = [
                h.citation(with_workspace=h.doc.workspace != ctx.workspace) for h in hits[:5]
            ]
            citations += _attachment_source_links(hits)
        else:
            head = "💬 판단 요청으로 답합니다. *아카이브에 관련 원문이 없어 일반적인 판단입니다* — 사내 사실 확인이 필요하면 원문을 따로 확인하세요.\n\n"
            citations = []

        logger.info(
            "advice ws=%s hits=%d model=%s cost=$%.4f q=%r",
            ctx.workspace, len(hits), resp.model, resp.cost_usd, q,
        )
        return Answer(
            head + resp.text.strip(), citations, resp.model, resp.cost_usd, len(hits),
            "advice", terms=list(terms or []),
        )

    def classify(self, question: str) -> Intent:
        """의도 분류(LLM, 실패 시 규칙). 라우팅만 하고 답은 만들지 않는다."""
        _, q = parse_model_flag(question)
        return classify(q, self._router)

    def plan(self, question: str) -> list[Intent]:
        """복합 질문을 하위질문 목록으로 분해한다(1차 LLM, 실패 시 규칙).

        라벨 하나만 돌려주던 `classify` 를 대체한다 - 사람은 한 번에 여러 가지를 묻고,
        예전 구조에서는 그중 하나만 처리 경로에 도달했다.
        """
        _, q = parse_model_flag(question)
        return plan(q, self._router)

    @property
    def router(self):
        """문장 생성(compose)용. 답변 엔진 밖에서도 같은 비용 상한을 쓰게 한다."""
        return self._router

    def respond(self, question: str, ctx: RequestContext, intent: Intent | None = None) -> Answer:
        """아카이브로 답할 수 있는 의도를 처리한다.

        status/help 는 봇 런타임 정보라 Slack 계층이 처리한다 — 여기로 오면 안내만 한다.
        """
        model, q = parse_model_flag(question)
        if not q:
            return Answer("질문 내용이 없습니다.", [], None, 0.0, 0, "error")
        intent = intent or classify(q, self._router)

        if intent.kind == "summary":
            return self.summarize(
                ctx,
                days=intent.days or DEFAULT_DAYS,
                model=model,
                workspace_filter=_mentioned_workspaces(q) or None,
                question=q,
            )
        if intent.kind == "advice":
            return self.advise(question, ctx, terms=intent.terms)
        if intent.kind == "smalltalk":
            return Answer(
                "네, 대기 중입니다. 아카이브에 쌓인 원문으로 답할 수 있는 걸 물어보세요. "
                "`도움말` 로 사용법을 볼 수 있습니다.",
                [], None, 0.0, 0, "smalltalk",
            )
        if intent.kind == "out_of_scope":
            # 분류기가 "다른 워크스페이스 내용 알려줘" 같은 범위 질문을 외부 정보로 오인하는 일이
            # 있었다. 아카이브 관련 표현이 있으면 거절하지 않고 기간 요약으로 되돌린다.
            if ARCHIVE_SCOPE_RE.search(q):
                logger.info("out_of_scope 재분류 -> summary q=%r", q)
                return self.summarize(
                    ctx,
                    days=parse_period(q),
                    model=model,
                    workspace_filter=_mentioned_workspaces(q) or None,
                    question=q,
                )
            return Answer(
                "사내 아카이브에 쌓인 원문만 근거로 답하는 봇입니다. "
                "일반 지식이나 외부 정보는 다루지 않습니다.",
                [], None, 0.0, 0, "out_of_scope",
            )
        if intent.kind in ("status", "help"):
            return Answer(
                "봇 상태·사용법은 `상태` / `도움말` 로 확인하세요.", [], None, 0.0, 0, intent.kind
            )
        return self.answer(question, ctx, terms=intent.terms)

    def answer(
        self, question: str, ctx: RequestContext, *, terms: list[str] | None = None
    ) -> Answer:
        """구체 사실 질문 — 원문 검색 후 그 라인만 근거로 답한다.

        terms 는 분류기가 뽑은 핵심어. 요청 표현("알려줘")이 검색을 오염시키는 걸 막는다.
        """
        model, q = parse_model_flag(question)
        if not q:
            return Answer("질문 내용이 없습니다.", [], None, 0.0, 0, "error")

        # 2겹: 색인이 아니라 원문 라인을 연다.
        query = " ".join(terms) if terms else q
        hits = self._store.search(query, ctx, limit=self._max_hits)

        if not hits:
            # 3겹: 근거가 없으면 **다른 질문에 답하지 않는다.** 예전엔 최근 원문 요약으로 폴백했는데,
            # 아카이브와 무관한 질문에도 그럴듯한 딴 얘기를 내놓아 더 나빴다.
            titles = self._store.titles(ctx)
            logger.info(
                "answer no_hits q=%r terms=%r ws=%s titles=%d", q, terms, ctx.workspace, len(titles)
            )
            if not titles:
                return Answer(
                    "열람 권한 범위에 아카이브된 문서가 없습니다. "
                    "채널에 봇을 초대(`/invite`)하고 대화가 쌓이길 기다려 주세요.",
                    [],
                    None,
                    0.0,
                    0,
                    "no_access",
                )
            listed = "\n".join(f"• {t}" for t in titles[:20])
            more = f"\n… 외 {len(titles) - 20}건" if len(titles) > 20 else ""
            return Answer(
                f"「{q}」에 해당하는 원문을 아카이브에서 찾지 못했습니다. 추측으로 답하지 않습니다.\n\n"
                f"열람 가능한 문서:\n{listed}{more}\n\n"
                "• 최근 대화 정리가 필요하면 `요약` 또는 `이번주 진행상황`\n"
                "• 판단·권고가 필요하면 그대로 물어보세요 (예: 「어느 방향이 나을까?」)\n"
                "• 봇 연결·수집 상태는 `상태`",
                [],
                None,
                0.0,
                0,
                "no_hits",
            )

        # 스캔 PDF·이미지는 우리 전처리로 읽히지 않는다. 승인된 원본이 있으면 그대로
        # 함께 보내 모델이 직접 읽게 한다. 전처리를 대체하는 게 아니라 - 어느 파일을
        # 볼지는 위 검색이 이미 골랐다 - 그 파일의 원본을 덧붙이는 것이다.
        attached = _originals(self._store, hits)
        prompt = f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n질문: {q}"
        user_content = (
            [*attached.blocks, {"type": "text", "text": prompt}]
            if attached.any
            else prompt
        )
        messages = [
            Message("system", SYSTEM_PROMPT),
            Message("user", user_content),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=1024
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, len(hits), "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, len(hits), "error")

        citations = [
            h.citation(with_workspace=h.doc.workspace != ctx.workspace) for h in hits[:5]
        ]
        citations += _attachment_source_links(hits)
        # 4겹: 질문·답변·근거를 전부 남긴다.
        logger.info(
            "answer ok ws=%s user=%s model=%s hits=%d cost=$%.4f q=%r srcs=%s",
            ctx.workspace,
            ctx.role,
            resp.model,
            len(hits),
            resp.cost_usd,
            q,
            citations,
        )
        # API 가 붙인 구조화된 인용(페이지 포함). 모델이 쓴 문장이 아니라서
        # 우리가 지어낸 출처가 아니라는 점이 중요하다.
        citations += documents.citation_lines(getattr(resp.raw, "content", None))
        body = resp.text.strip()
        note = attached.note()
        if note:
            body = f"{body}\n\n{note}"
        return Answer(
            body, citations, resp.model, resp.cost_usd, len(hits),
            "answered", terms=list(terms or []),
        )
