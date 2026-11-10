"""복합 질문 분해(1차 LLM) + 봇 자기설명 답변의 문장 생성.

실제 사고: "너가 예전에 했던 말 기억나? 그리고 지금 전산팀 워크스페이스에서는 무슨일이
벌어지고 있어?" 에 봇이 고정 문단(기억하지 않는다는 설명)만 내보내고 **두 번째 질문은
처리 경로에 도달조차 하지 못했다.** 분류기가 라벨 하나만 돌려주는 구조였기 때문이다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tybot.answer import AnswerEngine
from tybot.compose import join_sections, truncated_notice, write_from_facts
from tybot.intent import (
    ARCHIVE_KINDS,
    KINDS,
    MAX_TASKS,
    SELF_KINDS,
    WRITE_KINDS,
    Intent,
    apply_dm_feedback_context,
    carry_dm_subject_context,
    plan,
)


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.model = "fake"
        self.cost_usd = 0.0


class FakeRouter:
    """라우터 대역. 무엇을 요청했는지 기록해 프롬프트 계약을 검증한다."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list] = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        if not self._replies:
            raise AssertionError("예상보다 많이 호출됐다")
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r)


def _tasks(*items) -> str:
    return json.dumps({"tasks": list(items)}, ensure_ascii=False)


# --- 분해 -------------------------------------------------------------------
def test_llm_split_returns_two_tasks():
    router = FakeRouter([
        _tasks(
            {"kind": "memory", "question": "예전에 했던 말 기억나?"},
            {"kind": "summary", "question": "전산팀 워크스페이스 무슨일", "days": 7},
        )
    ])
    tasks = plan("예전에 했던 말 기억나? 그리고 전산팀 워크스페이스 무슨일 있어?", router)
    assert [t.kind for t in tasks] == ["memory", "summary"]
    assert tasks[1].question == "전산팀 워크스페이스 무슨일"
    assert all(t.source == "llm" for t in tasks)


def test_format_followup_keeps_planner_decision():
    from tybot.intent import Intent, apply_followup

    task = Intent("summary", routing_confidence=0.9, planner_model="planner",
                  suggested_specialist="hermes", required_capability="internal_document_summary")
    (got,) = apply_followup("이전 답변 형식을 bullet point 형식으로 바꿔서 답해줘",
                            [task], has_prior=True)
    assert got.format_only
    assert got.reference_mode == "prior_turn"
    assert got.topic_terms == []
    assert got.routing_confidence == 0.9
    assert got.suggested_specialist == "hermes"
    assert got.planner_model == "planner"


def test_unknown_kind_is_dropped_not_guessed():
    router = FakeRouter([
        _tasks({"kind": "정체불명"}, {"kind": "status", "question": "상태"})
    ])
    tasks = plan("뭔가 이상한 질문", router)
    assert [t.kind for t in tasks] == ["status"]


def test_all_kinds_unknown_falls_back_to_rule():
    router = FakeRouter([_tasks({"kind": "정체불명"})])
    tasks = plan("김해외동 기성금 얼마야", router)
    assert tasks[0].source == "regex"
    assert "김해외동" in tasks[0].terms


def test_llm_failure_falls_back_to_rule():
    """오늘의 401 상황 - 분해가 죽어도 봇은 답해야 한다."""
    router = FakeRouter([RuntimeError("401 authentication_error"), RuntimeError("또 실패")])
    tasks = plan("현재 상태", router)
    assert [t.kind for t in tasks] == ["status"]
    assert tasks[0].source == "regex"


def test_broken_json_falls_back_to_rule():
    router = FakeRouter(["이건 JSON 이 아니다"])
    tasks = plan("현재 상태", router)
    assert tasks[0].kind == "status"
    assert tasks[0].source == "regex"


