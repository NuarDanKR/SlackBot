"""질의응답 파이프라인 — 환각방지 4겹을 코드로 강제한다.

순서(바꾸지 말 것): 권한 필터 → 원문 검색 → (0건이면 목록만, LLM 호출 안 함) → LLM → 출처 부착 → 로깅.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .access import RequestContext
from .archive.store import ArchiveStore, SearchHit
from .intent import DEFAULT_DAYS, Intent, classify
from .gateway.base import Message, Sensitivity
from .gateway.cost import CostLimitExceeded
from .gateway.router import ModelNotAllowed, Router, UnknownModel

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

MODEL_FLAG_RE = re.compile(r"--model=([A-Za-z0-9._\-]+)")
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

@dataclass
class Answer:
    text: str
    citations: list[str]
    model: str | None
    cost_usd: float
    hit_count: int
    reason: str  # answered | no_hits | no_access | error

    def to_slack(self) -> str:
        if not self.citations:
            return self.text
        srcs = "\n".join(f"• {c}" for c in dict.fromkeys(self.citations))
        return f"{self.text}\n\n출처:\n{srcs}"


def parse_model_flag(text: str) -> tuple[str | None, str]:
    """`--model=xxx 질문...` → (모델, 질문)."""
    m = MODEL_FLAG_RE.search(text)
    if not m:
        return None, text.strip()
    return m.group(1), (text[: m.start()] + text[m.end() :]).strip()


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
    def from_env(cls, archive_dir: str | Path, **kw) -> "AnswerEngine":
        import os

        router = Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
        )
        return cls(ArchiveStore(archive_dir), router, **kw)

    def model_info(self) -> str:
        return self._router.default_model

    def spent_today(self) -> float:
        return self._router.spent_today

    def summarize(self, ctx: RequestContext, *, days: int = 7, model: str | None = None) -> Answer:
        """기간 요약 — 권한 내 전 채널의 최근 원문을 채널별로 정리한다.

        검색이 아니라 기간 스캔이므로, 근거는 여전히 원문 라인 그대로만 넣는다.
        """
        import datetime as _dt

        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
        blocks: list[str] = []
        citations: list[str] = []
        total = 0
        for doc in self._store.visible_docs(ctx):
            recent = [ln for ln in doc.raw_lines if TS_RE.match(ln.ts) and ln.ts[:10] >= cutoff]
            if not recent:
                continue
            recent = recent[-self._max_lines_per_channel :]
            total += len(recent)
            body = "\n".join(f"[{ln.ts}] {ln.speaker}: {ln.text}" for ln in recent)
            blocks.append(f"### 채널 {doc.channel}\n{body}")
            date = recent[-1].ts.split()[0]
            citations.append(f"{doc.channel}, 📄{doc.path.name}({date})")

        if not blocks:
            titles = self._store.titles(ctx)
            if not titles:
                return Answer(
                    "열람 권한 범위에 아카이브된 문서가 없습니다. 채널에 봇을 초대하고 수집을 기다려 주세요.",
                    [], None, 0.0, 0, "no_access",
                )
            return Answer(
                f"최근 {days}일 원문이 없습니다. 아카이브된 문서: " + ", ".join(titles[:20]),
                [], None, 0.0, 0, "no_hits",
            )

        messages = [
            Message("system", SUMMARY_PROMPT),
            Message(
                "user",
                "<원문>\n"
                + "\n\n".join(blocks)
                + f"\n</원문>\n\n최근 {days}일 진행 상황을 정리해 주세요.",
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

    def classify(self, question: str) -> Intent:
        """의도 분류(LLM, 실패 시 규칙). 라우팅만 하고 답은 만들지 않는다."""
        _, q = parse_model_flag(question)
        return classify(q, self._router)

    def respond(self, question: str, ctx: RequestContext, intent: Intent | None = None) -> Answer:
        """아카이브로 답할 수 있는 의도를 처리한다.

        status/help 는 봇 런타임 정보라 Slack 계층이 처리한다 — 여기로 오면 안내만 한다.
        """
        model, q = parse_model_flag(question)
        if not q:
            return Answer("질문 내용이 없습니다.", [], None, 0.0, 0, "error")
        intent = intent or classify(q, self._router)

        if intent.kind == "summary":
            return self.summarize(ctx, days=intent.days or DEFAULT_DAYS, model=model)
        if intent.kind == "smalltalk":
            return Answer(
                "네, 대기 중입니다. 아카이브에 쌓인 원문으로 답할 수 있는 걸 물어보세요. "
                "`도움말` 로 사용법을 볼 수 있습니다.",
                [], None, 0.0, 0, "smalltalk",
            )
        if intent.kind == "out_of_scope":
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
                "• 봇 연결·수집 상태는 `상태`",
                [],
                None,
                0.0,
                0,
                "no_hits",
            )

        messages = [
            Message("system", SYSTEM_PROMPT),
            Message("user", f"<원문>\n{_evidence_block(hits)}\n</원문>\n\n질문: {q}"),
        ]
        try:
            resp = self._router.complete(
                messages, model=model, sensitivity=self._sensitivity, max_tokens=1024
            )
        except (UnknownModel, ModelNotAllowed) as e:
            return Answer(f"모델 선택 오류: {e}", [], model, 0.0, len(hits), "error")
        except CostLimitExceeded as e:
            return Answer(f"오늘 LLM 사용 한도에 도달했습니다. ({e})", [], model, 0.0, len(hits), "error")

        citations = [h.citation() for h in hits[:5]]
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
        return Answer(resp.text.strip(), citations, resp.model, resp.cost_usd, len(hits), "answered")
