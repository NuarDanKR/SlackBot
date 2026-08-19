"""기간 요약 + exec 통합조회 — 권한/출처가 지켜지는지 검증."""
from __future__ import annotations

import datetime as dt

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

TODAY = dt.date.today()
OLD = TODAY - dt.timedelta(days=60)


def _doc(channel: str, lines: list[tuple[str, str, str]]) -> str:
    body = "\n".join(f"> [{ts}] {who}: {what}" for ts, who, what in lines)
    return (
        "---\n"
        "workspace: pilot\n"
        f'channel: "{channel}"\n'
        "visibility: private\n"
        f"acl: [{channel}]\n"
        f"doc_count: {len(lines)}\n"
        "last_ingested: 2026-08-19T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n" + body + "\n"
    )


class FakeProvider:
    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("정리 결과", spec.model, self.name, 500, 100, 0.005)


@pytest.fixture
def engine(tmp_path):
    base = tmp_path / "channels" / "pilot"
    base.mkdir(parents=True)
    (base / "자금.md").write_text(
        _doc(
            "#팀_자금(ABB540)_주간보고",
            [
                (f"{TODAY} 09:15", "홍길동", "김해외동 기성금 3억 2천만원 청구 완료"),
                (f"{OLD} 10:00", "홍길동", "작년 예산 회의 내용"),
            ],
        ),
        encoding="utf-8",
    )
    (base / "현장.md").write_text(
        _doc("#현장_김해외동(180182)_채팅방", [(f"{TODAY} 11:00", "이순신", "3공구 골조 완료")]),
        encoding="utf-8",
    )
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


def test_summary_covers_only_member_channels(engine):
    eng, fake = engine
    ctx = RequestContext(workspace="pilot", channels=frozenset({"#팀_자금(ABB540)_주간보고"}))
    ans = eng.summarize(ctx, days=7)
    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "기성금 3억 2천만원" in evidence
    assert "골조" not in evidence  # 멤버가 아닌 채널은 근거에서 제외
    assert len(ans.citations) == 1


def test_summary_exec_sees_all_channels(engine):
    eng, fake = engine
    ans = eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    evidence = fake.calls[0][1].content
    assert "기성금 3억 2천만원" in evidence and "골조" in evidence
    assert len(ans.citations) == 2


def test_summary_respects_period(engine):
    eng, fake = engine
    eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    assert "작년 예산 회의" not in fake.calls[0][1].content  # 기간 밖 원문 제외


def test_summary_without_permission_leaks_nothing(engine):
    eng, fake = engine
    ans = eng.summarize(RequestContext(workspace="pilot", channels=frozenset()), days=7)
    assert ans.reason == "no_access"
    assert "김해외동" not in ans.text and "자금" not in ans.text
    assert fake.calls == []


def test_summary_has_citations_in_slack_output(engine):
    eng, _ = engine
    ans = eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    out = ans.to_slack()
    assert "출처:" in out and "📄" in out
