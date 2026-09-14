"""마스터는 판정하고, 업무 답변은 전문 봇만 쓴다.

설계: `docs/design/master-specialist-orchestration.md` §9

이 파일이 지키는 불변식 하나: **업무 답변 경로에서 마스터 LLM 호출이 0이다.**
전문 봇이 없든, DB 가 죽었든, 시간이 초과됐든, 계약을 어겼든 마찬가지다. 예전에는
그 전부가 마스터 직접 답변으로 접혔고, 그래서 고장이 정상 답으로 보였다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tybot import master_planner, specialist_router
from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import Intent
from tybot.specialist_router import Specialist, SpecialistAnswer, SpecialistOutcome

DOC = """---
workspace: pilot
channel: "#현장-광주도시철도(180901)-정산"
channel_id: C0GJ
visibility: private
acl: [#현장-광주도시철도(180901)-정산]
last_ingested: 2026-09-11T17:00+09:00
---

## 원문 (자동 취합, 편집 금지)
> [2026-09-10 10:00] 김수현: 광주도시철도 미수금액 3억 2천만원
> [2026-09-10 11:00] 김수현: 주간 보고 제출했습니다
"""


class CountingProvider:
    """마스터가 업무 답변을 만들면 여기 자국이 남는다."""

    name = "anthropic"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0, tools=None):
        self.calls.append(list(messages))
        return LLMResponse("마스터가 쓴 문장", spec.model, self.name, 10, 5, 0.0)


def _engine(tmp_path, hook):
    provider = CountingProvider()
    router = Router(
        providers={"anthropic": provider},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    path = tmp_path / "workspaces" / "pilot" / "channels" / "C0GJ__정산" / "raw" / "2026-09-10.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC, encoding="utf-8")
    return AnswerEngine(ArchiveStore(tmp_path), router, specialist=hook), provider


def _ctx():
    return RequestContext(
        workspace="pilot",
        channels=frozenset({"#현장-광주도시철도(180901)-정산"}),
        channel_id="C0GJ",
        channel="#현장-광주도시철도(180901)-정산",
    )


def _task(**kw):
    base = dict(
        kind=master_planner.FACTUAL,
        original_fragment="미수금 얼마야",
        standalone_question="미수금 얼마야",
        required_capability=master_planner.INTERNAL_QA,
        routing_confidence=0.9,
    )
    base.update(kw)
    return master_planner.MasterTask(**base)


def _row(**kw):
    base = dict(
        key="hermes", name="Hermes", domain="내부 기록", routing_hint="",
        adapter="hermes", model="", min_confidence=0.5, rules="",
        execution_mode="prompt",
    )
    base.update(kw)
    return Specialist(**base)


# =============================================================================
# §9.2 마스터 비답변 불변식
# =============================================================================


@pytest.mark.parametrize(
    "outcome",
    [
        SpecialistOutcome(specialist_router.UNAVAILABLE, error_code="no-specialist-registered"),
        SpecialistOutcome(specialist_router.REGISTRY_ERROR, error_code="registry-unavailable"),
        SpecialistOutcome(specialist_router.NO_CAPABILITY, error_code="no-specialist-for:legal_analysis"),
        SpecialistOutcome(specialist_router.UNAVAILABLE, error_code="timeout"),
        SpecialistOutcome(specialist_router.UNAVAILABLE, error_code="adapter-error"),
        SpecialistOutcome(specialist_router.UNAVAILABLE, error_code="empty-output"),
        SpecialistOutcome(specialist_router.UNAVAILABLE, error_code="contract-violation"),
    ],
    ids=[
        "전문봇없음", "DB오류", "능력없음", "시간초과", "어댑터오류", "빈출력", "계약위반",
    ],
)
def test_no_failure_becomes_a_master_business_answer(tmp_path, outcome):
    engine, provider = _engine(tmp_path, lambda task, ctx, ev, **kw: outcome)

    answer = engine.answer("미수금 얼마야", _ctx(), terms=["미수금"], task=_task())

    assert answer.reason == "specialist_unavailable"
    assert outcome.error_code in answer.text, "사유 코드가 보여야 사람이 고친다"
    assert provider.calls == [], "마스터가 업무 답변을 만들었다"
    assert answer.specialist == ""
    assert answer.specialist_error_code == outcome.error_code


def test_a_low_confidence_routing_asks_instead_of_answering(tmp_path):
    """신뢰도 미달은 **장애가 아니다.** 되묻되, 마스터가 대신 답하지는 않는다."""
    outcome = SpecialistOutcome(
        specialist_router.CLARIFY, error_code="low-confidence",
        clarification="어느 현장을 말씀하시나요?",
    )
    engine, provider = _engine(tmp_path, lambda task, ctx, ev, **kw: outcome)

    answer = engine.answer("미수금 얼마야", _ctx(), terms=["미수금"], task=_task())

    assert answer.reason == "clarify"
    assert "어느 현장" in answer.text
    assert provider.calls == []


def test_zero_confidence_never_becomes_the_minimum_passing_score(monkeypatch):
    """누락·실패를 뜻하는 0은 falsy 우회로 전문 봇에 들어가면 안 된다."""
    monkeypatch.setattr(specialist_router, "available", lambda workspace: [_row()])
    monkeypatch.setattr(
        specialist_router,
        "_run_one",
        lambda *args, **kwargs: pytest.fail("신뢰도 0인데 전문 봇을 호출했다"),
    )

    outcome = specialist_router.serve(
        _task(routing_confidence=0.0),
        workspace="pilot",
        evidence=["원문"],
        router=None,
        authorization_id="pilot:member",
    )

    assert outcome.status == specialist_router.CLARIFY
    assert outcome.error_code == "low-confidence"


def test_rule_fallback_confidence_is_closed_by_the_real_router(monkeypatch):
    """planner 장애의 규칙 폴백도 실제 선택 계층에서 실행되지 않는다."""
    from tybot import intent

    planned = intent.plan("미수금 현황을 알려줘", None)
    decision = master_planner.from_intents(planned, text="미수금 현황을 알려줘")
    monkeypatch.setattr(specialist_router, "available", lambda workspace: [_row()])

    outcome = specialist_router.serve(
        decision.tasks[0],
        workspace="pilot",
        evidence=["원문"],
        router=None,
        authorization_id="pilot:member",
    )

    assert decision.tasks[0].routing_confidence == 0.0
    assert outcome.status == specialist_router.CLARIFY


def test_tools_success_without_any_touched_evidence_is_not_an_answer(monkeypatch):
    """그럴듯한 문장이 있어도 실제로 읽은 근거가 없으면 근거 부족이다."""
    from tybot import specialist_adapters

    class EmptyEvidenceAdapter:
        touched = SimpleNamespace(documents=(), live_permalinks=())
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            return "관련 자료가 없습니다."

    monkeypatch.setattr(
        specialist_router,
        "available",
        lambda workspace: [_row(execution_mode="tools")],
    )
    monkeypatch.setattr(
        specialist_adapters,
        "build",
        lambda *args, **kwargs: EmptyEvidenceAdapter(),
    )

    outcome = specialist_router.serve(
        _task(),
        workspace="pilot",
        evidence=[],
        router=None,
        authorization_id="pilot:member",
        toolbox_factory=object,
        record_call_row=False,
    )

    assert outcome.status == specialist_router.EVIDENCE_INSUFFICIENT
    assert outcome.error_code == "evidence_insufficient"
    assert outcome.answer is None


def test_specialist_call_keeps_decision_and_task_coordinates(monkeypatch):
    from tybot import specialist_adapters
    from tybot.console import specialist_store

    class Adapter:
        touched = SimpleNamespace(documents=(), live_permalinks=())
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            return "근거에 따른 답"

    written = []
    monkeypatch.setattr(specialist_router, "available", lambda workspace: [_row()])
    monkeypatch.setattr(
        specialist_adapters, "build", lambda *args, **kwargs: Adapter()
    )
    monkeypatch.setattr(specialist_store, "record_call", lambda **kwargs: written.append(kwargs))
    task = _task(decision_id="abc123", task_index=2)

    outcome = specialist_router.serve(
        task,
        workspace="pilot",
        evidence=["원문"],
        router=None,
        authorization_id="pilot:member",
    )

    assert outcome.status == specialist_router.SUCCESS
    assert written[0]["decision_id"] == "abc123"
    assert written[0]["task_index"] == 2
    assert written[0]["task_kind"] == "factual"
    assert written[0]["required_capability"] == "internal_document_qa"


def test_a_source_outside_the_acl_is_discarded_without_a_master_rewrite(tmp_path):
    """권한 밖 출처를 든 답은 버린다. **마스터가 대신 쓰지도 않는다.**"""
    other = SimpleNamespace(workspace="other", path=tmp_path / "남의문서.md")
    answer_obj = SpecialistAnswer("남의 자료로 답합니다", "hermes", "m", 0.0, documents=(other,))
    outcome = SpecialistOutcome(specialist_router.SUCCESS, answer=answer_obj)
    engine, provider = _engine(tmp_path, lambda task, ctx, ev, **kw: outcome)

    answer = engine.answer("미수금 얼마야", _ctx(), terms=["미수금"], task=_task())

    assert "남의 자료" not in answer.text
    assert answer.reason == "specialist_unavailable"
    assert answer.specialist_error_code == "acl-source-violation"
    assert provider.calls == []


@pytest.mark.parametrize("kind", ["factual", "summary", "advice"])
def test_a_successful_business_answer_always_names_its_specialist(tmp_path, kind):
    """성공한 업무 답변에는 **반드시** 전문 봇 이름이 있다."""
    outcome = SpecialistOutcome(
        specialist_router.SUCCESS,
        answer=SpecialistAnswer("정리했습니다.", "hermes", "m", 0.0),
        attempted=("hermes",),
    )
    engine, provider = _engine(tmp_path, lambda task, ctx, ev, **kw: outcome)
    ctx = _ctx()

    if kind == "factual":
        answer = engine.answer("미수금 얼마야", ctx, terms=["미수금"], task=_task())
    elif kind == "summary":
        answer = engine.summarize(ctx, days=3650, question="정리해줘", task=_task())
    else:
        answer = engine.advise("어느 쪽이 나을까", ctx, terms=["미수금"], task=_task())

    assert answer.specialist == "hermes"
    assert answer.attempted_specialists == ["hermes"]
    assert provider.calls == [], "마스터가 업무 답변을 만들었다"


# =============================================================================
# §9.1 맥락과 분류
# =============================================================================


def test_a_dangling_reference_becomes_a_standalone_question():
    """전문 봇에는 **풀린 문장**이 간다. "이전에 요청했던 내용" 으로는 못 찾는다."""
    turns = [{"record_id": "r1", "question": "광주도시철도 미수금 현황을 확인해줘"}]
    tasks = [Intent("search", question="내가 이전에 요청했던 내용을 다시 확인해줘")]

    decision = master_planner.from_intents(
        tasks, text="내가 이전에 요청했던 내용을 다시 확인해줘", turns=turns
    )

    (task,) = decision.tasks
    assert "미수금" in task.standalone_question
    assert task.original_fragment == "내가 이전에 요청했던 내용을 다시 확인해줘"


def test_a_planner_resolved_question_is_not_prefixed_again():
    """분해기가 이미 풀었으면 이전 질문을 덧붙이지 않는다.

    조건 없이 붙이면 묻지 않은 주제가 검색어에 섞인다.
    """
    turns = [{"record_id": "r1", "question": "첨부 문서 내용 알려줘"}]
    tasks = [Intent("search", question="가정산서.pdf 다시 확인해줘")]

    decision = master_planner.from_intents(
        tasks, text="처리 안 된 그 문서 다시 확인해줘", turns=turns
    )

    assert decision.tasks[0].standalone_question == "가정산서.pdf 다시 확인해줘"


def test_the_same_weekly_report_question_asks_for_the_same_capability():
    """같은 의미의 두 표현이 같은 능력으로 간다. 흔들리면 답도 흔들린다."""
    a = master_planner.from_intents(
        [Intent("summary", question="주간 보고 내용을 종합해줘")],
        text="주간 보고 내용을 종합해줘",
    )
    b = master_planner.from_intents(
        [Intent("summary", question="여태까지 수집된 주간 보고를 정리해줘")],
        text="여태까지 수집된 주간 보고를 정리해줘",
    )

    assert a.tasks[0].required_capability == b.tasks[0].required_capability
    assert a.tasks[0].required_capability == master_planner.INTERNAL_SUMMARY


def test_a_made_up_capability_falls_back_to_the_enum():
    """모델이 능력 이름을 지어내면 무시한다. 없는 능력은 후보 0이 된다."""
    task = Intent("search", question="q", required_capability="slack_admin")

    decision = master_planner.from_intents([task], text="q")

    assert decision.tasks[0].required_capability == master_planner.INTERNAL_QA


def test_a_parent_record_id_outside_the_thread_is_refused():
    """LLM 이 만든 부모 ID 가 근거 복원 대상이 되면 권한 입력이 모델 출력이 된다."""
    task = Intent("search", question="q", referenced_record_ids=["없는ID", "r1"])
    turns = [{"record_id": "r1", "question": "이전 질문"}]

    decision = master_planner.from_intents([task], text="q", turns=turns)

    assert decision.tasks[0].parent_record_ids == ("r1",)


def test_each_subquestion_keeps_its_own_standalone_question():
    tasks = [
        Intent(
            "search", question="김해외동 기성금 얼마야",
            planner_model="claude-haiku-4-5",
        ),
        Intent("summary", question="전산팀 진행 상황"),
    ]

    decision = master_planner.from_intents(tasks, text="둘 다 알려줘")

    assert len(decision.tasks) == 2
    assert decision.tasks[0].standalone_question == "김해외동 기성금 얼마야"
    assert decision.tasks[1].standalone_question == "전산팀 진행 상황"
    assert decision.planner_model == "claude-haiku-4-5"
    assert [task.task_index for task in decision.tasks] == [0, 1]
    assert all(task.decision_id == decision.decision_id for task in decision.tasks)


# =============================================================================
# §5.1 결정적 선택
# =============================================================================


def test_selection_is_deterministic_and_ignores_invented_keys(monkeypatch):
    monkeypatch.setattr(
        specialist_router, "capabilities_of",
        lambda s: ("internal_document_qa",) if s.key != "legalbot" else ("legal_analysis",),
    )
    roster = [_row(key="zeta"), _row(key="alpha"), _row(key="legalbot")]

    chain = specialist_router.select(_task(), roster)

    assert [c.key for c in chain] == ["alpha", "zeta"], "같은 입력이면 같은 순서"

    # 목록 밖 이름은 무시된다.
    chain = specialist_router.select(_task(suggested_specialist="존재하지않음"), roster)
    assert [c.key for c in chain] == ["alpha", "zeta"]

    # 후보 안에 있는 제안만 앞으로 당긴다.
    chain = specialist_router.select(_task(suggested_specialist="zeta"), roster)
    assert [c.key for c in chain] == ["zeta", "alpha"]


def test_a_failed_candidate_hands_off_to_the_next_approved_one(monkeypatch):
    """첫 봇이 실패하면 **같은 능력의 다음 승인 봇**을 시도한다(설계 §5.2)."""
    from tybot import specialist_adapters

    class Boom:
        touched = None
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            raise RuntimeError("터짐")

    class Fine:
        touched = None
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            return "두 번째가 답했습니다."

    def fake_build(key, router, **kw):
        return Boom() if key == "alpha" else Fine()

    monkeypatch.setattr(specialist_adapters, "build", fake_build)
    monkeypatch.setattr(
        specialist_router, "available",
        lambda ws: [_row(key="alpha", adapter="alpha"), _row(key="beta", adapter="beta")],
    )
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )

    outcome = specialist_router.serve(
        _task(), workspace="pilot", evidence=["원문"], router=None,
        authorization_id="pilot:member", record_call_row=False,
    )

    assert outcome.ok
    assert outcome.selected == "beta"
    assert outcome.attempted == ("alpha", "beta")


def test_every_candidate_is_tried_at_most_once(monkeypatch):
    """무한히 돌면 한 질문이 그날 전체 답변을 느리게 만든다."""
    from tybot import specialist_adapters

    tried: list[str] = []

    class Boom:
        touched = None
        last_model = ""
        last_cost_usd = 0.0

        def complete(self, request):
            raise RuntimeError("터짐")

    def fake_build(key, router, **kw):
        tried.append(key)
        return Boom()

    monkeypatch.setattr(specialist_adapters, "build", fake_build)
    monkeypatch.setattr(
        specialist_router, "available",
        lambda ws: [_row(key="alpha", adapter="alpha"), _row(key="beta", adapter="beta")],
    )
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )

    outcome = specialist_router.serve(
        _task(), workspace="pilot", evidence=["원문"], router=None,
        authorization_id="pilot:member", record_call_row=False,
    )

    assert tried == ["alpha", "beta"]
    assert outcome.status == specialist_router.UNAVAILABLE


# =============================================================================
# §6.5 시각 근거
# =============================================================================


def test_a_visual_question_goes_to_a_visual_capable_specialist(monkeypatch):
    from tybot import specialist_adapters

    seen = {}

    class FakeAdapter:
        touched = None
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            seen["visual"] = request.visual
            return "표를 읽었습니다."

    monkeypatch.setattr(specialist_adapters, "build", lambda key, router, **kw: FakeAdapter())
    monkeypatch.setattr(specialist_adapters, "supports_visual", lambda key: True)
    monkeypatch.setattr(specialist_router, "available", lambda ws: [_row()])
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )

    outcome = specialist_router.serve(
        _task(), workspace="pilot", evidence=["원문"], router=None,
        authorization_id="pilot:member", record_call_row=False,
        visual=({"type": "image"},),
    )

    assert outcome.ok
    assert seen["visual"] == ({"type": "image"},)


def test_no_visual_capable_specialist_closes_instead_of_letting_the_master_read(monkeypatch):
    """**마스터가 이미지를 대신 읽지 않는다.** 못 읽으면 못 읽는다고 말한다."""
    from tybot import specialist_adapters

    monkeypatch.setattr(specialist_adapters, "supports_visual", lambda key: False)
    monkeypatch.setattr(specialist_router, "available", lambda ws: [_row()])
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )

    outcome = specialist_router.serve(
        _task(), workspace="pilot", evidence=["원문"], router=None,
        authorization_id="pilot:member", record_call_row=False,
        visual=({"type": "image"},),
    )

    assert outcome.status == specialist_router.NO_CAPABILITY
    assert outcome.error_code == "visual-unsupported"


# =============================================================================
# §3-C 검색 예산
# =============================================================================


def test_the_tool_budget_stops_an_endless_search_without_claiming_absence():
    """예산 소진과 **자료 없음**을 구별한다. 둘을 섞으면 "없다" 가 거짓이 된다."""
    from tybot.specialist_tools import ToolBudget

    budget = ToolBudget(max_calls=2)

    assert budget.refuse("search") == ""
    budget.spend("search", "결과")
    assert budget.refuse("search") == ""
    budget.spend("search", "결과")

    refusal = budget.refuse("search")
    assert "예산" in refusal
    assert "단정하지 말고" in refusal, "「없다」 로 가지 말라고 말해야 한다"
    assert budget.exhausted
    assert budget.reason == "calls"


def test_one_noisy_tool_does_not_spend_the_whole_budget():
    """한 도구만 맴도는 것과 예산을 다 쓴 것은 다르다."""
    from tybot.specialist_tools import ToolBudget

    budget = ToolBudget(max_calls=99, max_per_tool=2)
    for _ in range(2):
        budget.spend("search", "x")

    refusal = budget.refuse("search")

    assert "다른 도구" in refusal
    assert not budget.exhausted, "전체 예산은 아직 남아 있다"


def test_the_budget_summary_carries_no_business_text():
    """추적에 업무 본문이 들어가면 감사 기록이 원문 사본이 된다."""
    from tybot.specialist_tools import ToolBudget

    budget = ToolBudget()
    budget.spend("search", "광주도시철도 미수금 3억")

    summary = budget.summary()

    assert "광주도시철도" not in summary
    assert "tool_calls=1" in summary


# =============================================================================
# §9.5 감사
# =============================================================================


def test_the_audit_record_names_the_final_responder(tmp_path):
    from tybot.audit import QALog, QARecord

    log = QALog(tmp_path, write_md=False)
    log.write(
        QARecord.build(
            workspace="pilot", channel="#c", channel_id="C1", user="U1",
            user_name="홍길동", question="미수금", intent_kind="search",
            intent_source="llm", reason="answered", hits=1, scope="현재 채널",
            citations=[], model="m", cost_usd=0.0, elapsed_ms=1, answer="답",
            request_ts="1", response_ts="2", thread_ts="T1", channel_type="channel",
            error="", decision_id="abc123", required_capability="internal_document_qa",
            final_responder="hermes", attempted_specialists=["hermes"],
            specialist_error_code="", planner_model="claude-haiku-4-5",
            task_traces=[{
                "task_index": 0, "task_kind": "factual",
                "required_capability": "internal_document_qa",
                "routing_confidence": 0.9, "final_responder": "hermes",
                "attempted_specialists": ["hermes"], "result": "answered",
                "error_code": "",
            }],
        )
    )

    row = log.find_answer("pilot", "C1", thread_ts="T1")

    assert row["decision_id"] == "abc123"
    assert row["final_responder"] == "hermes"
    assert row["attempted_specialists"] == ["hermes"]
    assert row["planner_model"] == "claude-haiku-4-5"
    assert row["task_traces"][0]["task_kind"] == "factual"


def test_the_specialist_outcome_log_line_has_no_business_text():
    outcome = SpecialistOutcome(
        specialist_router.UNAVAILABLE, error_code="timeout",
        attempted=("hermes",), decision_id="abc123",
    )

    line = outcome.log_line()

    assert "hermes" in line and "timeout" in line and "abc123" in line
    assert "미수금" not in line
