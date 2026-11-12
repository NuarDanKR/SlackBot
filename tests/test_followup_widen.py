"""후속 질문도 범위에 갇히지 않는다 (2026-11-12 오너 결정, thread-follow-up 설계 §10 폐기).

실측: 「재무목표 Top5」 다음 「연말 현금시재」·「연말 자금수지 계획」 이 후속으로 판정돼
첫 답의 40줄 안에서만 찾았고, 새 검색 없이 근거 없음이 됐다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tybot import answer as answer_module
from tybot.access import RequestContext

CTX = RequestContext(workspace="mgmt", channels=frozenset({"#plan"}))


def _hit(text, path, lineno):
    return SimpleNamespace(
        doc=SimpleNamespace(path=Path(path)),
        line=SimpleNamespace(text=text, lineno=lineno, speaker="담당", source_path=None),
        score=1,
    )


def _engine(fresh, calls):
    engine = answer_module.AnswerEngine(
        SimpleNamespace(), SimpleNamespace(), max_hits=6, per_document_hits=4,
        exhaustive_max_lines=150_000, focus_terms=True,
    )
    engine._store = SimpleNamespace(
        search=lambda q, ctx, **kw: calls.append(q) or list(fresh)
    )
    return engine


PRIOR = [_hit(f"재무목표 {i} 부채비율", "plan.md", i) for i in range(1, 5)] + [
    _hit("현금시재 3,000억 이상 보유 목표", "plan.md", 9)
]
FRESH = [_hit("11월 말 현금시재 1,714억", "cash.md", 1), _hit("연말 자금수지 계획", "cash.md", 2)]


def test_a_new_topic_searches_again_under_current_acl():
    calls: list[str] = []
    engine = _engine(FRESH, calls)
    intent = SimpleNamespace(terms=["태영건설", "연말", "현금시재"])
    hits = engine._widen_followup("연말 현금시재 알려주세요", CTX, intent, PRIOR)
    assert calls == ["연말 현금시재"]  # 회사 이름은 검색어에서 빠진다
    texts = [h.line.text for h in hits]
    # 이전 근거 중 이번 낱말에 맞는 줄 → 새 검색 → 나머지 이전 근거
    assert texts[0] == "현금시재 3,000억 이상 보유 목표"
    assert texts[1:3] == ["11월 말 현금시재 1,714억", "연말 자금수지 계획"]
    assert len(hits) == 6  # 상한 = max(max_hits=6, 이전 근거 5줄)


def test_a_pure_reference_keeps_the_prior_evidence():
    calls: list[str] = []
    engine = _engine(FRESH, calls)
    hits = engine._widen_followup("방금 그거 표로", CTX, SimpleNamespace(terms=[]), PRIOR)
    assert calls == [] and hits == PRIOR


def test_a_failed_search_falls_back_to_the_prior_evidence():
    engine = _engine(FRESH, [])

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    engine._store = SimpleNamespace(search=boom)
    hits = engine._widen_followup("연말 현금시재", CTX, SimpleNamespace(terms=["현금시재"]), PRIOR)
    assert hits == PRIOR


def test_duplicate_lines_are_kept_once():
    calls: list[str] = []
    engine = _engine([PRIOR[-1], *FRESH], calls)
    hits = engine._widen_followup("현금시재", CTX, SimpleNamespace(terms=["현금시재"]), PRIOR)
    keys = [(str(h.doc.path), h.line.lineno) for h in hits]
    assert len(keys) == len(set(keys))


def test_company_name_is_not_a_search_word():
    assert answer_module.focus_query("태영건설의 근무규정") == "근무규정"
    assert answer_module.focus_query("태영건설") == "태영건설"
