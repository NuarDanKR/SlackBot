"""질문 메아리 — 봇에게 같은 것을 물어본 기록은 근거 자리를 차지하지 않는다 (2026-11-12 실측).

이강석님 DM: 「…재무목표 Top5를 정리해 표로 설명」 을 여러 번 물었고, 그 질문 줄들이 질문
낱말을 전부 가져 근거 상위를 차지했다. 모델은 「원문에는 요청한 기록만 있다」 며 근거 없음으로
닫았다. 답이 든 본문 줄은 남아야 한다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import answer as answer_module
from tybot.access import RequestContext

Q = "태영건설 2026년 재무목표"


@pytest.mark.parametrize(
    "line",
    [
        "태영건설 2026년 연말까지 달성해야할 재무목표 Top5를 정리해 표로 설명",
        "태영건설 2026년 재무목표 알려주세요",
        "태영건설 2026년 재무목표는?",
    ],
)
def test_previous_questions_are_echoes(line):
    assert answer_module.is_question_echo(Q, line)


@pytest.mark.parametrize(
    "line",
    [
        "2026년 재무목표 1 부채비율 200% 이하",
        "태영건설 2026년 재무목표: 영업이익 325억 이상, 현금시재 3,000억 이상",
        "재무목표 정리 결과 공유드립니다. " + "부채비율 200% 이하 " * 30,  # 긴 본문
        "정리해 주세요",  # 질문 낱말이 없다
    ],
)
def test_answer_lines_are_not_echoes(line):
    assert not answer_module.is_question_echo(Q, line)


def _hit(text, path="a.md", lineno=1):
    return SimpleNamespace(
        doc=SimpleNamespace(path=Path(path)), line=SimpleNamespace(text=text, lineno=lineno)
    )


def test_search_drops_echoes_and_keeps_the_evidence_count():
    echoes = [_hit("태영건설 2026년 재무목표 정리해 표로 설명", "dm.md", i) for i in range(5)]
    answers = [_hit(f"2026년 재무목표 {i} 영업이익", "plan.md", i) for i in range(50)]
    engine = answer_module.AnswerEngine(
        SimpleNamespace(), SimpleNamespace(),
        max_hits=40, per_document_hits=4, exhaustive_max_lines=150_000, focus_terms=True,
    )
    seen = {}
    engine._store = SimpleNamespace(
        search=lambda q, ctx, **kw: seen.update(kw) or (echoes + answers)[: kw["limit"]]
    )
    hits = engine._search(Q, RequestContext(workspace="mgmt", channels=frozenset({"#x"})))
    assert len(hits) == 40
    assert all("정리해" not in hit.line.text for hit in hits)


def test_answer_prompt_allows_partial_answers():
    prompt = answer_module.SYSTEM_PROMPT
    assert "일부라도" in prompt
    assert "관련 내용이 하나도 없을 때만" in prompt
    assert "라고만 답한다" not in prompt