def test_write_intent_is_isolated():
    """수집 지시가 섞이면 그것만 실행한다 - 모호한 상태로 쓰기를 실행하지 않는다."""
    router = FakeRouter([
        _tasks({"kind": "summary", "question": "요약"}, {"kind": "ingest", "question": "수집해"})
    ])
    tasks = plan("요약하고 수집해", router)
    assert [t.kind for t in tasks] == ["ingest"]


def test_completed_upload_statement_cannot_authorize_ingest():
    router = FakeRouter([
        _tasks({"kind": "ingest", "question": "PCM 들어간 자료 업로드해놨어"})
    ])

    (task,) = plan("2025년도 자료에는 없어? PCM 들어간 자료 업로드해놨어", router)

    assert task.kind == "search"
    assert "PCM" in task.terms


def test_an_explicit_collection_command_still_authorizes_ingest():
    router = FakeRouter([
        _tasks({"kind": "ingest", "question": "이 채널 내용 수집해줘"})
    ])

    (task,) = plan("이 채널 내용 수집해줘", router)

    assert task.kind == "ingest"


def test_same_user_dm_carries_an_omitted_subject_into_a_new_period_search():
    task = Intent(
        "search",
        question="2025년에는 향타기 내용없어?",
        standalone_question="2025년에는 향타기 내용없어?",
        terms=["2025년", "향타기"],
    )
    turns = [{
        "question": "2024년 향타기 PCM 관련 자료 찾아줘",
        "subject_terms": ["2024년", "향타기", "PCM"],
    }]

    (got,) = carry_dm_subject_context(task.question, [task], turns)

    assert got.reference_mode == "none"
    assert "PCM" in got.terms
    assert "PCM" in got.standalone_question
    assert "2024년" not in got.standalone_question


def test_a_new_dm_topic_is_not_contaminated_by_the_previous_question():
    task = Intent("search", question="철근 반입 일정 알려줘", terms=["철근", "반입"])
    turns = [{
        "question": "향타기 PCM 관련 자료 찾아줘",
        "subject_terms": ["향타기", "PCM"],
    }]

    assert carry_dm_subject_context(task.question, [task], turns) == [task]


def test_a_declarative_dm_correction_becomes_feedback_context():
    task = Intent("memory", question="고창 데시앙이 맞아")
    turns = [{"question": "고창덕산 사업추진 일정 알려줘"}]

    (got,) = apply_dm_feedback_context(task.question, [task], turns)

    assert got.kind == "feedback"
    assert "고창" in got.terms
    assert "데시앙" in got.terms


def test_a_correction_question_is_not_silently_recorded_as_feedback():
    task = Intent("search", question="고창 데시앙이 맞아?")

    assert apply_dm_feedback_context(task.question, [task], [{}]) == [task]


def test_latest_dm_feedback_supplies_the_corrected_search_name():
    task = Intent(
        "summary",
        question="고창 덕산 사업협약서 주요내용 정리해줘",
        standalone_question="고창 덕산 사업협약서 주요내용 정리해줘",
        terms=["고창", "덕산", "사업협약서"],
    )
    turns = [{
        "question": "고창 데시앙이 맞아",
        "intent_kind": "feedback",
        "subject_terms": ["고창", "데시앙"],
    }]

    (got,) = carry_dm_subject_context(task.question, [task], turns)

    assert "데시앙" in got.terms
    assert "데시앙" in got.standalone_question


def test_task_cap_is_enforced():
    router = FakeRouter([
        _tasks(*[{"kind": "search", "question": f"q{i}", "terms": [f"t{i}"]} for i in range(6)])
    ])
    assert len(plan("여러 질문", router)) <= MAX_TASKS


def test_same_kind_is_merged_with_union_of_terms():
    router = FakeRouter([
        _tasks(
            {"kind": "search", "question": "김해외동", "terms": ["김해외동"]},
            {"kind": "search", "question": "기성금", "terms": ["기성금"]},
        )
    ])
    (task,) = plan("김해외동 그리고 기성금", router)
    assert set(task.terms) == {"김해외동", "기성금"}


