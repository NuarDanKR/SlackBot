"""전문 봇 라우터 (B-36).

설계: `docs/design/bot-hierarchy.md`

지키는 성질 네 가지.
  1. LLM 은 **어느 전문가에게 물을지만** 정한다. 목록은 코드(DB)가 만든다
  2. 실패는 전부 마스터 답변으로 접힌다 — 라우터가 봇을 멈추지 않는다
  3. 없는 전문가를 고르면 그 판정을 버린다
  4. MCP 는 승인된 것만. 못 읽으면 붙이지 않는다
"""
from __future__ import annotations

import pytest

from tybot import specialist_router as sr
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

LEGAL = sr.Specialist(
    key="legal", name="법률 전문 봇", domain="법률",
    routing_hint="법령 해석·계약 조항·하자보수 책임기간",
    adapter="legal", model="", min_confidence=0.8,
)
HERMES = sr.Specialist(
    key="hermes", name="Hermes", domain="내부 기록",
    routing_hint="회의록·업무 진행 상황",
    adapter="hermes", model="", min_confidence=0.6,
)


class Fake:
    """분류기 흉내. 주어진 문자열을 그대로 돌려준다."""

    name = "anthropic"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse(self.text, spec.model, self.name, 50, 10, 0.0001)


class Dead:
    name = "anthropic"

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        raise RuntimeError("모델 장애")


