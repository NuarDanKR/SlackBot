"""마스터 판정 — **한 번의 구조화된 판정**으로 작업과 필요한 전문 능력을 정한다.

설계: [`docs/design/master-specialist-orchestration.md`](../../docs/design/master-specialist-orchestration.md)

## 왜 하나로 합쳤나

예전에는 한 질문이 확률적 판정을 **두 번** 거쳤다.

```text
intent.plan()            질문 분해 — 스레드 맥락을 본다
specialist_router.route() 전문 봇 선택 — 현재 질문 문자열만 본다
```

두 번째가 첫 번째의 결과를 못 봤다. 그래서 "내가 이전에 요청했던 내용을 다시
확인해줘" 는 근거가 미수금 문서로 복원돼도 **라우터는 그 사실을 모른 채** 모호한
문장만 보고 전문 봇을 골랐다(2026-09-13 검증). 게다가 두 번째 판정의 실패는 전부
`none` 으로 접혀 마스터 직접 답변이 됐다.

지금은 한 번 판정하고, 그 결과를 **코드가 검증**한다. LLM 이 만들 수 있는 것은
의미 판단뿐이다 — 능력 이름은 열거형에서만, 전문 봇 키는 지금 활성인 후보에서만,
부모 QA ID 는 이 스레드에 실제로 있는 것에서만 고를 수 있다.

## 무엇을 LLM 이 정하지 않나

기간(`여태까지` 를 7일로 바꾸는 것), 채널·DM 범위, 권한, 출처. 전부 결정적 코드가
원문 질문과 `RequestContext` 에서 계산한다. LLM 이 준 주제어와 문서 후보는
**검색 힌트일 뿐** 권한이나 source ID 가 아니다.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

from .intent import ARCHIVE_KINDS, SELF_KINDS, WRITE_KINDS, Intent

log = logging.getLogger("tybot.master_planner")

# --- 능력 열거형 --------------------------------------------------------------
#
# **코드가 제공한 이름만 허용한다.** 모델이 `slack_admin` 같은 능력을 지어내면
# 그 판정 전체를 믿을 수 없다 — 능력 이름은 곧 어느 봇이 답하느냐이기 때문이다.
INTERNAL_QA = "internal_document_qa"
INTERNAL_SUMMARY = "internal_document_summary"
LEGAL = "legal_analysis"
TAX = "tax_analysis"
CONSTRUCTION = "construction_analysis"

CAPABILITIES = (INTERNAL_QA, INTERNAL_SUMMARY, LEGAL, TAX, CONSTRUCTION)

# 작업 종류. `system`·`clarify` 만 TYBot 이 결정적 문구로 답할 수 있다.
SYSTEM = "system"
CLARIFY = "clarify"
FACTUAL = "factual"
SUMMARY = "summary"
ANALYSIS = "analysis"
ADVICE = "advice"
WRITE = "write"

TASK_KINDS = (SYSTEM, CLARIFY, FACTUAL, SUMMARY, ANALYSIS, ADVICE, WRITE)

# **전문 봇만 답할 수 있는 작업.** 근거에 기반한 업무 답변이 여기 전부 들어간다.
# 이 목록에 들어 있는 작업에서 마스터 LLM 이 문장을 만들면 정책 위반이다.
BUSINESS_KINDS = frozenset({FACTUAL, SUMMARY, ANALYSIS, ADVICE})

# 기존 `Intent.kind` → 작업 종류. 분류기 출력을 그대로 쓰지 않고 여기서 좁힌다.
KIND_MAP = {
    "search": FACTUAL,
    "summary": SUMMARY,
    "advice": ADVICE,
    "status": SYSTEM,
    "help": SYSTEM,
    "memory": SYSTEM,
    "smalltalk": SYSTEM,
    "out_of_scope": SYSTEM,
    "ingest": WRITE,
    "ingest_all": WRITE,
    "feedback": WRITE,
}

# 작업 종류 → 기본 능력. LLM 이 다른 능력을 제안할 수 있지만, 제안이 없거나
# 열거형 밖이면 이 표가 답이다.
DEFAULT_CAPABILITY = {
    FACTUAL: INTERNAL_QA,
    SUMMARY: INTERNAL_SUMMARY,
    ANALYSIS: INTERNAL_QA,
    ADVICE: INTERNAL_QA,
}


# --- 표시 산출물 (설계: pii-guardrail-and-canvas-artifacts.md §3) ---------------
#
# 「Canvas에 캘린더로 작성해줘」 는 **두 가지 요청**이다 — 근거에서 찾을 사실과,
# 그것을 어떻게 보여 줄지. 예전에는 이 문장이 통째로 전문 봇에 갔고, Hermes 는
# "Canvas 편집은 내 역할이 아니다" 라고 답했다. 근거 추출은 성공했는데 산출물
# 요청 때문에 답이 실패로 끝났다.
DELIVERY_MESSAGE = "message"
DELIVERY_CANVAS = "canvas"
DELIVERIES = (DELIVERY_MESSAGE, DELIVERY_CANVAS)

OP_ANSWER_DOCUMENT = "answer_document"
OP_EDIT_CANVAS = "edit_existing_canvas"
OP_CREATE_EVENTS = "create_calendar_events"
OPERATIONS = (OP_ANSWER_DOCUMENT, OP_EDIT_CANVAS, OP_CREATE_EVENTS)

# **지금 배포가 실제로 할 수 있는 것.** 나머지는 판정될 수는 있어도 실행되지
# 않는다 — 실행한 척하지 않고 지원 여부를 알린다(§3.1).
SUPPORTED_OPERATIONS = (OP_ANSWER_DOCUMENT,)

LAYOUTS = ("auto", "report", "table", "timeline", "calendar_grid")

# 금액 표시 단위. **사용자가 말한 것만** 받는다(§5.4).
UNITS = ("원", "천원", "백만원", "억원")

MAX_TITLE_CHARS = 60
# 예전 고정 제목. 이 값이 제목으로 돌아오면 아무것도 고른 게 아니다.
LEGACY_TITLE = "TYBot 정식 답변"

# 제목에 남기면 안 되는 것. 제목은 Slack 목록에 한 줄로 보이는 자리라, 문법이
# 섞이면 깨진 문자열로 보이고 링크가 섞이면 클릭 유도가 된다.
_TITLE_BANNED = re.compile(r"[\r\n\t<>|`*_#\[\]]|https?://|캔버스|canvas", re.IGNORECASE)
# 제목이 **새로 만든** 사실인지 보는 거친 검사. 숫자와 직함은 질문에 있던 것만.
_TITLE_NUMBER = re.compile(r"\d[\d,.]*")
# `김 부장` 처럼 **성 한 글자 + 직함**도 잡는다. 사람 이름은 제목에서 가장 조용히
# 새로 생기는 값이라, 넓게 잡고 질문에 있는지 확인하는 편이 낫다.
_TITLE_PERSON = re.compile(r"[가-힣]{1,4}\s*(부장|과장|차장|대리|사원|팀장|소장|이사|상무|전무|사장|대표)")


@dataclass(frozen=True)
class ArtifactRequest:
    """결과를 **어떻게 보여 줄지**. 무엇을 답할지가 아니다.

    값은 전부 코드가 검증한 뒤의 것이다. planner 가 준 원본은 `Intent` 에 남는다.
    """

    delivery: str = DELIVERY_MESSAGE
    operation: str = OP_ANSWER_DOCUMENT
    layout: str = "auto"
    title: str = ""
    title_source: str = "fallback"
    target_unit: str = ""
    # 판정은 됐지만 지금 실행할 수 없는 동작. **빈 문자열이 아니면 실행하지
    # 않고 사람에게 알린다** — 조용히 다른 것을 해 주면 한 일과 요청이 어긋난다.
    unsupported_operation: str = ""

    @property
    def wants_canvas(self) -> bool:
        """새 답변 Canvas 를 만들어야 하는가."""
        return (
            self.delivery == DELIVERY_CANVAS
            and self.operation == OP_ANSWER_DOCUMENT
            and not self.unsupported_operation
        )

    @property
    def canvas_title(self) -> str:
        """Canvas metadata 제목. 핵심은 AI 가 쓰고 **생성 주체 표식은 남긴다.**

        접미사는 `canvas_answer` 한 곳에서만 정의한다 — 만드는 쪽과 수집에서
        제외하는 쪽이 다른 값을 들면 우리가 만든 문서를 우리가 다시 수집한다.
        """
        from .canvas_answer import TITLE_SUFFIX

        return f"{self.title}{TITLE_SUFFIX}" if self.title else ""


def _clean_title(raw: str, *, question: str) -> tuple[str, str]:
    """(검증된 제목, 출처). 못 쓰면 빈 제목과 `fallback`.

    **고쳐서 쓰지 않는다.** 이상한 제목을 다듬어 넣으면 그 제목이 모델이 쓴
    것인지 우리가 만든 것인지 구별할 수 없게 된다.
    """
    original = raw or ""
    # **줄바꿈은 정규화 전에 본다.** `" ".join(split())` 이 먼저 지나가면 여러
    # 줄짜리 제목이 한 줄로 바뀌어 검사를 통과한다 — 제목은 한 줄이어야 한다.
    if _TITLE_BANNED.search(original):
        return "", "fallback"
    title = " ".join(original.split())
    if not title or len(title) > MAX_TITLE_CHARS:
        return "", "fallback"
    if title.startswith(LEGACY_TITLE):
        return "", "fallback"
    # 질문에 없던 숫자·직함을 제목이 **새로 만들면** 그것은 근거 없는 사실이다.
    asked = question or ""
    asked_numbers = set(_TITLE_NUMBER.findall(asked))
    if any(n not in asked_numbers for n in _TITLE_NUMBER.findall(title)):
        return "", "fallback"
    if any(m.group(0) not in asked for m in _TITLE_PERSON.finditer(title)):
        return "", "fallback"
    return title, "planner"


def _fallback_title(question: str, *, layout: str = "auto") -> str:
    """검증 가능한 결정적 대체 제목.

    질문을 그대로 자르면 ``Canvas에 ...`` 같은 산출물 지시나 Markdown 문법이
    메타데이터 제목으로 다시 들어갈 수 있다. AI 제목 검증이 실패한 경로에서도
    제목 계약은 동일하게 지킨다.
    """
    head = re.sub(r"https?://\S+", " ", question or "", flags=re.IGNORECASE)
    head = re.sub(r"캔버스|canvas", " ", head, flags=re.IGNORECASE)
    head = re.sub(r"[\r\n\t<>|`*_#\[\]]", " ", head)
    head = re.sub(r"\s*·\s*TYBot\s*$", "", head, flags=re.IGNORECASE)
    head = " ".join(head.split())[:40].strip(" ,.?!")
    if head and not head.startswith(LEGACY_TITLE) and not _TITLE_BANNED.search(head):
        return head
    defaults = {
        "calendar_grid": "업무 일정 정리",
        "timeline": "업무 일정 정리",
        "table": "업무 현황 정리",
        "report": "업무 보고",
    }
    return defaults.get(layout, "업무 답변")


def artifact_for(task: Intent, *, canvas_requested: bool = False) -> ArtifactRequest:
    """planner 제안 → 검증된 표시 요청.

    `canvas_requested` 는 사용자가 「캔버스로 답변해줘」 라고 **명시한** 경우다.
    LLM 이 죽어도 그 명시 요청은 살아 있어야 한다 — 규칙이 할 수 있는 것은
    거기까지다. `캘린더` 같은 표면형으로 layout 이나 쓰기 동작을 정하지 않는다.
    """
    delivery = (task.artifact_delivery or "").strip().lower()
    if delivery not in DELIVERIES:
        delivery = DELIVERY_CANVAS if canvas_requested else DELIVERY_MESSAGE
    elif canvas_requested:
        # 명시 요청은 판정보다 세다. 사용자가 말한 것을 모델 판정으로 덮지 않는다.
        delivery = DELIVERY_CANVAS

    operation = (task.artifact_operation or "").strip().lower()
    if operation not in OPERATIONS:
        operation = OP_ANSWER_DOCUMENT
    unsupported = "" if operation in SUPPORTED_OPERATIONS else operation
    if unsupported:
        log.info("지원하지 않는 산출물 동작 판정: %s", unsupported)

    layout = (task.artifact_layout or "").strip().lower()
    if layout not in LAYOUTS:
        layout = "auto"

    question = task.question or task.standalone_question or ""
    title, source = _clean_title(task.artifact_title, question=question)
    if not title:
        title = _fallback_title(task.research_question or question, layout=layout)

    unit = (task.target_unit or "").strip()
    if unit not in UNITS:
        unit = ""

    return ArtifactRequest(
        delivery=delivery,
        operation=operation,
        layout=layout,
        title=title,
        title_source=source,
        target_unit=unit,
        unsupported_operation=unsupported,
    )


@dataclass(frozen=True)
class MasterTask:
    """하나의 하위 요청. **범위를 넓히는 값은 하나도 들어 있지 않다.**"""

    kind: str
    # 사용자 원문 조각. 감사와 복합 질문 분리 확인용이다.
    original_fragment: str
    # 지칭을 풀어 쓴 문장. **전문 봇에는 이 값을 준다** — "그 문서" 를 그대로
    # 넘기면 전문 봇이 무엇을 찾아야 하는지 알 수 없다.
    standalone_question: str
    required_capability: str = ""
    suggested_specialist: str = ""
    routing_confidence: float = 0.0
    decision_id: str = ""
    task_index: int = 0
    editing_text: str = ""
    parent_record_ids: tuple[str, ...] = ()
    topic_terms: tuple[str, ...] = ()
    document_query: tuple[str, ...] = ()
    clarification: str = ""
    # **Canvas 생성 지시를 뺀** 사실 질문. 전문 봇에는 이 문장이 간다 — 산출물
    # 요청을 그대로 보내면 "그건 내 역할이 아니다" 만 돌아온다(설계 §3.3).
    research_question: str = ""
    # 결과를 어떻게 보여 줄지. 검증은 `artifact_for()` 가 이미 끝냈다.
    artifact: ArtifactRequest = field(default_factory=ArtifactRequest)
    # 기간·첨부 상태처럼 결정적 코드가 계산한 값을 그대로 들고 다닌다.
    intent: Intent | None = field(default=None, compare=False, repr=False)

    @property
    def is_business(self) -> bool:
        """전문 봇만 답할 수 있는 작업인가."""
        return self.kind in BUSINESS_KINDS

    @property
    def question(self) -> str:
        """전문 봇과 검색에 줄 문장.

        `research_question` 이 있으면 그것이 먼저다 — 산출물 지시가 빠진 문장이
        전문 봇에게 답할 수 있는 형태이기 때문이다.
        """
        return self.research_question or self.standalone_question or self.original_fragment


@dataclass(frozen=True)
class MasterDecision:
    tasks: tuple[MasterTask, ...]
    planner_model: str = ""
    decision_id: str = ""

    @property
    def business_tasks(self) -> tuple[MasterTask, ...]:
        return tuple(t for t in self.tasks if t.is_business)


def new_decision_id() -> str:
    """이 요청의 판정을 QA 기록·전문 봇 호출과 잇는 값. 본문은 담지 않는다."""
    return uuid.uuid4().hex[:16]


def capability_for(kind: str, proposed: str = "") -> str:
    """이 작업에 필요한 능력.

    LLM 제안은 **열거형 안일 때만** 받는다. 밖이면 조용히 기본값으로 돌아간다 —
    없는 능력을 요구하면 후보가 0이 되고, 그건 분류 실패가 아니라 답변 실패로
    나타나서 원인을 찾기 어렵다.
    """
    got = (proposed or "").strip().lower()
    if got in CAPABILITIES:
        return got
    if got:
        log.info("알 수 없는 능력 제안 무시: %r", got[:40])
    return DEFAULT_CAPABILITY.get(kind, "")


def _standalone(task: Intent, text: str, turns: list[dict] | None) -> str:
    """지칭이 남은 문장을 독립적으로 이해되게 만든다.

    순서가 중요하다.

    1. 분해기가 `standalone_question` 을 줬으면 그것이다.
    2. 안 줬어도 **원문과 다른 문장**을 줬으면 이미 푼 것이다 —
       "처리 안 된 문서도 다시 확인해줘" → "가정산서.pdf 다시 확인해줘".
    3. 둘 다 아니고 지칭 표현이 남아 있으면, 같은 스레드의 **이전 사용자 질문**을
       앞에 붙인다. 이전 *봇 답변* 이 아니다 — 답변 문장을 쓰면 요약을 근거로
       요약하는 길이 다시 열린다(원칙 1).

    3번을 조건 없이 하면 안 된다. 이미 풀린 문장에까지 이전 질문을 붙이면 묻지
    않은 주제가 검색어에 섞이고, 그게 곧 "관련 없는 자료가 답에 들어온다" 다.
    """
    from .intent import REFERENCE_RE

    explicit = (task.standalone_question or "").strip()
    if explicit:
        return explicit
    question = (task.question or "").strip()
    raw = (text or "").strip()
    if question and question != raw:
        return question
    current = question or raw
    if not REFERENCE_RE.search(current):
        return current
    prior = ""
    for turn in reversed(turns or []):
        got = str(turn.get("question") or "").strip()
        if got:
            prior = got
            break
    return f"{prior} — 이어서: {current}" if prior else current


def _research_question(task: Intent, standalone: str) -> str:
    """산출물 지시를 뺀 사실 질문.

    planner 가 줬으면 그것이다. 안 줬으면 **비워 둔다** — 정규식으로 「Canvas에」
    를 잘라 내면 반쯤 맞는 문장이 되고, 그 문장으로 검색하면 묻지 않은 자료가
    섞인다. 못 푼 것은 못 푼 대로 두고 원래 문장을 쓴다.
    """
    got = (task.research_question or "").strip()
    return got if got and got != standalone else ""


def from_intents(
    tasks: list[Intent],
    *,
    text: str,
    turns: list[dict] | None = None,
    planner_model: str = "",
    decision_id: str = "",
    canvas_requested: bool = False,
) -> MasterDecision:
    """분해 결과를 오케스트레이션 판정으로 옮긴다.

    **여기서 LLM 을 다시 부르지 않는다.** 필요한 의미 판단은 이미 분해 단계에서
    한 번 끝났고, 남은 것은 검증과 매핑이다.
    """
    allowed_parents = {
        str(turn.get("record_id") or "") for turn in (turns or []) if turn.get("record_id")
    }
    resolved_decision_id = decision_id or new_decision_id()
    out: list[MasterTask] = []
    for task_index, task in enumerate(tasks):
        kind = KIND_MAP.get(task.kind, SYSTEM)
        fragment = task.question or text
        # 부모 ID 는 **이 스레드에 실제로 있는 것만** 받는다. 모델이 지어낸 ID 가
        # 근거 복원 대상이 되면, 그 순간 권한 판정의 입력이 모델 출력이 된다.
        parents = tuple(
            pid for pid in (task.referenced_record_ids or ()) if pid in allowed_parents
        )
        standalone = (
            _standalone(task, text, turns) if kind in BUSINESS_KINDS else fragment
        )
        out.append(
            MasterTask(
                kind=kind,
                original_fragment=fragment,
                standalone_question=standalone,
                research_question=_research_question(task, standalone),
                artifact=artifact_for(task, canvas_requested=canvas_requested),
                required_capability=capability_for(kind, task.required_capability),
                suggested_specialist=(task.suggested_specialist or "").strip().lower(),
                routing_confidence=float(task.routing_confidence or 0.0),
                decision_id=resolved_decision_id,
                task_index=task_index,
                parent_record_ids=parents,
                topic_terms=tuple(task.topic_terms or ()),
                document_query=tuple(task.document_query or task.terms or ()),
                intent=task,
            )
        )
    return MasterDecision(
        tasks=tuple(out),
        planner_model=(
            planner_model
            or next((str(getattr(task, "planner_model", "") or "") for task in tasks
                     if getattr(task, "planner_model", "")), "")
        ),
        decision_id=resolved_decision_id,
    )


__all__ = [
    "ADVICE",
    "ANALYSIS",
    "ARCHIVE_KINDS",
    "BUSINESS_KINDS",
    "CAPABILITIES",
    "CLARIFY",
    "CONSTRUCTION",
    "DELIVERIES",
    "DELIVERY_CANVAS",
    "DELIVERY_MESSAGE",
    "FACTUAL",
    "INTERNAL_QA",
    "INTERNAL_SUMMARY",
    "LAYOUTS",
    "LEGAL",
    "OPERATIONS",
    "OP_ANSWER_DOCUMENT",
    "OP_CREATE_EVENTS",
    "OP_EDIT_CANVAS",
    "SELF_KINDS",
    "SUMMARY",
    "SUPPORTED_OPERATIONS",
    "SYSTEM",
    "TASK_KINDS",
    "TAX",
    "UNITS",
    "WRITE",
    "WRITE_KINDS",
    "ArtifactRequest",
    "MasterDecision",
    "MasterTask",
    "artifact_for",
    "capability_for",
    "from_intents",
    "new_decision_id",
]
