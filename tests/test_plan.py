"""복합 질문 분해(1차 LLM) + 봇 자기설명 답변의 문장 생성.

실제 사고: "너가 예전에 했던 말 기억나? 그리고 지금 전산팀 워크스페이스에서는 무슨일이
벌어지고 있어?" 에 봇이 고정 문단(기억하지 않는다는 설명)만 내보내고 **두 번째 질문은
처리 경로에 도달조차 하지 못했다.** 분류기가 라벨 하나만 돌려주는 구조였기 때문이다.
"""
from __future__ import annotations

import json

from tybot.compose import join_sections, truncated_notice, write_from_facts
from tybot.intent import ARCHIVE_KINDS, KINDS, MAX_TASKS, SELF_KINDS, WRITE_KINDS, plan


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


def test_days_is_clamped():
    router = FakeRouter([_tasks({"kind": "summary", "question": "요약", "days": 99999})])
    (task,) = plan("요약", router)
    assert task.days == 365


def test_bad_days_uses_default():
    router = FakeRouter([_tasks({"kind": "summary", "question": "요약", "days": "이번주"})])
    (task,) = plan("요약", router)
    assert task.days == 7


def test_planner_prompt_asks_for_task_list():
    router = FakeRouter([_tasks({"kind": "status", "question": "상태"})])
    plan("상태", router)
    system = router.calls[0][0].content
    assert '"tasks"' in system
    assert "하위질문" in system


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
