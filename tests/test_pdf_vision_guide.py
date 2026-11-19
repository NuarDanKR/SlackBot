"""B-80 PDF 쪽 Vision 전사 → 답변 근거. 검색·권한·원본 재검증·후속 좌표·fail-closed.

DB 는 대역이다(`_PageConn`). 실제 SQL 모양은 `test_pdf_vision_guide_db.py` 가 폐기 DB 로 본다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tybot import pdf_vision_guide as guide
from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive import attachment_doc, writer
from tybot.archive.files import attachment_storage
from tybot.archive.store import ArchiveStore, RawLine, SearchHit
from tybot.evidence_refs import (
    VISION_PAGE,
    EvidenceRef,
    refs_from_hits,
    refs_from_json,
    refs_to_json,
)
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

WS, CH, CHANNEL = "tyit", "C0SITE0001", "#현장_김해외동(180182)_채팅방"
OTHER_CH, OTHER_CHANNEL = "C0OTHER001", "#팀_자금(ABB540)_주간보고"
FID = "F0PDF00001"
PDF_NAME = "하도급계약서.pdf"
PDF = b"%PDF-1.4\n" + b"synthetic scanned contract page bytes\n" * 20
SHA = hashlib.sha256(PDF).hexdigest()
TRANSCRIPT = "하도급 계약금액 3억 2천만원\n지급 조건 기성 90% 준공 10%"
REV = "rev-page3-0001"


# --- 아카이브 ---------------------------------------------------------------


def _archive(root: Path, *, sha: str = SHA, with_object: bool = True, pdf: bytes = PDF,
             state: str = attachment_doc.PARTIAL, channel: str = CHANNEL,
             channel_id: str = CH, human: bool = True) -> None:
    """채널 원문(첨부 줄) + 부분 변환 첨부 정본 + 원본 PDF."""
    writer.ingest(
        root, workspace=WS, channel=channel, channel_id=channel_id, acl=[channel],
        messages=[
            writer.IncomingMessage(
                datetime(2026, 11, 12, 1, 0, tzinfo=UTC), "김현장",
                f"[첨부:자동변환] {PDF_NAME} (pdf, 1MB) · id:{FID}",
                source_ts="1794000000.000100",
            )
        ],
    )
    doc = attachment_doc.AttachmentDoc(
        workspace=WS, channel_id=channel_id, channel=channel, file_id=FID, name=PDF_NAME,
        revision="rev0000000001", visibility="private", acl=frozenset({channel}),
        text="1쪽 표지 하도급계약서", conversion_state=state,
        message_ts="1794000000.000100", filetype="pdf", sha256=sha,
        staged_at="2026-11-12T01:00:00+00:00", human_upload_confirmed=human,
    )
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")
    if with_object:
        objects = attachment_storage(root, WS, channel_id).objects_dir / FID
        objects.mkdir(parents=True, exist_ok=True)
        (objects / PDF_NAME).write_bytes(pdf)


def _row(**over) -> dict:
    row = {
        "revision_id": REV, "workspace": WS, "channel_id": CH, "file_id": FID,
        "page_number": 3, "original_sha256": SHA, "status": "extracted",
        "body": TRANSCRIPT, "created_at": "2026-11-19 01:00:00+00",
    }
    row.update(over)
    return row


class _PageConn:
    """`pdf_page_search_entry` 조회 대역. 세 질의(낱말·같은 쪽·revision)를 흉내 낸다."""

    def __init__(self, rows, *, fail: bool = False):
        self.rows = list(rows)
        self.fail = fail
        self.sql: list = []
        self.closed = False

    def close(self):
        self.closed = True

    def cursor(self):
        conn = self

        class _Cur:
            rows: tuple = ()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params):
                if conn.fail:
                    raise RuntimeError("db down")
                conn.sql.append((sql, params))
                if "ids" in params:
                    self.rows = [r for r in conn.rows if r["revision_id"] in params["ids"]]
                elif "fid" in params:
                    keys = set(zip(params["ws"], params["ch"], params["fid"], params["pg"],
                                   strict=True))
                    self.rows = [
                        r for r in conn.rows
                        if (r["workspace"], r["channel_id"], r["file_id"], r["page_number"])
                        in keys
                    ]
                else:
                    words = [
                        v[1:-1].replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\")
                        for k, v in sorted(params.items()) if k.startswith("kw")
                    ]
                    pairs = set(zip(params["scope_ws"], params["scope_ch"], strict=True))
                    self.rows = [
                        r for r in conn.rows
                        if (r["workspace"], r["channel_id"]) in pairs
                        and r["status"] in ("extracted", "uncertain")
                        and any(w in r["body"].replace(" ", "").lower() for w in words)
                    ]

            def fetchall(self):
                return list(self.rows)

        return _Cur()


def _ctx(channels=(CHANNEL,), **kw):
    return RequestContext(workspace=WS, channels=frozenset(channels), **kw)


def _vip(sources=((WS, CH),)):
    return RequestContext(workspace="vip", evidence_sources=frozenset(sources))


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "archive"
    _archive(root)
    return ArchiveStore(root)


# --- 1. 최초 질문 검색 -------------------------------------------------------


def test_first_question_finds_the_page_and_labels_it_as_a_derived_transcript(store):
    assert not store.search("계약금액", _ctx(), limit=20)  # 로컬 본문만으로는 못 찾는다

    hits = guide.expand(store, _ctx(), "계약금액 얼마", conn=_PageConn([_row()]))

    assert len(hits) == 1
    hit = hits[0]
    assert hit.line.text.startswith("[AI 문서 전사·파생 · PDF 3쪽] ")
    assert "3억 2천만원" in hit.line.text
    citation = hit.citation()
    assert f"📄{PDF_NAME} 3쪽" in citation and "[AI 문서 전사·파생]" in citation
    assert (hit.revision_id, hit.file_id, hit.page_number, hit.original_sha256) == (
        REV, FID, 3, SHA)
    assert hit.line.message_ts == "1794000000.000100"  # 출처가 원본 첨부 메시지로 간다


def test_only_visible_channels_are_queried(store):
    conn = _PageConn([_row()])
    guide.expand(store, _ctx(), "계약금액", conn=conn)
    sql, params = conn.sql[0]
    assert params["scope_ws"] == [WS] and params["scope_ch"] == [CH]
    # 좌표 **쌍**으로 LIMIT 전에 자른다. ws·채널을 따로 ANY 로 걸지 않는다.
    assert "unnest(%(scope_ws)s::text[], %(scope_ch)s::text[])" in sql
    assert "= ANY(" not in sql
    assert sql.index("unnest(%(scope_ws)s") < sql.index("LIMIT")


def test_cross_pairs_of_visible_workspaces_and_channels_are_not_queried(tmp_path):
    """(A,ch1)·(B,ch2) 가 보이면 (A,ch2)·(B,ch1) 은 후보가 아니다 — LIMIT 을 먹지 않는다."""
    pairs = {("A", "C1"), ("B", "C2")}
    rows = [
        _row(workspace="A", channel_id="C2", revision_id="cross-1"),
        _row(workspace="B", channel_id="C1", revision_id="cross-2"),
        _row(workspace="A", channel_id="C1", revision_id="ok-1"),
    ]
    got = guide.active_entries(pairs, tokens=["계약금액"], conn=_PageConn(rows))
    assert [e.revision_id for e in got] == ["ok-1"]


def test_evidence_ref_kind_literal_names_vision_page():
    import typing

    kinds = typing.get_args(typing.get_type_hints(EvidenceRef)["kind"])
    assert set(kinds) == {"archive_line", "live_message", VISION_PAGE}


# --- 2. TYBot·VIPBot 같은 답변 경로 -----------------------------------------


class _Provider:
    name = "anthropic"

    def __init__(self):
        self.calls = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("계약금액은 3억 2천만원입니다.", spec.model, self.name, 10, 5, 0.0)


def _engine(store):
    provider = _Provider()
    router = Router(
        providers={"anthropic": provider},
        registry={"claude-sonnet-5": ModelSpec("claude-sonnet-5", "anthropic", 3.0, 15.0,
                                               Sensitivity.CONFIDENTIAL)},
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(store, router, allow_master_business_answers=True), provider


@pytest.mark.parametrize("ctx", [_ctx(), _vip()], ids=["tybot", "vipbot"])
def test_tybot_and_vipbot_answer_through_the_same_path(store, monkeypatch, ctx):
    conn = _PageConn([_row()])
    monkeypatch.setattr(guide, "_connect", lambda: conn)
    engine, provider = _engine(store)

    answer = engine.answer("계약금액 얼마야", ctx)

    assert answer.reason == "answered"
    prompt = provider.calls[0][1].content
    assert "[AI 문서 전사·파생 · PDF 3쪽]" in prompt and "3억 2천만원" in prompt
    assert any(f"{PDF_NAME} 3쪽" in c and "AI 문서 전사·파생" in c for c in answer.citations)
    refs = [r for r in answer.evidence_refs if r.kind == VISION_PAGE]
    assert [(r.revision_id, r.file_id, r.page_number, r.original_sha256) for r in refs] == [
        (REV, FID, 3, SHA)]
    # QA 로그에 들어가는 참조에 전사 문장이 없다
    logged = json.dumps(refs_to_json(answer.evidence_refs), ensure_ascii=False)
    assert "3억" not in logged and "계약금액" not in logged and "지급 조건" not in logged


@pytest.mark.parametrize("ctx", [_ctx(), _vip()], ids=["tybot", "vipbot"])
def test_document_set_summary_uses_the_page_transcript(store, monkeypatch, ctx):
    """명시적 문서 질의(`document_query`·`terms`) 요약도 같은 근거·같은 출처·같은 좌표."""
    conn = _PageConn([_row()])
    monkeypatch.setattr(guide, "_connect", lambda: conn)
    engine, provider = _engine(store)

    answer = engine.summarize(ctx, question="하도급계약서 계약금액 정리해줘",
                              terms=["계약금액"], document_query=["하도급계약서"],
                              all_time=True)

    assert answer.reason == "answered"
    prompt = provider.calls[0][1].content
    assert "[AI 문서 전사·파생 · PDF 3쪽]" in prompt and "3억 2천만원" in prompt
    assert any(f"{PDF_NAME} 3쪽" in c and "AI 문서 전사·파생" in c for c in answer.citations)
    refs = [r for r in answer.evidence_refs if r.kind == VISION_PAGE]
    assert [(r.revision_id, r.page_number) for r in refs] == [(REV, 3)]


def test_a_plain_period_summary_does_not_open_page_transcripts(store, monkeypatch):
    """낱말 없는 기간 요약은 전 쪽을 열지 않는다 — DB 에 붙지도 않는다."""
    opened = []
    monkeypatch.setattr(guide, "_connect", lambda: opened.append(1) or _PageConn([_row()]))
    engine, provider = _engine(store)
    engine.summarize(_ctx(), all_time=True)
    assert opened == []
    assert all("AI 문서 전사" not in m.content for call in provider.calls for m in call)


def test_summary_page_transcripts_respect_the_workspace_filter(store, monkeypatch):
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    engine, _ = _engine(store)
    assert engine._summary_vision_hits(["계약금액"], _ctx(),
                                       workspace_filter=frozenset({"other"})) == []
    assert len(engine._summary_vision_hits(["계약금액"], _ctx())) == 1
    assert engine._summary_vision_hits([], _ctx()) == []


@pytest.mark.parametrize("ctx", [_ctx(), _vip()], ids=["tybot", "vipbot"])
def test_advice_uses_the_page_transcript_as_an_in_house_fact(store, monkeypatch, ctx):
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    engine, provider = _engine(store)

    answer = engine.advise("계약금액 기준으로 보면 지급 조건이 적정한지 판단해줘", ctx,
                           terms=["계약금액", "지급"])

    prompt = " ".join(m.content for m in provider.calls[0])
    assert "[AI 문서 전사·파생 · PDF 3쪽]" in prompt and "3억 2천만원" in prompt
    assert answer.reason == "advice"
    assert any(f"{PDF_NAME} 3쪽" in c and "AI 문서 전사·파생" in c for c in answer.citations)


def test_summary_follow_up_cites_the_page_not_the_file(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    hits, _ = store.resolve_refs(refs, _ctx())
    engine, _ = _engine(store)
    answer = engine.summarize(_ctx(), question="다시 정리", evidence_hits=hits)
    assert any(f"{PDF_NAME} 3쪽" in c and "AI 문서 전사·파생" in c for c in answer.citations)


# --- 3. 범위 밖 차단 ---------------------------------------------------------


def test_another_channel_is_blocked(store):
    assert guide.expand(store, _ctx(channels=(OTHER_CHANNEL,)), "계약금액",
                        conn=_PageConn([_row()])) == []


def test_another_workspace_row_is_never_used(store):
    rows = [_row(workspace="other", revision_id="rev-other")]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []


def test_vip_source_outside_the_registered_set_is_blocked(store):
    assert guide.expand(store, _vip(sources=((WS, OTHER_CH),)), "계약금액",
                        conn=_PageConn([_row()])) == []
    assert guide.expand(store, _vip(sources=()), "계약금액", conn=_PageConn([_row()])) == []


def test_a_projection_row_for_a_channel_the_requester_cannot_see_is_dropped(tmp_path):
    root = tmp_path / "archive"
    _archive(root)
    _archive(root, channel=OTHER_CHANNEL, channel_id=OTHER_CH)
    store = ArchiveStore(root)
    rows = [_row(channel_id=OTHER_CH, revision_id="rev-other")]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []


# --- 4. 원본·좌표 재검증 -----------------------------------------------------


def test_a_missing_original_pdf_drops_the_page(tmp_path):
    root = tmp_path / "archive"
    _archive(root, with_object=False)
    assert guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn([_row()])) == []


def test_changed_original_bytes_drop_the_page(tmp_path):
    root = tmp_path / "archive"
    _archive(root, pdf=PDF + b"tampered")
    assert guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn([_row()])) == []


def test_a_reconverted_document_with_a_new_hash_drops_old_coordinates(tmp_path):
    other = hashlib.sha256(b"other").hexdigest()
    root = tmp_path / "archive"
    _archive(root, sha=other)
    assert guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn([_row()])) == []


def test_a_blocked_current_document_drops_its_transcripts(tmp_path):
    root = tmp_path / "archive"
    _archive(root, state=attachment_doc.BLOCKED)
    assert guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn([_row()])) == []


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path):
    root = tmp_path / "archive"
    fake = b"not a pdf at all"
    _archive(root, pdf=fake, sha=hashlib.sha256(fake).hexdigest())
    rows = [_row(original_sha256=hashlib.sha256(fake).hexdigest())]
    assert guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn(rows)) == []


def test_a_converter_app_or_legacy_pdf_without_the_human_flag_is_still_evidence(tmp_path):
    """오너 결정 2026-11-19: PDF 쪽 전사는 「사람 업로드 확인」 을 요구하지 않는다.

    변환 앱 PDF(사람이 올린 HWP 의 변환본)·옛 문서·파일 목록 소급은 그 값이 거짓이다.
    봇 출력 되먹임은 수집 입구(봇·앱 메시지 배제)가 막는다.
    """
    root = tmp_path / "archive"
    _archive(root, human=False)
    hits = guide.expand(ArchiveStore(root), _ctx(), "계약금액", conn=_PageConn([_row()]))
    assert len(hits) == 1


# --- 5. PII 재검사 -----------------------------------------------------------


def test_pii_is_rechecked_at_read_time(store):
    rows = [_row(body="하도급 계약금액 3억\n대표자 주민번호 900101-1234567")]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []
    rows = [_row(body="하도급 계약금액 3억\n담당 연락처 010-1234-5678")]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []


# --- 6. 상태 ----------------------------------------------------------------


@pytest.mark.parametrize("status", ["uncertain", "failed", "withheld"])
def test_only_extracted_pages_become_evidence(store, status):
    assert guide.expand(store, _ctx(), "계약금액",
                        conn=_PageConn([_row(status=status)])) == []


def test_a_newer_uncertain_revision_does_not_fall_back_to_an_older_extracted(store):
    rows = [
        _row(),
        _row(revision_id="rev-page3-0002", status="uncertain", body="판독 불명",
             created_at="2026-11-19 02:00:00+00"),
    ]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []


@pytest.mark.parametrize("status", ["failed", "withheld"])
def test_a_newer_tombstone_blocks_the_older_extracted_in_search_and_follow_up(
    store, monkeypatch, status
):
    """최신 판이 실패·보류 묘비(본문 없음)면 옛 extracted 는 검색에도 후속에도 없다."""
    _, refs = _follow_refs(store, _ctx())
    tomb = _row(revision_id="rev-page3-0002", status=status, body="",
                created_at="2026-11-19 02:00:00+00")
    rows = [_row(), tomb]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn(rows))
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and dropped == [guide.DROP_PROJECTION]


def test_revision_order_uses_the_real_timestamp_not_its_text_shape(store):
    """DB 가 돌려주는 datetime 은 UTC 고정 폭으로 비교한다(시간대·소수 자릿수 무관)."""
    from datetime import timedelta, timezone

    old = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
    # 같은 순간 뒤의 판을 다른 시간대(+09:00)로 표현 — 글자로는 「더 작아」 보인다.
    newer = (old + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=-9)))
    rows = [
        _row(created_at=old),
        _row(revision_id="rev-page3-0002", status="failed", body="", created_at=newer),
    ]
    assert guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows)) == []
    rows = [
        _row(created_at=old.replace(microsecond=1)),
        _row(revision_id="rev-page3-0002", status="failed", body="", created_at=old),
    ]
    assert len(guide.expand(store, _ctx(), "계약금액", conn=_PageConn(rows))) == 1


def test_verify_refuses_a_non_extracted_entry_directly(store):
    entry = guide.PageEntry(REV, WS, CH, FID, 3, SHA, "uncertain", TRANSCRIPT)
    hit, code = guide.verify(store, _ctx(), entry, {(WS, CH)})
    assert hit is None and code == guide.DROP_STATUS


# --- 7·8. 후속 질문 ----------------------------------------------------------


def _follow_refs(store, ctx):
    hits = guide.expand(store, ctx, "계약금액", conn=_PageConn([_row()]))
    logged = json.loads(json.dumps(refs_to_json(refs_from_hits(hits, store.root))))
    return logged, refs_from_json(logged)


def test_follow_up_stores_only_coordinates(store):
    logged, _refs = _follow_refs(store, _ctx())
    assert logged == [{
        "kind": VISION_PAGE, "workspace": WS, "channel_id": CH, "document_path": "",
        "line_no": 0, "source_ts": "", "content_hash": "", "message_ts": "",
        "revision_id": REV, "file_id": FID, "page_number": 3, "original_sha256": SHA,
    }]
    assert "3억" not in json.dumps(logged, ensure_ascii=False)


def test_follow_up_restores_the_page_from_coordinates(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))

    hits, dropped = store.resolve_refs(refs, _ctx())

    assert dropped == []
    assert len(hits) == 1 and hits[0].line.text.startswith("[AI 문서 전사·파생 · PDF 3쪽]")
    assert "3억 2천만원" in hits[0].line.text  # 문장은 DB 에서 다시 읽었다


def test_follow_up_rechecks_permission(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    hits, dropped = store.resolve_refs(refs, _ctx(channels=(OTHER_CHANNEL,)))
    assert hits == [] and guide.DROP_ACL in dropped
    hits, dropped = store.resolve_refs(refs, _vip(sources=((WS, OTHER_CH),)))
    assert hits == []


def test_follow_up_rechecks_the_original(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    obj = attachment_storage(store.root, WS, CH).objects_dir / FID / PDF_NAME
    obj.write_bytes(PDF + b"changed")
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and "original_hash_mismatch" in dropped
    obj.unlink()
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and "original_missing" in dropped


def test_follow_up_drops_a_missing_or_superseded_projection(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([]))
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and dropped == [guide.DROP_PROJECTION]
    newer = _row(revision_id="rev-page3-0002", created_at="2026-11-19 03:00:00+00")
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row(), newer]))
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and dropped == [guide.DROP_PROJECTION]


def test_follow_up_refuses_tampered_coordinates(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    forged = EvidenceRef(**{**refs[0].to_json(), "page_number": 4})
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()]))
    hits, dropped = store.resolve_refs([forged], _ctx())
    assert hits == [] and dropped == [guide.DROP_COORDINATE]


def test_follow_up_with_the_db_down_opens_nothing(store, monkeypatch):
    _, refs = _follow_refs(store, _ctx())
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()], fail=True))
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and dropped == [guide.DROP_DB]
    monkeypatch.setattr(guide, "_connect", lambda: None)
    hits, dropped = store.resolve_refs(refs, _ctx())
    assert hits == [] and dropped == [guide.DROP_DB]


@pytest.mark.parametrize("row", [
    {"revision_id": "../x"},
    {"page_number": 0},
    {"original_sha256": "abc"},
    {"file_id": ""},
])
def test_malformed_vision_refs_from_the_log_are_dropped(row):
    base = {"kind": VISION_PAGE, "workspace": WS, "channel_id": CH, "revision_id": REV,
            "file_id": FID, "page_number": 3, "original_sha256": SHA}
    assert refs_from_json([{**base, **row}]) == []


def test_archive_line_refs_keep_their_old_json_shape(store):
    hit = SearchHit(doc=store.visible_channel_docs(_ctx())[0],
                    line=RawLine("2026-11-12 10:00", "김현장", "x", 1), score=1)
    row = refs_to_json(refs_from_hits([hit], store.root))[0]
    assert "revision_id" not in row and "page_number" not in row


# --- 9. DB 장애 → 기존 검색만 -------------------------------------------------


def test_db_failure_keeps_existing_search_only(store, monkeypatch):
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([_row()], fail=True))
    assert guide.expand(store, _ctx(), "계약금액") == []
    engine, _ = _engine(store)
    base = store.search("하도급계약서", _ctx(), limit=20)
    assert engine._with_pdf_vision_guide("하도급계약서", base, _ctx()) == base


def test_no_database_url_reads_no_documents(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    class _Store:
        def visible_channel_docs(self, ctx):
            raise AssertionError("문서를 읽으면 안 된다")

    assert guide.expand(_Store(), _ctx(), "계약금액") == []


def test_answer_with_db_down_still_answers_from_ordinary_search(store, monkeypatch):
    monkeypatch.setattr(guide, "_connect", lambda: _PageConn([], fail=True))
    engine, provider = _engine(store)
    answer = engine.answer("하도급계약서 표지", _ctx())
    assert answer.reason == "answered"
    assert "AI 문서 전사" not in provider.calls[0][1].content


# --- 스키마 ------------------------------------------------------------------


def test_schema_index_expression_matches_the_guide_query():
    sql = (Path(__file__).resolve().parents[1] / "deploy/sql/archiver_vision_schema.sql"
           ).read_text(encoding="utf-8")
    assert guide.ENTRY_COMPACT_BODY.replace("e.", "") in sql


def test_the_guide_reads_only_the_projection_never_the_vision_ledger():
    source = Path(guide.__file__).read_text(encoding="utf-8")
    for table in ("archiver_vision_page_revision", "archiver_vision_job",
                  "archiver_vision_policy", "archiver_vision_approval"):
        assert table not in source
