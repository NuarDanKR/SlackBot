"""의도 분류 — LLM 판단 + 규칙 폴백."""
from __future__ import annotations

import json

import pytest

from tybot import intent
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import CLASSIFIER_MODEL, classify, classify_by_rule, parse_period


class ScriptedProvider:
    """분류기가 뱉을 문자열을 그대로 돌려준다."""

    name = "anthropic"

    def __init__(self, payload: str, *, raises: Exception | None = None):
        self.payload = payload
        self.raises = raises
        self.models: list[str] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        if self.raises:
            raise self.raises
        self.models.append(spec.model)
        return LLMResponse(self.payload, spec.model, self.name, 200, 30, 0.0002)


def _router(provider, *, with_haiku=True):
    registry = {
        "claude-sonnet-5": ModelSpec(
            "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
        )
    }
    if with_haiku:
        registry[CLASSIFIER_MODEL] = ModelSpec(
            CLASSIFIER_MODEL, "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
        )
    return Router(
        providers={"anthropic": provider}, registry=registry, cost_guard=CostGuard(10.0)
    )


def test_llm_decides_kind_and_terms():
    p = ScriptedProvider(json.dumps({"kind": "search", "terms": ["김해외동", "기성금"]}))
    intent = classify("김해외동 기성금 얼마인지 알려줘", _router(p))
    assert intent.kind == "search"
    assert intent.terms == ["김해외동", "기성금"]  # 요청 표현('알려줘')은 검색어에서 빠진다
    assert intent.source == "llm"


def test_uses_cheap_classifier_model():
    p = ScriptedProvider(json.dumps({"kind": "status"}))
    classify("너 지금 잘 돌아가?", _router(p))
    assert p.models == [CLASSIFIER_MODEL]  # 분류는 최저가 모델로


def test_falls_back_to_default_model_when_classifier_missing():
    p = ScriptedProvider(json.dumps({"kind": "status"}))
    intent = classify("상태 어때", _router(p, with_haiku=False))
    assert p.models == ["claude-sonnet-5"]
    assert intent.kind == "status"


def test_code_fenced_json_is_parsed():
    p = ScriptedProvider('```json\n{"kind": "summary", "days": 30}\n```')
    intent = classify("한달치 정리해줘", _router(p))
    assert intent.kind == "summary" and intent.days == 30


@pytest.mark.parametrize(
    "payload", ["설명만 하고 JSON 없음", '{"kind": "존재하지않는것"}', '{"kind": '],
)
def test_bad_output_falls_back_to_rules(payload):
    intent = classify("현재 너의 상태 알려줘", _router(ScriptedProvider(payload)))
    assert intent.source == "regex"
    assert intent.kind == "status"


def test_llm_failure_does_not_break_bot():
    p = ScriptedProvider("", raises=RuntimeError("API 다운"))
    intent = classify("이번주 요약해줘", _router(p))
    assert intent.source == "regex" and intent.kind == "summary"


def test_no_router_uses_rules():
    assert classify("도움말", None).kind == "help"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("현재 너의 상태 알려줘", "status"),
        ("연결 상태 어때?", "status"),
        ("지금 살아있어?", "status"),
        ("사용법 알려줘", "help"),
        ("이번주 진행 상황", "summary"),
        ("김해외동 기성금 얼마야?", "search"),
    ],
)
def test_rule_fallback_covers_common_phrasing(text, kind):
    assert classify_by_rule(text).kind == kind


def test_rule_fallback_strips_request_words():
    assert "알려줘" not in classify_by_rule("김해외동 기성금 알려줘").terms


@pytest.mark.parametrize(
    "text,days", [("30일 요약", 30), ("2주 진행상황", 14), ("이번달 요약", 30), ("요약해줘", 7)]
)
def test_parse_period(text, days):
    assert parse_period(text) == days


@pytest.mark.parametrize(
    "text,kind",
    [
        ("내용 수집해", "ingest"),
        ("수집해줘", "ingest"),
        ("이 채널 취합해줘", "ingest"),
        ("대화 모아줘", "ingest"),
        ("수집", "ingest"),
        ("전체 수집해", "ingest_all"),
        ("모든 채널 수집", "ingest_all"),
        # 현황 '질문'은 수집 실행이 아니다
        ("몇 건 수집했어?", "status"),
        ("수집 상태 알려줘", "status"),
    ],
)
def test_ingest_command_vs_status_question(text, kind):
    assert classify_by_rule(text).kind == kind


# --- 상태 질문: LLM 분류기가 죽었을 때가 가장 중요하다 -------------------------
# 실제 사고: 키가 401 이 되어 분류기가 규칙으로 폴백했는데, 규칙이 "현재 상태" 를
# search 로 봤다. 그래서 상태 질문이 아카이브 검색 -> LLM 호출 -> 또 401 로 죽었다.
STATUS_QUESTIONS = [
    "현재 상태",
    "지금 상태",
    "상태 알려줘",
    "상태는 어때?",
    "상태 확인해줘",
    "봇 상태",
    "상태",
    "시스템 상태",
    "서버 상황",
    "현재 상황 보여줘",
]


