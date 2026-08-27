"""의도 분류 — LLM 판단 + 규칙 폴백."""
from __future__ import annotations

import json

import pytest

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
