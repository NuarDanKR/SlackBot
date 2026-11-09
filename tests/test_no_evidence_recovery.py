"""「근거 없음」 을 줄인다 — **같은 권한 범위 안에서 놓친 근거**만 되찾는다.

근거 없이 답하게 하는 것이 아니다(hallucination-guard 3겹). 바뀌는 것은 셋이다.

1. 분류기 핵심어로 0건이면 질문 원문의 낱말로 한 번 더 찾는다
2. 질문 어미(「청구했나요」)를 떼어 본문의 「청구」 에 맞게 한다
3. 채널 질문의 0건 안내에 DM 으로 물을 수 있다는 사실을 붙인다 — 범위는 그대로
"""
from __future__ import annotations

import datetime as _dt

import pytest

from tybot import search_index as si
from tybot.access import RequestContext
from tybot.answer import CHANNEL_SCOPE_HINT, AnswerEngine, _question_retry_query
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

CHANNEL = "#현장_김해외동(180182)_채팅방"
CHANNEL_ID = "C0SITE00001"


class _Fake:
    name = "anthropic"

    def __init__(self):
        self.calls = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("기성금은 3억 2천만원입니다.", spec.model, self.name, 100, 20, 0.001)


def _write(tmp_path, line: str) -> None:
    today = _dt.date.today()
    path = tmp_path / "channels" / "pilot" / f"{CHANNEL_ID}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "workspace: pilot\n"
        f'channel: "{CHANNEL}"\n'
        f"channel_id: {CHANNEL_ID}\n"
        f"source_date: {today}\n"
        "visibility: private\n"
        f"acl: [{CHANNEL}]\n"
        "doc_count: 1\n"
        f"last_ingested: {today}T17:00+09:00\n"
        "---\n\n## 요약\n-\n\n## 원문 (자동 취합, 편집 금지)\n"
        f"> [{today} 09:00] 홍길동: {line}\n",
        encoding="utf-8",
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # 색인 DB 없이 파일 스캔 경로로 돈다 — 점수 함수는 같다.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake = _Fake()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router, allow_master_business_answers=True), fake


def _dm_ctx():
    return RequestContext(workspace="pilot", channels=frozenset({CHANNEL}))


def _channel_ctx():
    return RequestContext(
        workspace="pilot",
        channels=frozenset({CHANNEL}),
        channel_id=CHANNEL_ID,
        channel=CHANNEL,
        user_id="U1",
    )


# --- 1. 질문 낱말로 재검색 -----------------------------------------------------


def test_missed_classifier_terms_fall_back_to_the_question_words(engine, tmp_path):
    """분류기가 「미수금」 을 골랐지만 문서는 「기성금」 이라 적었다. 질문엔 그 낱말이 있다."""
    _write(tmp_path, "기성금 3억 2천만원 청구 완료")
    eng, fake = engine

    ans = eng.answer("이번 달 기성금 청구했나요", _dm_ctx(), terms=["미수금"])

    assert ans.reason == "answered"
    assert ans.citations, "근거 없이 답하면 안 된다 — 출처가 붙어야 한다"
    assert "3억 2천만원" in fake.calls[0][1].content


def test_still_no_evidence_when_the_question_words_miss_too(engine, tmp_path):
    """다시 찾아도 없으면 예전처럼 닫는다. 모델은 부르지 않는다."""
    _write(tmp_path, "어제 점심 메뉴 이야기")
    eng, fake = engine

    ans = eng.answer("기성금 청구했나요", _dm_ctx(), terms=["미수금"])

    assert ans.reason == "no_hits"
    assert "추측으로 답하지 않습니다" in ans.text
    assert fake.calls == []


def test_the_retry_never_leaves_the_channel_scope(engine, tmp_path):
    """채널 질문은 재검색도 그 채널 안에서만 한다(`can_access` 1번)."""
    _write(tmp_path, "어제 점심 메뉴 이야기")
    other = tmp_path / "channels" / "pilot" / "C0OTHER0001.md"
    today = _dt.date.today()
    other.write_text(
        "---\nworkspace: pilot\nchannel: \"#현장_다른곳(999999)_채팅방\"\n"
        "channel_id: C0OTHER0001\n"
        f"source_date: {today}\nvisibility: private\n"
        "acl: [#현장_다른곳(999999)_채팅방]\ndoc_count: 1\n"
        f"last_ingested: {today}T17:00+09:00\n---\n\n## 원문 (자동 취합, 편집 금지)\n"
        f"> [{today} 09:00] 김철수: 기성금 9억 청구\n",
        encoding="utf-8",
    )
    eng, fake = engine

    ans = eng.answer("기성금 청구했나요", _channel_ctx(), terms=["미수금"])

    assert ans.reason == "no_hits"
    assert "9억" not in ans.text
    assert fake.calls == []


def test_retry_query_uses_only_words_the_person_wrote():
    assert _question_retry_query("이번 달 기성금 청구했나요 알려줘", ["미수금"]) == (
        "이번 기성금 청구했나요"
    )
    # 핵심어에 이미 있던 낱말뿐이면 다시 찾을 이유가 없다.
    assert _question_retry_query("기성금 알려줘", ["기성금"]) == ""
    # 요청 표현·지칭어만 남으면 아무 줄이나 맞는다 — 찾지 않는다.
    assert _question_retry_query("방금 그거 다시 알려줘", ["보고서"]) == ""


# --- 2. 질문 어미 --------------------------------------------------------------


@pytest.mark.parametrize(("query", "expected"), [
    ("청구했나요", ["청구"]),
    ("기성금은요", ["기성금"]),
    ("진행하나요", ["진행"]),
    ("승인했는지", ["승인"]),
    ("완료입니까", ["완료"]),
    # 남는 말이 1자면 떼지 않는다
    ("할까요", ["할까요"]),
    # 명사 끝에 흔한 말은 목록에 없다
    ("수입인지", ["수입인지"]),
])
def test_question_endings_are_stripped_only_when_a_real_word_remains(query, expected):
    assert si.tokens_of(query) == expected


def test_stripping_question_endings_only_widens():
    for word in ["청구했나요", "기성금은요", "진행하나요", "승인했는지"]:
        stem = si.tokens_of(word)[0]
        assert word.startswith(stem) and len(stem) >= 2


# --- 3. 채널 0건 안내 ----------------------------------------------------------


def test_channel_no_evidence_points_to_dm_without_widening(engine, tmp_path):
    _write(tmp_path, "어제 점심 메뉴 이야기")
    eng, fake = engine

    ans = eng.answer("기성금 정산 결과가 어떻게 됐어?", _channel_ctx())

    assert ans.reason == "no_hits"
    assert "현재 채널에서 해당 근거를 찾지 못했습니다." in ans.text
    assert CHANNEL_SCOPE_HINT in ans.text
    assert "권한" not in ans.text.replace(CHANNEL_SCOPE_HINT, "")
    assert fake.calls == []