@pytest.mark.parametrize("q", STATUS_QUESTIONS)
def test_status_questions_need_no_llm(q):
    assert classify_by_rule(q).kind == "status"


# 상태 표현이 들어갔지만 실제로는 아카이브 질문인 것들 - status 로 새면 안 된다.
NOT_STATUS = [
    ("현재 자금 상태 문서 찾아줘", "search"),
    ("이번주 현장 진행 상황 요약해줘", "summary"),
    ("이전 답변 기억나?", "memory"),
]


@pytest.mark.parametrize(("q", "kind"), NOT_STATUS)
def test_status_pattern_does_not_swallow_archive_questions(q, kind):
    assert classify_by_rule(q).kind == kind


# --- 문서 집합 요약: 기간의 범위 (설계 §9) --------------------------------------
#
# 「여태까지 수집된 주간 보고를 종합해줘」 를 최근 7일로 처리하면, 파일이 멀쩡히
# 아카이브에 있어도 후보에 안 들어온다. 사용자가 요청한 범위와 실제 조회 범위가
# 다른데 답변은 「자료가 없다」 로 나가서, 그 차이가 어디에도 안 보인다.
REAL_QUESTION = "여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘"


def test_the_real_question_is_a_summary_not_a_status():
    """`수집된` 이 status 로 새면 요약 요청이 통째로 사라진다(2026-09-11 실측)."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.kind == "summary"


def test_the_real_question_asks_for_all_time():
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.time_scope == intent.SCOPE_ALL
    assert got.wants_all_time


def test_the_real_question_carries_the_document_kinds():
    """본문에 「주간 보고 회의」 가 없어도 파일명으로 찾을 수 있어야 한다(§2.6)."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert "주간보고" in got.document_query
    assert "주간업무보고" in got.document_query
    assert "업무보고" in got.document_query
    assert got.is_document_set


def test_summary_keeps_its_topic_terms():
    """검색어가 비면 채널 전체 최근 줄을 쓴다 — 질문과 상관없는 답이 된다(§2.5)."""
    assert intent.classify_by_rule(REAL_QUESTION).terms


@pytest.mark.parametrize("text", [
    "여태까지 올라온 자료 정리해줘",
    "지금까지 수집된 업무보고 종합해줘",
    "그동안 쌓인 내용 요약해줘",
    "전체 기간 현황 정리해줘",
])
def test_all_time_words(text):
    assert intent.parse_time_scope(text) == intent.SCOPE_ALL


@pytest.mark.parametrize("text", [
    "이번주 진행상황 정리해줘",
    "최근 30일 미수금 정리해줘",
    "오늘 뭐 있었어 요약해줘",
])
def test_explicit_periods_are_bounded(text):
    assert intent.parse_time_scope(text) == intent.SCOPE_BOUNDED


def test_no_period_stays_default():
    assert intent.parse_time_scope("진행상황 정리해줘") == intent.SCOPE_DEFAULT


def test_days_is_not_overwritten_by_the_scope():
    """`days` 를 0 이나 큰 수로 덮어쓰면 「기본이라 7일」 과 「전체 요청」 을 못 가른다."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.days == intent.DEFAULT_DAYS
    assert got.time_scope == intent.SCOPE_ALL


# --- 동의어는 검토된 사전에서만 -----------------------------------------------
def test_document_synonyms_do_not_spread_to_other_kinds():
    """「보고」 에서 「회계보고」·「사고보고」 로 번지면 묻지 않은 문서가 섞인다."""
    got = intent.expand_document_query("주간보고 종합해줘")
    assert "주간보고" in got
    assert not any("회계" in x or "사고" in x for x in got)


def test_unknown_document_kind_yields_nothing():
    assert intent.expand_document_query("점심 메뉴 정리해줘") == []


def test_spaces_in_the_document_name_still_match():
    """사람은 「주간 보고」 라고 띄어 쓴다. 파일명은 붙여 쓴다."""
    assert intent.expand_document_query("주간 보고 종합해줘")


def test_every_synonym_entry_includes_its_own_key():
    """키가 빠지면 그 이름으로 올라온 파일을 자기 이름으로 못 찾는다."""
    for key, values in intent.DOCUMENT_SYNONYMS.items():
        assert key in values, key


# --- status 와 요약의 경계 -----------------------------------------------------
@pytest.mark.parametrize("text", ["수집 현황 알려줘", "몇 건 수집됐어?", "수집했어?"])
def test_real_collection_status_questions_still_work(text):
    assert intent.classify_by_rule(text).kind == "status"


def test_document_query_is_empty_for_non_summary():
    """검색 질문에 문서 종류를 붙이면 단일 사실 질문이 문서 집합으로 바뀐다."""
    got = intent.classify_by_rule("김해외동 기성금 얼마야")
    assert got.kind == "search"
    assert got.document_query == []
    assert not got.is_document_set
