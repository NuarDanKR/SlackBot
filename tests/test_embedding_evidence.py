"""벡터 후보 → 원문 좌표 → 출처 시험 (B-12).

**여기 쓰는 글은 전부 합성이다.** 실제 사내 원문·채널명·사람 이름을 넣지 않는다.

이 시험이 붙들고 있는 것은 넷이다.

1. 근거는 **원문 한 줄**이다. 청크 조각이 아니다(원칙 1)
2. 출처는 `SearchHit.citation()` 이 만든다 — 벡터 경로가 따로 만들지 않는다(원칙 2)
3. 좌표(`EvidenceRef`)는 **다시 열린다.** `ArchiveStore.resolve_refs` 가 연다
4. 권한이 바뀌면 그 좌표는 안 열린다
"""
from __future__ import annotations

import hashlib
import pathlib
from types import SimpleNamespace

import pytest

from tybot.access import RequestContext
from tybot.archive import embedding_evidence
from tybot.archive.chunking import split
from tybot.archive.embedding_evidence import (
    CHUNK_LINES_CAPPED,
    CHUNK_LINES_MISSING,
    MAX_LINES_PER_CHUNK,
    NO_LINE_COORDINATE,
    REF_LIMIT_REACHED,
    SOURCE_CHANGED,
    SOURCE_NOT_VISIBLE,
    evidence_for,
)
from tybot.archive.embedding_source import (
    ATTACHMENT,
    CHANNEL,
    VisibleSources,
    canonical_lines,
    canonical_text,
    normalize,
)
from tybot.archive.store import ArchiveDoc, ArchiveStore, RawLine
from tybot.embedding_resolver import resolve_candidates
from tybot.embedding_search import VectorCandidate
from tybot.evidence_refs import ARCHIVE_LINE, content_hash

WS = "pilot"
CTX = RequestContext(workspace=WS)
ME = RequestContext(workspace=WS, user_id="U1")

CHANNEL_NAME = "#팀_합성000(ZZZ000)_생성"
CHANNEL_ID = "C00000000"
RAW_REL = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
ATT_REL = "workspaces/pilot/channels/C00000000/attachments/F00000000/r1.md"


# --- 합성 아카이브 -----------------------------------------------------------


def _channel(root, *, days: int = 1, lines: int = 6) -> None:
    out = root / "workspaces" / WS / "channels" / CHANNEL_ID / "raw"
    out.mkdir(parents=True, exist_ok=True)
    for day in range(days):
        rows = "\n".join(
            f"> [2026-01-{day + 1:02d} 09:{i:02d} |17670{day}{i:04d}.0001] "
            f"화자{i % 3}: 합성 본문 {day}-{i}"
            for i in range(lines)
        )
        (out / f"2026-01-{day + 1:02d}.md").write_text(
            f'---\nworkspace: {WS}\nchannel: "{CHANNEL_NAME}"\n'
            f"channel_id: {CHANNEL_ID}\nvisibility: public\nacl: []\n"
            f"last_ingested: 2026-01-{day + 1:02d}T17:00+09:00\n---\n\n"
            f"## 원문 (자동 취합, 편집 금지)\n{rows}\n",
            encoding="utf-8",
        )


