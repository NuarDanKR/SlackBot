"""답변 파이프라인 — 환각방지 4겹 회귀 테스트."""
from __future__ import annotations

import pytest

from tybot import documents
from tybot.access import RequestContext
from tybot.answer import AnswerEngine, parse_model_flag
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.router import Router
from tybot.intent import Intent

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
    return AnswerEngine(
        ArchiveStore(tmp_path), router, allow_master_business_answers=True
    ), fake


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


def test_answer_sends_an_ocr_screened_image_with_the_text_evidence(engine, monkeypatch):
    eng, fake = engine
    visual = documents.Attached(
        blocks=[{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
        }],
        included=["현장사진.png"],
        skipped=[],
    )
    monkeypatch.setattr("tybot.answer._visual_originals", lambda root, hits: visual)

    ans = eng.answer("기성금 얼마야", _ctx())

    assert ans.reason == "answered"
    content = fake.calls[0][1].content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "3억 2천만원" in content[0]["text"]
    assert content[1]["type"] == "image"


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


# --- 좁힌 범위에서 되돌아 나오는 문 (2026-09-18 패킷 QA ec366cd4·43142c76) -----
class _Followup:
    """`ThreadFollowupResolver` 가 돌려주는 모양만 흉내 낸다."""

    def __init__(self, *, topic_terms=(), hits=(), dropped=()):
        self.evidence_hits = list(hits)
        self.attachments = []
        self.parent_record_ids = ["prev"]
        self.topic_terms = list(topic_terms)
        self.resolution = "prior_topic"
        self.dropped_codes = list(dropped)
        self.needs_clarification = False
        self.choices = []
        self.refs_requested = 3
        self.editing_text = ""
        self.applied = True
        self.empty = not self.evidence_hits

    def log_line(self) -> str:
        return f"followup_resolution={self.resolution} refs_resolved={len(self.evidence_hits)}"


def test_a_widened_follow_up_is_not_a_dead_end(engine):
    """사용자가 「넓혀서 찾아줘」 라고 했는데 봇이 좁힌 자리에 서 있었다.

    이전 근거에서 못 찾으면 **현재 권한으로 다시 검색한다.** 넓혔다는 사실은
    답에 적는다 — 조용히 넓히면 좁게 물은 사람이 그 사실을 알 수 없다.
    """
    eng, _ = engine
    intent = Intent("search", terms=["기성금"], question="기성금 쪽으로 넓혀서 찾아줘")

    ans = eng._respond_scoped(
        "기성금 쪽으로 넓혀서 찾아줘",
        _ctx(),
        intent,
        _Followup(topic_terms=["기성금"], dropped=["topic_no_match"]),
    )

    assert ans.reason == "answered"
    assert "3억 2천만원" in ans.text or ans.citations
    assert "넓혀" in ans.text or "다시 찾" in ans.text
    assert ans.context_resolution == "widened_after_scope_miss"


def test_a_purely_deictic_follow_up_still_refuses_to_widen(engine):
    """「방금 그거 다시」 는 넓힐 주제가 없다. 넓히면 엉뚱한 답이 그 자리에 온다."""
    eng, _ = engine
    intent = Intent("search", terms=[], question="방금 그거 다시 보여줘")

    ans = eng._respond_scoped(
        "방금 그거 다시 보여줘", _ctx(), intent, _Followup(dropped=["hash_mismatch"])
    )

    assert ans.reason == "no_hits"
    assert "다시 확인하지 못했습니다" in ans.text


def test_pointing_at_attachments_is_not_asking_their_status():
    """「너가 말해준 첨부들 기준으로 요약해줘」 는 **범위**이지 상태 질문이 아니다.

    둘을 같은 값으로 묶었더니 요약 요청에 「관련 파일 상태 • xxx.xlsx — 변환 완료」
    한 줄만 나갔다(2026-09-22 운영). 물은 것의 절반이 아니라 0% 다.
    """
    from tybot.intent import asks_attachment_status

    for pointing in (
        "그 첨부들 기준으로 요약해줘",
        "너가 말해준 첨부파일들 기준으로 월간 회의 내용 알려줘",
    ):
        assert not asks_attachment_status(pointing), pointing
    for asking in ("첨부 변환 상태 알려줘", "처리 안 된 첨부 알려줘", "그 파일 변환 됐어?"):
        assert asks_attachment_status(asking), asking


def test_a_status_block_never_replaces_the_answer(engine):
    """상태를 묻지 않았으면 상태 블록은 아예 붙지 않는다.

    예전에는 첨부가 있기만 하면 블록이 따라 붙었고, 근거가 0건인 순간 그 블록이
    답을 통째로 대체했다. 근거로 쓴 첨부의 변환 상태는 `evidence_note()` 가 알린다.
    """
    from pathlib import Path

    from tybot.attachment_review import Attachment

    eng, _ = engine
    followup = _Followup(topic_terms=["기성금"], dropped=[])
    followup.attachments = [Attachment(
        workspace="pilot", channel_id="C1", file_id="F1", name="주간보고.xlsx",
        filetype="xlsx", mimetype="application/vnd.ms-excel", size=10,
        status="converted", object_path=None, meta_path=Path("meta.json"),
    )]
    intent = Intent("summary", terms=["기성금"], question="그 첨부들 기준으로 요약해줘")
    assert not intent.include_attachment_status

    ans = eng._respond_scoped("그 첨부들 기준으로 요약해줘", _ctx(), intent, followup)

    assert "관련 파일 상태" not in ans.text
    assert "3억 2천만원" in ans.text or ans.citations, "본문 없이 닫혔다"