def test_llm_days_are_ignored_and_period_comes_from_the_question():
    router = FakeRouter([_tasks({"kind": "summary", "question": "요약", "days": 99999})])
    (task,) = plan("최근 3일 요약", router)
    assert task.days == 3


def test_bad_days_uses_default():
    router = FakeRouter([_tasks({"kind": "summary", "question": "요약", "days": "이번주"})])
    (task,) = plan("요약", router)
    assert task.days == 7


def test_llm_reference_mode_is_accepted_only_with_prior_context():
    payload = _tasks({
        "kind": "search",
        "question": "그 금액은?",
        "reference_mode": "prior_turn",
        "terms": ["금액"],
    })
    with_context = plan("그 금액은?", FakeRouter([payload]), conversation_context="이전 질문")
    without_context = plan("그 금액은?", FakeRouter([payload]))

    assert with_context[0].reference_mode == "prior_turn"
    assert without_context[0].reference_mode == "none"


def test_llm_reference_mode_populates_only_execution_metadata():
    router = FakeRouter([_tasks({
        "kind": "search",
        "question": "그 미수금 문서는?",
        "reference_mode": "prior_topic",
        "terms": ["미수금"],
    })])

    (task,) = plan("그 미수금 문서는?", router, conversation_context="이전 질문")

    assert task.reference_mode == "prior_topic"
    assert task.topic_terms == ["미수금"]
    assert not task.include_attachment_status


def test_llm_source_scope_decision_is_not_overridden_by_regex():
    router = FakeRouter([_tasks({
        "kind": "search",
        "question": "그럼 채널에 있는 폴더는?",
        "asks_about_our_sources": True,
    })])

    (task,) = plan("그럼 채널에 있는 폴더는?", router)

    assert task.kind == "help"
    assert task.asks_about_our_sources


def test_llm_failure_does_not_guess_a_followup_scope():
    router = FakeRouter([RuntimeError("down"), RuntimeError("down")])

    (task,) = plan("그 문서 다시 확인해줘", router, conversation_context="이전 질문")

    assert task.reference_mode == "none"


def test_planner_prompt_asks_for_task_list():
    """**확정 명령이 아닌** 질문으로 본다.

    `상태`·`도움말`·수집 명령은 규칙이 바로 확정하고 LLM 을 부르지 않는다
    (`_deterministic_plan`). 그런 질문으로 프롬프트를 검사하면 호출이 0건이라
    시험이 무엇을 보는지와 무관하게 깨진다.
    """
    router = FakeRouter([_tasks({"kind": "search", "question": "기성금 얼마야"})])
    plan("기성금 얼마야", router)
    system = router.calls[0][0].content
    assert '"tasks"' in system
    assert "하위질문" in system


def test_answer_engine_plan_accepts_and_forwards_specialist_roster():
    router = FakeRouter([_tasks({
        "kind": "summary",
        "question": "주간 보고 요약",
        "capability": "internal_document_summary",
        "specialist": "hermes",
        "confidence": 0.9,
    })])
    engine = AnswerEngine(store=None, router=router)
    roster = [SimpleNamespace(
        key="hermes",
        name="Hermes",
        domain="내부 문서",
        routing_hint="보고서 요약",
    )]

    (task,) = engine.plan("주간 보고를 요약해줘", specialists=roster)

    assert task.suggested_specialist == "hermes"
    user = router.calls[0][1].content
    assert "<전문봇>" in user
    assert "hermes: Hermes / 내부 문서 — 보고서 요약" in user