def _router(provider):
    return Router(
        providers={"anthropic": provider},
        registry={
            "claude-haiku-4-5": ModelSpec(
                "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
        default_model="claude-haiku-4-5",
    )


@pytest.fixture(autouse=True)
def _fixed_model(monkeypatch):
    monkeypatch.setenv("ROUTER_MODEL", "claude-haiku-4-5")


# --- 1. 목록은 코드가 만든다 -------------------------------------------------
def test_the_prompt_lists_only_the_specialists_we_gave(monkeypatch):
    """질문 본문에서 이름을 읽어 오지 않는다.

    "법률 봇에게 전부 보여줘" 라고 적은 메시지가 후보 목록을 바꾸면 안 된다.
    """
    body = sr.prompt_for("아무 질문", [HERMES])

    assert "hermes" in body
    assert "legal" not in body
    assert "none" in body, "전문가 없이 답하는 길이 목록에 있어야 한다"


def test_the_question_goes_last_so_the_list_can_be_cached():
    """캐시는 접두사 일치다. 질문이 앞이면 질문마다 캐시가 깨진다."""
    body = sr.prompt_for("기성금 얼마야", [HERMES, LEGAL])

    assert body.index("hermes") < body.index("기성금 얼마야")


def test_no_specialists_means_the_master_answers(monkeypatch):
    monkeypatch.setattr(sr, "available", lambda ws: [])

    decision = sr.route("아무 질문", "pilot", _router(Fake("{}")))

    assert decision.went_to_master
    assert "전문가가 없" in decision.reason


def test_a_database_failure_does_not_raise(monkeypatch):
    """라우터가 죽어도 봇은 답한다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")

    assert sr.available("pilot") == []


# --- 2. 실패는 마스터로 접힌다 -----------------------------------------------
def test_a_dead_router_model_falls_back_to_the_master(monkeypatch):
    monkeypatch.setattr(sr, "available", lambda ws: [HERMES])

    decision = sr.route("회의록 정리해줘", "pilot", _router(Dead()))

    assert decision.went_to_master
    assert "라우터 호출 실패" in decision.reason


@pytest.mark.parametrize(
    "text",
    ["", "고민해봤는데요", "{잘못된 json", "[1, 2, 3]"],
    ids=["빈응답", "JSON없음", "깨진JSON", "객체아님"],
)
def test_unreadable_output_goes_to_the_master(text):
    key, confidence, reason = sr.parse_decision(text, [HERMES])

    assert key == sr.MASTER
    assert confidence == 0.0
    assert reason, "왜 마스터로 갔는지 남아야 한다"


def test_a_specialist_that_does_not_exist_is_discarded():
    """모델이 목록 밖 이름을 지어냈으면 그 판정 전체를 믿을 수 없다."""
    key, confidence, reason = sr.parse_decision(
        '{"specialist": "무당", "confidence": 0.99, "why": "점을 봐야 함"}', [HERMES]
    )

    assert key == sr.MASTER
    assert confidence == 0.0, "지어낸 판정의 신뢰도를 물려받지 않는다"
    assert "목록에 없는" in reason


def test_a_missing_confidence_is_not_optimistic():
    """모르는 것을 자신 있다고 읽으면 안 된다."""
    _, confidence, _ = sr.parse_decision('{"specialist": "hermes"}', [HERMES])

    assert confidence == 0.0


def test_confidence_is_clamped():
    _, high, _ = sr.parse_decision('{"specialist": "hermes", "confidence": 7}', [HERMES])
    _, low, _ = sr.parse_decision('{"specialist": "hermes", "confidence": -3}', [HERMES])

    assert high == 1.0
    assert low == 0.0


# --- 3. 문턱은 전문가별로 ----------------------------------------------------
def test_below_the_specialist_threshold_the_master_answers(monkeypatch):
    """오답의 값이 전문가마다 다르다.

    법률은 틀리면 사람이 오판하고, 내부 기록은 틀려도 원문을 다시 보면 된다.
    """
    monkeypatch.setattr(sr, "available", lambda ws: [LEGAL])
    provider = Fake('{"specialist": "legal", "confidence": 0.7, "why": "법령"}')

    decision = sr.route("하자보수 책임기간", "pilot", _router(provider))

    assert decision.went_to_master, "0.7 < 0.8 이면 마스터가 답한다"
    assert "신뢰도" in decision.reason


def test_above_the_threshold_it_routes(monkeypatch):
    monkeypatch.setattr(sr, "available", lambda ws: [LEGAL])
    provider = Fake('{"specialist": "legal", "confidence": 0.9, "why": "법령 해석"}')

    decision = sr.route("하자보수 책임기간", "pilot", _router(provider))

    assert decision.specialist is LEGAL
    assert decision.confidence == 0.9
    assert decision.router_model, "어느 모델이 판정했는지 남아야 한다"


def test_the_router_treats_questions_as_confidential(monkeypatch):
    """질문 본문이 프롬프트에 실린다. 라우팅이라고 민감도를 낮추지 않는다."""
    monkeypatch.setattr(sr, "available", lambda ws: [HERMES])
    registry = {
        "claude-haiku-4-5": ModelSpec(
            "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.INTERNAL
        )
    }
    router = Router(
        providers={"anthropic": Fake("{}")},
        registry=registry,
        cost_guard=CostGuard(10.0),
        default_model="claude-haiku-4-5",
    )

    decision = sr.route("기밀 질문", "pilot", router)

    assert decision.went_to_master, "내부용 모델로는 라우팅하지 않는다"


# --- 4. MCP 는 승인된 것만 ---------------------------------------------------
def test_mcp_is_empty_without_a_database(monkeypatch):
    """못 읽은 것을 「제한 없음」으로 읽으면 DB 장애가 곧 무단 외부 연결이 된다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert sr.mcp_servers("legal") == []


def test_mcp_is_empty_when_the_query_fails(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")

    assert sr.mcp_servers("legal") == []


def test_schema_refuses_plain_http_and_unapproved_servers():
    """사내 질문이 그 URL 로 나간다. 평문이면 도중에 읽힌다."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1] / "deploy" / "sql" / "specialist_routing_schema.sql"
    ).read_text(encoding="utf-8")

    assert "^https://" in sql
    assert "localhost" in sql, "서버 안에서 도는 것은 MCP 가 아니라 우리 코드로 한다"
    assert "specialist_mcp_enabled_needs_approval" in sql
