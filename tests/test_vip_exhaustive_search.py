"""VIPBot 전수 탐색 — 「근거 없음」 을 줄이려고 보이는 문서를 전부 본다 (2026-11-12 오너 요청).

실측(VIP 기록): 같은 질문이 07:29 에는 경영계획수립·조직성과관리 문서를 근거로 받아
답했고, 09:34 부터는 새로 들어온 ESG 문서 두 개가 상위 근거를 독차지해 「재무목표를
찾을 수 없다」 가 됐다. 거는 것:

1. VIP 엔진은 색인 후보 창을 거치지 않고 보이는 문서 전부를 점수 매긴다
2. 한 문서가 근거 자리를 독차지하지 않는다(문서당 상한)
3. 요청 표현(정리해·표로·설명해주세요·반드시)은 검색어가 아니다
4. 권한은 그대로다 — 전수 탐색도 보이는 문서 안에서만 돈다
5. TYBot 기본 경로는 바뀌지 않는다
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import answer as answer_module
from tybot import search_index
from tybot.access import RequestContext
from tybot.archive import attachment_doc, writer
from tybot.archive.store import ArchiveStore

WS, CH, CHANNEL = "mgmt", "C0PLAN0001", "#팀-기획_abb110-경영계획수립"
OTHER_CH, OTHER_CHANNEL = "C0ESG00001", "#팀-기획_abb110-esg"


def _attachment(root: Path, *, channel_id, channel, file_id, text, staged):
    doc = attachment_doc.AttachmentDoc(
        workspace=WS,
        channel_id=channel_id,
        channel=channel,
        file_id=file_id,
        name=f"{file_id}.pdf",
        revision=f"rev{file_id[-6:]}",
        visibility="private",
        acl=frozenset({channel}),
        text=text,
        conversion_state=attachment_doc.CONVERTED,
        message_ts="1794000000.000100",
        filetype="pdf",
        sha256="a" * 64,
        staged_at=staged,
    )
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")


def _archive(root: Path) -> ArchiveStore:
    for channel_id, channel in ((CH, CHANNEL), (OTHER_CH, OTHER_CHANNEL)):
        writer.ingest(
            root,
            workspace=WS,
            channel=channel,
            channel_id=channel_id,
            acl=[channel],
            messages=[
                writer.IncomingMessage(
                    datetime(2026, 11, 1, tzinfo=UTC),
                    "담당자",
                    "첨부 공유드립니다",
                )
            ],
        )
    # 옛 문서: 진짜 답(재무목표)이 든 경영계획.
    _attachment(
        root,
        channel_id=CH,
        channel=CHANNEL,
        file_id="F0PLAN0001",
        text="2026년 재무목표 1 부채비율 200% 이하\n2026년 재무목표 2 영업이익률 5%",
        staged="2026-10-01T00:00:00+00:00",
    )
    # 새 문서: 질문 낱말(2026년·연말·달성·목표)을 잔뜩 가진 ESG 문서. 줄이 많다.
    esg = "\n".join(f"2026년 연말까지 ESG 목표 {i} 달성해야 정리 표로 설명" for i in range(30))
    _attachment(
        root,
        channel_id=OTHER_CH,
        channel=OTHER_CHANNEL,
        file_id="F0ESG00001",
        text=esg,
        staged="2026-11-12T00:00:00+00:00",
    )
    return ArchiveStore(root)


def _ctx(channels=(CHANNEL, OTHER_CHANNEL)):
    return RequestContext(workspace=WS, channels=frozenset(channels))


Q = "태영건설에서 2026년 연말까지 반드시 달성해야 할 재무목표 Top5를 정리해 표로 설명해주세요"


def test_focus_query_drops_request_words_and_latin_josa():
    assert answer_module.focus_query(Q) == "태영건설 2026년 연말 달성해야 재무목표 top5"
    # 다 빼면 비는 질문은 원래 낱말을 쓴다.
    assert answer_module.focus_query("정리해줘") == "정리해줘"


def test_exhaustive_search_never_uses_the_index_window(tmp_path, monkeypatch):
    store = _archive(tmp_path)

    def window(*_a, **_k):
        raise AssertionError("전수 탐색은 색인 후보 창을 쓰지 않는다")

    monkeypatch.setattr(search_index, "candidates", window)
    query = answer_module.focus_query(Q)
    # 실측과 같은 모양: 점수만으로는 낱말을 더 많이 맞춘 ESG 줄이 자리를 다 차지한다.
    crowded = store.search(query, _ctx(), limit=10, exhaustive=True)
    assert not any("재무목표" in hit.line.text for hit in crowded)
    # 문서당 상한(VIP 설정)이 있으면 진짜 답이 든 문서가 근거에 들어온다.
    hits = store.search(query, _ctx(), limit=10, exhaustive=True, per_document=4)
    assert any("재무목표" in hit.line.text for hit in hits)


def test_one_document_cannot_take_every_evidence_slot(tmp_path, monkeypatch):
    store = _archive(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)
    query = "2026년 연말 달성해야 재무목표 목표"

    crowded = store.search(query, _ctx(), limit=6)
    diverse = store.search(query, _ctx(), limit=6, exhaustive=True, per_document=2)

    def docs(hits):
        return {str(hit.doc.path) for hit in hits}

    assert len(diverse) == 6
    assert any("재무목표" in hit.line.text for hit in diverse)
    assert len(docs(diverse)) >= len(docs(crowded))
    per_doc = {}
    for hit in diverse:
        per_doc[str(hit.doc.path)] = per_doc.get(str(hit.doc.path), 0) + 1
    # 상한은 「먼저 고르기」 다. 남는 자리는 채우되 다른 문서가 먼저 들어간다.
    assert sorted(per_doc.values())[0] >= 1


def test_exhaustive_search_stays_inside_the_visible_documents(tmp_path):
    store = _archive(tmp_path)
    hits = store.search("재무목표 ESG", _ctx(channels=(OTHER_CHANNEL,)), limit=20, exhaustive=True)
    assert hits
    assert all("재무목표 1" not in hit.line.text for hit in hits)


def test_per_document_zero_keeps_the_old_ranking(tmp_path, monkeypatch):
    store = _archive(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)
    query = "2026년 연말 달성해야 재무목표 목표"
    plain = store.search(query, _ctx(), limit=6)
    explicit = store.search(query, _ctx(), limit=6, per_document=0)
    assert [(str(h.doc.path), h.line.lineno) for h in plain] == [
        (str(h.doc.path), h.line.lineno) for h in explicit
    ]


def test_the_vip_engine_turns_exhaustive_search_on(monkeypatch, tmp_path):
    from tybot import vipbot_engine

    profile = SimpleNamespace(
        model="claude-sonnet-5-5",
        daily_limit_usd=10.0,
        cost_state_path=tmp_path / "c.json",
        prompts=None,
        max_output_tokens=1024,
    )
    engine = vipbot_engine.build_engine(
        archive_dir=tmp_path,
        profile=profile,
        anthropic_api_key="sk-ant-test",
        provider_factory=lambda **_k: SimpleNamespace(name="anthropic"),
    )
    assert engine._exhaustive_search is True
    assert engine._per_document_hits == vipbot_engine.VIP_PER_DOCUMENT_HITS
    assert engine._max_hits == vipbot_engine.VIP_MAX_HITS > 20


def test_the_tybot_default_engine_is_unchanged():
    engine = answer_module.AnswerEngine(SimpleNamespace(), SimpleNamespace())
    assert engine._exhaustive_search is False and engine._per_document_hits == 0
    calls = []
    engine._store = SimpleNamespace(search=lambda q, ctx, limit: calls.append((q, limit)) or [])
    assert engine._search("질문", _ctx()) == []
    assert calls == [("질문", 20)]


@pytest.mark.parametrize("word", sorted(answer_module.REQUEST_WORDS))
def test_request_words_never_include_a_subject_word(word):
    for subject in ("재무", "목표", "부채", "매출", "공정", "현장", "기성"):
        assert subject not in word