def _attachment(root, *, body: str | None = None) -> pathlib.Path:
    out = (
        root / "workspaces" / WS / "channels" / CHANNEL_ID / "attachments" / "F00000000"
    )
    out.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else "\n".join(
        ["# 합성 보고", "", "| 칸 | 값 |", "| --- | --- |", "| 가 | 1 |"]
    )
    path = out / "r1.md"
    path.write_text(
        f"---\nkind: attachment\nworkspace: {WS}\nchannel_id: {CHANNEL_ID}\n"
        f'channel: "{CHANNEL_NAME}"\n'
        f"file_id: F00000000\nfile_name: 합성보고.pdf\nrevision: r1\n"
        f"visibility: public\nacl: \nconversion_state: succeeded\n"
        f"message_ts: '170000000.000001'\nfiletype: pdf\nsha256: {'a' * 64}\n"
        f"staged_at: 2026-01-01T00:00+09:00\nconverted_at: 2026-01-01T01:00+09:00\n"
        f"converter_name: synth\nconverter_version: '1'\n"
        f"human_upload_confirmed: true\n---\n\n# 합성보고.pdf\n\n{text}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _attachment(tmp_path)
    return tmp_path


def _opened(root, ctx=CTX) -> VisibleSources:
    return VisibleSources(ArchiveStore(root), ctx)


def _candidate(source, start: int, end: int, number: int = 0) -> VectorCandidate:
    return VectorCandidate(
        workspace=source.workspace,
        channel_id=source.channel_id,
        doc_path=source.doc_path,
        source_revision=source.source_revision,
        chunker_revision="source-v1:chunk-v1:max=1200:ov=120",
        model_revision="synth-model-1",
        chunk_no=number,
        start_offset=start,
        end_offset=end,
        text_sha256=hashlib.sha256(source.text[start:end].encode("utf-8")).hexdigest(),
        distance=0.1,
    )


def _resolve(root, candidates, ctx=CTX):
    """창구 하나로 복원까지 — 실제 호출 순서 그대로."""
    sources = _opened(root, ctx)
    resolution = resolve_candidates(candidates, open_visible=sources.open)
    return sources, resolution


# --- 정본과 줄자리가 같은 함수에서 나온다 -------------------------------------


def test_the_spans_describe_the_text_they_were_built_with(archive):
    sources = _opened(archive)
    entry = sources.entry(WS, CHANNEL_ID, RAW_REL)

    assert entry is not None
    assert entry.spans
    joined = "".join(entry.source.text[s.start : s.end] for s in entry.spans)
    assert joined == entry.source.text
    for span in entry.spans:
        piece = entry.source.text[span.start : span.end]
        assert piece.endswith("\n")
        assert str(span.line.text) in piece


def test_asking_for_an_entry_opens_the_coordinate_itself(archive):
    """`entry()` 는 `open()` 과 **같은 문**을 지난다. 먼저 열어 둘 의무가 없다."""
    fresh = _opened(archive)

    entry = fresh.entry(WS, CHANNEL_ID, RAW_REL)

    assert entry is not None
    assert entry.source.doc_path == RAW_REL
    assert _opened(archive).entry(WS, "C00009999", RAW_REL) is None


def test_canonical_text_is_unchanged_by_the_span_rewrite(archive):
    """정본 글자가 한 글자라도 달라지면 색인된 지문이 전부 무효가 된다."""
    store = ArchiveStore(archive)
    for doc in store.docs():
        kind = ATTACHMENT if "attachments" in doc.path.parts else CHANNEL
        text, spans = canonical_lines(doc, doc.raw_lines, kind=kind)
        rows = [
            (str(line.text or "") if kind == ATTACHMENT
             else f"[{line.ts}] {line.speaker}: {line.text}")
            for line in sorted(doc.raw_lines, key=lambda ln: (ln.lineno, ln.ts))
        ]
        rows = [row for row in rows if row.strip()]
        old = normalize("\n".join(rows) + "\n") if rows else ""

        assert text == old
        assert text == canonical_text(doc, doc.raw_lines, kind=kind)
        assert len(spans) == len(rows)


def test_a_row_ending_in_a_lone_cr_keeps_the_old_canonical_text_contract(tmp_path):
    """경계의 `\r` + 구분 `\n` 은 CRLF 하나다. 줄바꿈 둘로 늘리면 안 된다."""
    path = tmp_path / "synthetic.md"
    doc = ArchiveDoc(
        path=path,
        workspace=WS,
        channel=CHANNEL_NAME,
        channel_id=CHANNEL_ID,
        visibility="public",
        acl=frozenset(),
        share_with=frozenset(),
        last_ingested=None,
    )
    rows = [
        RawLine("", "", "first\r", 1, path),
        RawLine("", "", "second", 2, path),
    ]

    text, spans = canonical_lines(doc, rows, kind=ATTACHMENT)

    assert text == normalize("first\r\nsecond\n") == "first\nsecond\n"
    assert [text[span.start : span.end] for span in spans] == ["first\n", "second\n"]


def test_a_nul_does_not_shift_the_spans(tmp_path, monkeypatch):
    """줄마다 고른 뒤 이어 붙이는 이유. 다 붙인 뒤 고르면 뒤쪽 자리가 밀린다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path, lines=3)
    raw = tmp_path / RAW_REL
    raw.write_text(
        raw.read_text(encoding="utf-8").replace("합성 본문 0-0", "합성\x00본문 0-0"),
        encoding="utf-8",
    )
    entry = _opened(tmp_path).entry(WS, CHANNEL_ID, RAW_REL)

    assert "\x00" not in entry.source.text
    for span in entry.spans:
        assert entry.source.text[span.start : span.end].endswith("\n")
    assert entry.spans[-1].end == len(entry.source.text)


# --- 1. 근거는 원문 한 줄이다 --------------------------------------------------


def test_a_chunk_becomes_the_original_lines_not_the_chunk_text(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    assert got.dropped == ()
    assert len(got.hits) == 6  # 청크 하나가 여섯 줄을 덮었다
    for hit in got.hits:
        # **정본 줄(`[시각] 화자: 본문`)이 아니라 원문 본문이다.**
        assert hit.line.text.startswith("합성 본문")
        assert not hit.line.text.startswith("[")


def test_the_hit_carries_the_document_the_line_came_from(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    for hit in got.hits:
        assert hit.doc.channel_id == CHANNEL_ID
        assert hit.doc.workspace == WS
        assert hit.line.source_path.as_posix().endswith("2026-01-01.md")


def test_only_the_lines_the_chunk_touches_come_back(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    entry = sources.entry(WS, CHANNEL_ID, RAW_REL)
    third = entry.spans[2]
    _, resolution = _resolve(archive, [_candidate(source, third.start, third.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    assert len(got.hits) == 1
    assert got.hits[0].line.text == "합성 본문 0-2"


def test_a_line_half_covered_by_the_chunk_still_counts(archive):
    """겹침을 둔 긴 행에서는 경계에 반 줄만 걸친다. 버리면 거기서 근거가 사라진다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    entry = sources.entry(WS, CHANNEL_ID, RAW_REL)
    first, second = entry.spans[0], entry.spans[1]
    middle = _candidate(source, first.end - 3, second.start + 3)
    _, resolution = _resolve(archive, [middle])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    assert [hit.line.text for hit in got.hits] == ["합성 본문 0-0", "합성 본문 0-1"]


# --- 2. 출처는 기존 자리에서 나온다 -------------------------------------------


def test_the_citation_comes_from_the_existing_path(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)
    citation = got.hits[0].citation()

    # 출처 문자열을 벡터 경로가 따로 만들지 않는다는 것이 요점이다.
    from tybot.channels import source_label

    assert citation.endswith("📄2026-01-01.md(2026-01-01)")
    assert citation.startswith(source_label(CHANNEL_NAME))
    assert "합성 본문" not in citation  # 출처에 본문을 싣지 않는다


def test_an_attachment_chunk_cites_the_attachment_file(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, ATT_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, ATT_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    assert got.hits
    assert all(hit.citation().endswith("📄r1.md(2026-01-01)") for hit in got.hits)
    assert any(hit.line.text == "| 가 | 1 |" for hit in got.hits)


# --- 3. 좌표는 다시 열린다 -----------------------------------------------------


def test_the_refs_reopen_through_resolve_refs(archive):
    """이 시험이 「연결됐다」 의 증거다 — 벡터가 만든 좌표를 기존 문이 연다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)
    got = evidence_for(resolution.resolved, sources, archive)

    hits, dropped = ArchiveStore(archive).resolve_refs(got.refs, CTX)

    assert dropped == []
    assert len(hits) == len(got.refs)
    assert [h.line.text for h in hits] == [h.line.text for h in got.hits]
    assert [h.citation() for h in hits] == [h.citation() for h in got.hits]


def test_every_ref_is_an_archive_line_coordinate_without_content(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    for ref, hit in zip(got.refs, got.hits, strict=True):
        assert ref.kind == ARCHIVE_LINE
        assert ref.document_path == RAW_REL
        assert ref.channel_id == CHANNEL_ID
        assert ref.line_no == hit.line.lineno
        assert ref.content_hash == content_hash(
            hit.line.ts, hit.line.speaker, hit.line.text
        )
        # 참조는 감사 기록(JSONL)에 실린다. 본문이 들어가면 아카이브 밖에
        # 원문 사본이 한 벌 더 생긴다.
        assert hit.line.text not in str(ref.to_json())


def test_an_edited_line_no_longer_reopens(archive):
    """좌표만 믿지 않는다 — 지문이 안 맞으면 그 줄이 아니다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)
    got = evidence_for(resolution.resolved, sources, archive)
    raw = archive / RAW_REL
    raw.write_text(
        raw.read_text(encoding="utf-8").replace("합성 본문 0-0", "다른 본문 0-0"),
        encoding="utf-8",
    )

    hits, dropped = ArchiveStore(archive).resolve_refs(got.refs, CTX)

    assert "hash_mismatch" in dropped
    assert all(h.line.text != "합성 본문 0-0" for h in hits)


# --- 4. 권한이 바뀌면 안 열린다 -------------------------------------------------


def test_a_candidate_from_another_requesters_view_is_not_turned_into_evidence(archive):
    """복원한 창구와 **다른** 창구로 넘기면 좌표가 안 열려 근거가 안 된다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    path = archive / RAW_REL
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("visibility: public", "visibility: private")
        .replace("acl: []", "acl: [#다른팀_합성(ZZZ999)_생성]"),
        encoding="utf-8",
    )

    got = evidence_for(resolution.resolved, _opened(archive), archive)

    assert got.hits == ()
    assert got.dropped == (SOURCE_NOT_VISIBLE,)
    assert not got


def test_a_source_changed_after_resolution_is_not_mapped_by_old_offsets(archive):
    """권한뿐 아니라 본문도 resolver와 출처 변환 사이에 바뀔 수 있다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    raw = archive / RAW_REL
    raw.write_text(
        raw.read_text(encoding="utf-8").replace("합성 본문 0-0", "교체 본문 0-0"),
        encoding="utf-8",
    )

    got = evidence_for(resolution.resolved, _opened(archive), archive)

    assert got.hits == ()
    assert got.dropped == (SOURCE_CHANGED,)


def test_a_forged_resolved_body_is_not_mapped_to_current_lines(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    candidate = _candidate(source, chunk.start, chunk.end)
    forged = SimpleNamespace(candidate=candidate, text="not the resolved chunk")

    got = evidence_for([forged], _opened(archive), archive)

    assert got.hits == ()
    assert got.dropped == (SOURCE_CHANGED,)


def test_refs_stop_reopening_after_the_permission_change(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)
    got = evidence_for(resolution.resolved, sources, archive)
    path = archive / RAW_REL
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("visibility: public", "visibility: private")
        .replace("acl: []", "acl: [#다른팀_합성(ZZZ999)_생성]"),
        encoding="utf-8",
    )

    hits, dropped = ArchiveStore(archive).resolve_refs(got.refs, CTX)

    assert hits == []
    assert dropped == ["permission_changed"]


# --- 상한과 사유 코드 -----------------------------------------------------------


def test_the_same_line_is_not_cited_twice(archive):
    """긴 행은 겹침을 두고 쪼개진다. 같은 줄이 두 청크에 들어 있다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    entry = sources.entry(WS, CHANNEL_ID, RAW_REL)
    first, second = entry.spans[0], entry.spans[1]
    overlapping = [
        _candidate(source, first.start, second.end, 0),
        _candidate(source, first.start, second.end, 1),
    ]
    _, resolution = _resolve(archive, overlapping)
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive)

    assert len(got.hits) == 2
    assert len({(r.document_path, r.line_no) for r in got.refs}) == 2


def test_one_chunk_cannot_eat_the_whole_reference_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path, lines=40)
    sources = _opened(tmp_path)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    _, resolution = _resolve(tmp_path, [_candidate(source, 0, len(source.text))])
    sources = _opened(tmp_path)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, tmp_path)

    assert len(got.hits) == MAX_LINES_PER_CHUNK
    assert CHUNK_LINES_CAPPED in got.dropped


def test_the_total_reference_limit_holds(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path, lines=40)
    sources = _opened(tmp_path)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    entry = sources.entry(WS, CHANNEL_ID, RAW_REL)
    many = [
        _candidate(source, span.start, span.end, i)
        for i, span in enumerate(entry.spans)
    ]
    _, resolution = _resolve(tmp_path, many)
    sources = _opened(tmp_path)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, tmp_path, limit=5)

    assert len(got.hits) == 5
    assert len(got.refs) == 5
    assert REF_LIMIT_REACHED in got.dropped


def test_a_single_chunk_respects_the_remaining_budget(archive):
    """상한이 바깥 고리에만 있으면 **한 청크 안에서** 예산을 넘겨 버린다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    _, resolution = _resolve(archive, [_candidate(source, 0, len(source.text))])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    got = evidence_for(resolution.resolved, sources, archive, limit=3)

    assert len(got.hits) == 3
    assert len(got.refs) == 3
    assert REF_LIMIT_REACHED in got.dropped


def test_a_fresh_window_opens_what_it_needs(archive):
    """부르는 쪽이 미리 열어 둘 의무가 없다. 열리는 문은 그대로 하나다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])

    got = evidence_for(resolution.resolved, _opened(archive), archive)

    assert got.hits
    assert got.dropped == ()


def test_an_empty_resolution_is_empty_evidence(archive):
    got = evidence_for([], _opened(archive), archive)

    assert got.hits == () and got.refs == () and got.dropped == ()
    assert not got


def test_a_chunk_outside_the_text_drops_without_an_exception(archive):
    """resolver 가 이미 거르지만 문이 하나뿐이면 안 된다."""
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)

    class _Far:
        candidate = type(
            "C",
            (),
            {
                "workspace": WS,
                "channel_id": CHANNEL_ID,
                "doc_path": RAW_REL,
                "start_offset": 10**6,
                "end_offset": 10**6 + 10,
            },
        )()

    got = evidence_for([_Far()], sources, archive)

    assert got.hits == ()
    assert got.dropped == (CHUNK_LINES_MISSING,)


def test_a_coordinate_the_window_never_opened_is_refused(archive):
    """이 창구로 복원한 것이 아니면 근거가 안 된다."""

    class _Elsewhere:
        candidate = type(
            "C",
            (),
            {
                "workspace": WS,
                "channel_id": "C00009999",
                "doc_path": "workspaces/pilot/channels/C00009999/raw/2026-01-01.md",
                "start_offset": 0,
                "end_offset": 10,
            },
        )()

    got = evidence_for([_Elsewhere()], _opened(archive), archive)

    assert got.hits == ()
    assert got.dropped == (SOURCE_NOT_VISIBLE,)


def test_a_line_without_a_channel_coordinate_is_dropped(archive, monkeypatch):
    """다음 요청에서 범위를 확인할 수 없는 참조는 남기지 않는다."""
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    chunk = split(source.text)[0]
    _, resolution = _resolve(archive, [_candidate(source, chunk.start, chunk.end)])
    sources = _opened(archive)
    sources.open(WS, CHANNEL_ID, RAW_REL)
    monkeypatch.setattr(embedding_evidence, "ref_from_hit", lambda hit, root: None)

    got = evidence_for(resolution.resolved, sources, archive)

    assert got.hits == ()
    assert got.dropped == (NO_LINE_COORDINATE,)


def test_the_drop_codes_carry_no_content(archive):
    sources = _opened(archive)
    source = sources.open(WS, CHANNEL_ID, RAW_REL)
    _, resolution = _resolve(archive, [_candidate(source, 0, len(source.text))])

    got = evidence_for(resolution.resolved, _opened(archive), archive)

    for code in got.dropped:
        assert code.replace("_", "").isalpha()
        assert "/" not in code and "합성" not in code


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 41}, "limit"),
        ({"per_chunk": 0}, "per_chunk"),
        ({"per_chunk": MAX_LINES_PER_CHUNK + 1}, "per_chunk"),
    ],
)
def test_reference_caps_cannot_be_disabled_or_expanded(archive, kwargs, message):
    with pytest.raises(ValueError, match=message):
        evidence_for([], _opened(archive), archive, **kwargs)
