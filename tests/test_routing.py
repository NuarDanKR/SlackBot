"""질문 의도 라우팅 — 광범위 질문이 0건으로 죽지 않는지 검증."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import CLASSIFIER_MODEL, classify_by_rule

TODAY = dt.date.today()

DOC = f"""---
workspace: pilot
channel: "#프로젝트-업데이트"
visibility: private
acl: [#프로젝트-업데이트]
doc_count: 2
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
-

## 원문 (자동 취합, 편집 금지)
> [{TODAY} 09:15] 홍길동: 김해외동 기성금 3억 2천만원 청구 완료
> [{TODAY} 10:00] 이순신: 3공구 골조 마무리
"""


class FakeProvider:
    """분류 호출과 답변 호출을 구분한다. `calls` 에는 **답변 호출만** 쌓인다."""

    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []
        self.classified: list[str] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        if "라우터" in messages[0].content:  # 분류기 시스템 프롬프트
            q = messages[1].content
            self.classified.append(q)
            kind = classify_by_rule(q)
            payload = json.dumps(
                {"kind": kind.kind, "days": kind.days, "terms": kind.terms}, ensure_ascii=False
            )
            return LLMResponse(payload, spec.model, self.name, 200, 30, 0.0002)
        self.calls.append(list(messages))
        return LLMResponse("정리 결과", spec.model, self.name, 300, 60, 0.002)


@pytest.fixture
def engine(tmp_path):
    p = tmp_path / "channels" / "pilot" / "프로젝트-업데이트.md"
    p.parent.mkdir(parents=True)
    p.write_text(DOC, encoding="utf-8")
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            ),
            CLASSIFIER_MODEL: ModelSpec(
                CLASSIFIER_MODEL, "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
            ),
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router), fake


def _ctx():
    return RequestContext(workspace="pilot", channels=frozenset({"#프로젝트-업데이트"}))


@pytest.mark.parametrize(
    "q",
    [
        "현재까지 상황 요약해줘",
        "이번주 진행 상황 알려줘",
        "지금 어디까지 됐어?",
        "프로젝트 현황 브리핑",
    ],
)
def test_broad_questions_go_to_summary(engine, q):
    eng, fake = engine
    ans = eng.respond(q, _ctx())
    assert ans.reason == "answered"
    assert "기성금 3억 2천만원" in fake.calls[0][1].content
    assert ans.citations  # 출처는 언제나 붙는다


def test_specific_question_still_uses_search(engine):
    eng, fake = engine
    ans = eng.respond("기성금 얼마야", _ctx())
    assert ans.reason == "answered"
    # 검색 경로는 매칭된 라인만 근거로 넣는다
    assert "골조" not in fake.calls[0][1].content


def test_zero_hit_does_not_answer_a_different_question(engine):
    eng, fake = engine
    ans = eng.respond("자재 단가 협상 결과", _ctx())
    assert ans.reason == "no_hits"
    assert fake.calls == []          # 엉뚱한 요약을 만들지 않는다
    assert "요약" in ans.text        # 대신 다음 행동을 안내


def test_no_permission_still_blocks_fallback(engine):
    eng, fake = engine
    ans = eng.respond("현재까지 상황 요약해줘", RequestContext(workspace="pilot"))
    assert ans.reason == "no_access"
    assert "프로젝트" not in ans.text
    assert fake.calls == []


def test_out_of_scope_is_refused_without_llm(engine):
    eng, fake = engine
    from tybot.intent import Intent

    ans = eng.respond("파이썬 문법 알려줘", _ctx(), Intent("out_of_scope"))
    assert ans.reason == "out_of_scope"
    assert fake.calls == []
