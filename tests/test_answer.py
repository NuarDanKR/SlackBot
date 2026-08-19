"""답변 파이프라인 — 환각방지 4겹 회귀 테스트."""
from __future__ import annotations

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine, parse_model_flag
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.router import Router

DOC = """---
workspace: pilot
channel: "#현장_김해외동(180182)_채팅방"
visibility: private
acl: [#현장_김해외동(180182)_채팅방]
doc_count: 1
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
-

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 09:15] 홍길동: 기성금 3억 2천만원 청구 완료
"""


class FakeProvider:
    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("기성금은 3억 2천만원입니다.", spec.model, self.name, 100, 20, 0.001)


@pytest.fixture
def engine(tmp_path):
    p = tmp_path / "channels" / "pilot" / "김해외동.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DOC, encoding="utf-8")
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={"claude-sonnet-5": ModelSpec("claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL)},
        cost_guard=__import__("tybot.gateway.cost", fromlist=["CostGuard"]).CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router), fake


def _ctx(channels=("#현장_김해외동(180182)_채팅방",)):
    return RequestContext(workspace="pilot", channels=frozenset(channels))


def test_answer_attaches_citation(engine):
    eng, fake = engine
    ans = eng.answer("기성금 얼마야", _ctx())
    assert ans.reason == "answered"
    assert ans.citations and "김해외동" in ans.citations[0]
    assert "출처:" in ans.to_slack()
    # 근거는 원문 라인이어야 한다
    assert "3억 2천만원" in fake.calls[0][1].content


def test_zero_hits_never_answers_something_else(engine):
    """근거 0건이면 다른 질문에 답하지 않는다. LLM 호출도 안 한다(비용 0)."""
    eng, fake = engine
    ans = eng.answer("전혀없는키워드zzz", _ctx())
    assert ans.reason == "no_hits"
    assert "추측으로 답하지 않습니다" in ans.text
    assert "#현장_김해외동(180182)_채팅방" in ans.text  # 어디를 볼지는 알려준다
    assert fake.calls == []


def test_zero_hits_and_no_recent_raw_returns_title_list(tmp_path):
    """기간 밖(오래된) 원문뿐이면 LLM 없이 문서 목록만 준다."""
    old = DOC.replace("2026-08-12", "2020-01-05")
    p = tmp_path / "channels" / "pilot" / "김해외동.md"
    p.parent.mkdir(parents=True)
    p.write_text(old, encoding="utf-8")
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=__import__("tybot.gateway.cost", fromlist=["CostGuard"]).CostGuard(10.0),
    )
    ans = AnswerEngine(ArchiveStore(tmp_path), router).answer("전혀없는키워드zzz", _ctx())
    assert ans.reason == "no_hits"
    assert "#현장_김해외동(180182)_채팅방" in ans.text
    assert fake.calls == []  # 근거가 없으면 LLM 호출 자체를 안 한다


def test_no_permission_never_leaks_channel_name(engine):
    eng, fake = engine
    ans = eng.answer("기성금 얼마야", _ctx(channels=()))
    assert ans.reason == "no_access"
    assert "김해외동" not in ans.text
    assert fake.calls == []


def test_model_flag():
    assert parse_model_flag("--model=claude-opus-4-8 기성금?") == ("claude-opus-4-8", "기성금?")
    assert parse_model_flag("기성금?") == (None, "기성금?")


def test_unknown_model_is_rejected(engine):
    eng, _ = engine
    ans = eng.answer("--model=gpt-9 기성금 얼마야", _ctx())
    assert ans.reason == "error" and "모델" in ans.text