def test_thread_context_is_for_reference_resolution_not_evidence():
    router = FakeRouter([_tasks({
        "kind": "search",
        "question": "가정산서.pdf 다시 확인해줘",
        "terms": ["가정산서.pdf"],
    })])

    (task,) = plan(
        "처리 안 된 하나의 문서도 다시 확인해줘",
        router,
        conversation_context="이전 봇 답변: 가정산서.pdf 하나는 변환 실패",
    )

    assert task.question == "가정산서.pdf 다시 확인해줘"
    assert task.terms == ["가정산서.pdf"]
    user = router.calls[0][1].content
    assert "<이전_스레드>" in user
    assert "<현재_질문>" in user
    assert "사실 근거가" in router.calls[0][0].content


def test_a_singular_failed_attachment_follow_up_cannot_expand_to_channel_summary():
    router = FakeRouter([_tasks({
        "kind": "search",
        "question": "처리 실패 문서 정리",
        "standalone_question": "202512 미수금관리보고.pdf 내용을 확인해줘",
        "reference_mode": "prior_attachments",
        "terms": ["202512 미수금관리보고.pdf"],
    })])
    context = (
        "이전 봇 답변: _근거: 문서 4건 · 자동 변환 실패로 내용을 읽지 못한 첨부: "
        "202512 미수금관리보고.pdf_"
    )

    (task,) = plan("처리 안 된 하나의 문서도 다시 확인해서 정리해줘", router,
                   conversation_context=context)

    assert task.kind == "search"
    assert task.terms == ["202512 미수금관리보고.pdf"]
    assert task.source == "llm"
    assert task.reference_mode == "prior_attachments"


def test_singular_failed_attachment_context_also_works_when_planner_is_down():
    context = "자동 변환 실패로 내용을 읽지 못한 첨부: 가정산서.pdf"

    (task,) = plan("처리 안 된 하나의 문서 확인해줘", None,
                   conversation_context=context)

    assert task.kind == "search"
    assert task.reference_mode == "none"
    assert "가정산서.pdf" not in task.terms


# --- 의도 분류 집합 ----------------------------------------------------------
def test_kind_groups_cover_every_kind_exactly_once():
    """분류만 추가하고 실행 경로를 안 붙이면 그 의도는 조용히 무응답이 된다."""
    grouped = list(ARCHIVE_KINDS) + list(SELF_KINDS) + list(WRITE_KINDS)
    assert sorted(grouped) == sorted(KINDS)
    assert len(grouped) == len(set(grouped))


# --- 문장 생성 --------------------------------------------------------------
def test_facts_are_passed_but_prose_comes_from_model():
    router = FakeRouter(["기억하지 않습니다. 매번 원문에서 다시 찾습니다."])
    out = write_from_facts(
        router,
        question="기억나?",
        facts={"이전_답변_기억": False},
        fallback="FALLBACK",
    )
    assert out.startswith("기억하지 않습니다")
    user_msg = router.calls[0][1].content
    assert "이전_답변_기억" in user_msg
    assert "기억나?" in user_msg


def test_compose_failure_uses_fallback():
    router = FakeRouter([RuntimeError("401")])
    out = write_from_facts(router, question="q", facts={}, fallback="정해진 문구")
    assert out == "정해진 문구"


def test_empty_model_output_uses_fallback():
    router = FakeRouter(["", "   "])
    assert write_from_facts(router, question="q", facts={}, fallback="FB") == "FB"


def test_no_router_uses_fallback():
    assert write_from_facts(None, question="q", facts={}, fallback="FB") == "FB"


def test_sections_are_separated_so_citations_stay_attributable():
    merged = join_sections(["기억 설명", "요약 답변\n출처: #채널, 📄문서(2026-08-27)"])
    assert "───" in merged
    assert "출처: #채널" in merged


def test_single_section_has_no_separator():
    assert join_sections(["하나뿐"]) == "하나뿐"


def test_empty_sections_still_say_something():
    assert join_sections(["", "   "]) == "답변을 만들지 못했습니다."


def test_dropped_questions_are_announced():
    """조용히 버리면 '물었는데 무시당했다' 가 된다 - 이번 개편의 출발점."""
    assert "2건" in truncated_notice(2)
