"""판단·권고 경로 — 사실과 판단의 경계가 유지되는지 검증."""
from __future__ import annotations

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import Intent, classify_by_rule

DOC = """---
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
> [2026-08-14 09:15] 단라운: Workspace 구성안 1안(멀티 워크스페이스) vs 2안(단일 워크스페이스) 검토 중
> [2026-08-14 10:00] 단라운: 1안 리스크는 워크스페이스 간 메시징 문제, 게스트 초대 시 과금
"""


class FakeProvider:
    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("결론: 큰 채널 하나가 낫습니다.", spec.model, self.name, 400, 80, 0.003)


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
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router), fake


def _ctx(channels=("#프로젝트-업데이트",)):
    return RequestContext(workspace="pilot", channels=frozenset(channels))


def test_advice_uses_archive_when_relevant(engine):
    eng, fake = engine
    ans = eng.advise("워크스페이스 구성 어느 방향을 추천해?", _ctx(), terms=["워크스페이스", "구성"])
    assert ans.reason == "advice"
    assert "1안" in fake.calls[0][1].content  # 관련 원문을 근거로 넣었다
    assert ans.citations  # 원문을 썼으면 출처가 붙는다
    assert "원문 2건을 근거로 참고" in ans.text or "근거로 참고" in ans.text


def test_advice_without_archive_is_labelled(engine):
    eng, fake = engine
    ans = eng.advise("회의록 양식은 어떤 게 좋을까?", _ctx(), terms=["회의록", "양식"])
    assert ans.reason == "advice"
    assert "일반적인 판단입니다" in ans.text  # 라벨은 코드가 붙인다(LLM 에 맡기지 않음)
    assert ans.citations == []
    assert "(관련 원문 없음)" in fake.calls[0][1].content


def test_advice_respects_permission(engine):
    """권한 없는 채널 원문은 판단 근거에도 들어가지 않는다."""
    eng, fake = engine
    ans = eng.advise("워크스페이스 구성 추천해줘", _ctx(channels=()), terms=["워크스페이스"])
    assert ans.citations == []
    assert "1안" not in fake.calls[0][1].content


def test_advice_prompt_forbids_inventing_internal_facts(engine):
    eng, fake = engine
    eng.advise("어느 방향이 나을까?", _ctx(), terms=["방향"])
    system = fake.calls[0][0].content
    assert "사내 사실" in system and "추정치" in system


def test_respond_routes_advice(engine):
    eng, fake = engine
    ans = eng.respond("채널 하나로 묶는 게 나을까?", _ctx(), Intent("advice", terms=["채널"]))
    assert ans.reason == "advice"
    assert fake.calls  # 판단 요청은 답을 만든다(예전엔 도움말로 새거나 거절됐다)


@pytest.mark.parametrize(
    "text",
    [
        "채널을 하나하나 만들기보다 큰 채널 하나 만들고 연관 프로젝트를 넣고 싶은데 어느 방향을 추천해?",
        "이 구성의 장단점 알려줘",
        "이렇게 하면 문제 생길까?",
        "회의록은 어떤 방법으로 정리하는 게 좋을까?",
    ],
)
def test_rule_fallback_detects_advice(text):
    assert classify_by_rule(text).kind == "advice"


@pytest.mark.parametrize("text", ["김해외동 기성금 얼마야?", "착공일 언제야?"])
def test_fact_questions_are_not_advice(text):
    assert classify_by_rule(text).kind == "search"
