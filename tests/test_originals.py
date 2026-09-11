"""첨부 원본 전송 — 검수 게이트 · 형식 제한 · 상한.

검토: docs/design/trust-and-usability-review.md §3

스캔 PDF 는 텍스트 레이어가 없어 우리 전처리에서 `미변환` 이 되고, 결과적으로 제목만
아카이브된다. 원본을 그대로 보내면 모델이 이미지로 읽는다.

**승인 게이트가 안전장치의 전부다.** 수집 단계 PII 거절은 텍스트 기반이라 스캔본에
작동하지 않는다 — 우리가 못 읽어서 못 걸러낸 것이 벤더로 가는 유일한 경로가 여기다.
"""
from __future__ import annotations

import json

import pytest

from tybot import documents
from tybot.answer import _attachment_source_links
from tybot.archive.store import ArchiveDoc, RawLine, SearchHit
from tybot.attachment_review import (
    APPROVED,
    PENDING,
    REJECTED,
    approve,
    find_approved,
    find_sendable,
    pending,
    reject,
    scan,
    summary,
)


def _stage(tmp_path, *, name="보고서.pdf", ws="mgmt", ch="C1", status=PENDING,
           body=b"%PDF-1.4 fake", file_id="F1"):
    """`stage_files` 가 만드는 것과 같은 모양으로 만든다."""
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    suffix = f"workspaces/{ws}/channels/{ch}/attachments/{file_id}"
    staged = tmp_path / "staging" / suffix
    objects = tmp_path / "objects" / suffix
    staged.mkdir(parents=True, exist_ok=True)
    objects.mkdir(parents=True, exist_ok=True)
    obj = objects / name
    obj.write_bytes(body)
    (staged / "metadata.json").write_text(json.dumps({
        "schema_version": 1, "status": status, "slack_file_id": file_id,
        "name": name, "filetype": name.rsplit(".", 1)[-1], "mimetype": "",
        "declared_size": len(body), "object_path": str(obj),
    }, ensure_ascii=False), encoding="utf-8")
    return archive


# --- 검수 --------------------------------------------------------------------
def test_new_attachments_start_pending():
    """기본이 승인이면 게이트가 없는 것과 같다."""
    assert PENDING != APPROVED


def test_scan_finds_staged_items(tmp_path):
    archive = _stage(tmp_path)
    (item,) = scan(archive)
    assert item.workspace == "mgmt"
    assert item.channel_id == "C1"
    assert item.name == "보고서.pdf"
    assert item.status == PENDING


def test_pending_filters_by_status(tmp_path):
    archive = _stage(tmp_path)
    assert len(pending(archive)) == 1
    approve(scan(archive)[0], actor="dan")
    assert pending(archive) == []


def test_approval_records_who_and_when(tmp_path):
    """나중에 '이건 왜 나갔나' 를 답할 수 있어야 한다."""
    archive = _stage(tmp_path)
    done = approve(scan(archive)[0], actor="dan@taeyoung.com", note="사내 공지, PII 없음")
    assert done.status == APPROVED
    assert done.approved_by == "dan@taeyoung.com"
    assert done.approved_at
    assert "PII 없음" in done.note


def test_approval_without_an_actor_is_refused(tmp_path):
    archive = _stage(tmp_path)
    with pytest.raises(ValueError, match="승인자"):
        approve(scan(archive)[0], actor="  ")


def test_reject_is_reversible_and_keeps_the_bytes(tmp_path):
    """원본을 지우면 오판을 다시 검토할 근거가 사라진다."""
    archive = _stage(tmp_path)
    approve(scan(archive)[0], actor="dan")
    done = reject(scan(archive)[0], actor="dan", note="개인정보 포함")
    assert done.status == REJECTED
    assert done.object_path.is_file()


def test_find_approved_only_returns_approved(tmp_path):
    archive = _stage(tmp_path)
    assert find_approved(archive, workspace="mgmt", channel_id="C1", name="보고서.pdf") is None
    approve(scan(archive)[0], actor="dan")
    assert find_approved(archive, workspace="mgmt", channel_id="C1", name="보고서.pdf")


def test_find_approved_respects_the_channel(tmp_path):
    archive = _stage(tmp_path)
    approve(scan(archive)[0], actor="dan")
    assert find_approved(archive, workspace="mgmt", channel_id="C9", name="보고서.pdf") is None
    assert find_approved(archive, workspace="pilot", channel_id="C1", name="보고서.pdf") is None


def test_ambiguous_name_is_not_resolved(tmp_path):
    """어느 것인지 모르는 채로 원본을 벤더에 보내지 않는다."""
    archive = _stage(tmp_path, file_id="F1")
    _stage(tmp_path, file_id="F2")
    for item in scan(archive):
        approve(item, actor="dan")
    assert find_approved(archive, workspace="mgmt", channel_id="C1", name="보고서.pdf") is None


def test_summary_lists_pending(tmp_path):
    archive = _stage(tmp_path)
    text = summary(pending(archive))
    assert "검수 대기 1건" in text
    assert "보고서.pdf" in text


def test_missing_staging_dir_is_not_an_error(tmp_path):
    assert scan(tmp_path / "없음" / "archive") == []


def test_review_log_has_no_filename(tmp_path, caplog):
    """검수 로그가 파일명 목록이 되면 그 자체가 자료 목록이 된다."""
    archive = _stage(tmp_path, name="김해외동_기성금.pdf")
    with caplog.at_level("INFO"):
        approve(scan(archive)[0], actor="dan")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "김해외동_기성금.pdf" not in logged
    assert "actor=dan" in logged


# --- 무엇이 사람을 기다리는가 (2026-09-08) -----------------------------------
#
# 처음에는 **모든** 첨부가 승인을 기다렸다. 그래서 대기 31건·승인 0건이 되었고,
# 사용자는 승인이 필요한지조차 몰랐다. 게이트가 아니라 정체였다.
#
# 막으려던 것은 하나다 — 텍스트가 없어 PII 검사가 돌지 않는 파일.
def test_a_converted_file_does_not_wait_for_a_human(tmp_path):
    """변환본이 아카이브에 들어갔다 = 수집 단계 PII 검사를 통과했다.

    그 파일까지 대기시키면 정작 사람이 봐야 할 스캔본이 목록에 묻힌다.
    """
    archive = _stage(tmp_path, name="가정산서.xlsx", status=PENDING)

    got = find_sendable(archive, workspace="mgmt", channel_id="C1",
                        name="가정산서.xlsx", text_extracted=True)

    assert got is not None, "변환된 파일이 승인을 기다린다"


def test_a_scan_still_waits(tmp_path):
    """이미지·스캔본에는 글자가 없어 PII 검사가 **아예 작동하지 않는다.**

    우리가 못 읽어서 못 걸러낸 것이 벤더로 가는 유일한 경로가 여기다.
    """
    archive = _stage(tmp_path, name="스캔본.png", status=PENDING)

    got = find_sendable(archive, workspace="mgmt", channel_id="C1",
                        name="스캔본.png", text_extracted=False)

    assert got is None


def test_a_rejection_beats_the_conversion(tmp_path):
    """사람이 안 된다고 한 것을 자동 판정이 되돌리면 그 판단이 의미를 잃는다."""
    archive = _stage(tmp_path, name="명단.xlsx", status=REJECTED)

    got = find_sendable(archive, workspace="mgmt", channel_id="C1",
                        name="명단.xlsx", text_extracted=True)

    assert got is None


def test_an_approval_beats_the_missing_conversion(tmp_path):
    """사람이 보고 승인한 스캔본은 변환이 안 됐어도 나간다 — 그게 승인의 쓸모다."""
    archive = _stage(tmp_path, name="도면.png", status=APPROVED)

    got = find_sendable(archive, workspace="mgmt", channel_id="C1",
                        name="도면.png", text_extracted=False)

    assert got is not None


def test_an_ambiguous_name_is_never_sent(tmp_path):
    """같은 이름이 둘이면 어느 것인지 모른다. 모르는 채로 원본을 벤더에 보내지 않는다."""
    _stage(tmp_path, name="보고서.pdf", status=PENDING, file_id="F1")
    archive = _stage(tmp_path, name="보고서.pdf", status=PENDING, file_id="F2")

    got = find_sendable(archive, workspace="mgmt", channel_id="C1",
                        name="보고서.pdf", text_extracted=True)

    assert got is None


def test_the_answer_path_never_collects_original_bytes():
    """답변 엔진은 승인 상태와 무관하게 첨부 원본을 외부 모델에 보내지 않는다."""
    import inspect

    from tybot import answer

    source = inspect.getsource(answer.AnswerEngine)
    assert "documents.collect" not in source
    assert "find_sendable" not in source
    assert not hasattr(answer, "_originals")


# --- 형식 --------------------------------------------------------------------
class FakeItem:
    def __init__(self, name, path, size=None):
        self.name = name
        self.object_path = path
        exists = bool(path) and path.is_file()
        self.size = size if size is not None else (path.stat().st_size if exists else 0)


def _file(tmp_path, name, body=b"x" * 100):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_pdf_becomes_a_document_block_with_citations(tmp_path):
    got = documents.collect([FakeItem("보고서.pdf", _file(tmp_path, "보고서.pdf"))])
    (block,) = got.blocks
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    assert block["citations"] == {"enabled": True}
    assert "\n" not in block["source"]["data"]  # base64 에 개행이 있으면 거부된다


def test_image_becomes_an_image_block(tmp_path):
    got = documents.collect([FakeItem("사진.png", _file(tmp_path, "사진.png"))])
    (block,) = got.blocks
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"


@pytest.mark.parametrize("name", ["표.xlsx", "발표.pptx", "문서.docx", "보고.hwp"])
def test_office_formats_are_refused_with_a_reason(tmp_path, name):
    """문서 블록 타입이 아니다. 조용히 빠지면 사람이 읽었다고 믿는다."""
    got = documents.collect([FakeItem(name, _file(tmp_path, name))])
    assert got.blocks == []
    assert any(name in s for s in got.skipped)
    assert "원본 전송이 안 됩니다" in got.skipped[0]


def test_missing_original_is_reported(tmp_path):
    got = documents.collect([FakeItem("없음.pdf", tmp_path / "없음.pdf")])
    assert got.blocks == []
    assert "찾지 못함" in got.skipped[0]


def test_oversized_file_is_skipped(tmp_path):
    big = _file(tmp_path, "큰것.pdf", b"x" * 2048)
    got = documents.collect([FakeItem("큰것.pdf", big)], max_file_bytes=1024)
    assert got.blocks == []
    assert "너무 큼" in got.skipped[0]


def test_file_count_is_capped(tmp_path):
    items = [FakeItem(f"{i}.pdf", _file(tmp_path, f"{i}.pdf")) for i in range(5)]
    got = documents.collect(items, max_files=2)
    assert len(got.blocks) == 2
    assert any("2개까지만" in s for s in got.skipped)


def test_total_size_is_capped(tmp_path):
    items = [FakeItem(f"{i}.pdf", _file(tmp_path, f"{i}.pdf", b"x" * 900)) for i in range(3)]
    got = documents.collect(items, max_total_bytes=1500)
    assert len(got.blocks) == 1
    assert any("용량 한도" in s for s in got.skipped)


def test_note_reports_both_sides(tmp_path):
    got = documents.collect([
        FakeItem("보고서.pdf", _file(tmp_path, "보고서.pdf")),
        FakeItem("표.xlsx", _file(tmp_path, "표.xlsx")),
    ])
    note = got.note()
    assert "원본 첨부로 읽음: 보고서.pdf" in note
    assert "표.xlsx" in note


def test_empty_input_produces_nothing(tmp_path):
    got = documents.collect([])
    assert not got.any
    assert got.note() == ""


def test_filenames_are_not_logged(tmp_path, caplog):
    with caplog.at_level("INFO"):
        documents.collect([FakeItem("김해외동_기성금.pdf", _file(tmp_path, "김해외동_기성금.pdf"))])
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "김해외동_기성금.pdf" not in logged
    assert "included=1" in logged


# --- 페이지 출처 --------------------------------------------------------------
def test_citation_lines_carry_page_numbers():
    """API 가 붙인 구조화된 인용이라, 우리가 지어낸 출처가 아니다."""
    content = [{"citations": [
        {"document_title": "보고서.pdf", "start_page_number": 7, "end_page_number": 7},
        {"document_title": "보고서.pdf", "start_page_number": 9, "end_page_number": 11},
    ]}]
    assert documents.citation_lines(content) == ["📄보고서.pdf 7p", "📄보고서.pdf 9-11p"]


def test_citation_lines_are_deduped():
    content = [{"citations": [{"document_title": "a.pdf", "start_page_number": 1}]},
               {"citations": [{"document_title": "a.pdf", "start_page_number": 1}]}]
    assert documents.citation_lines(content) == ["📄a.pdf 1p"]


def test_citation_lines_tolerate_missing_pages():
    assert documents.citation_lines([{"citations": [{"document_title": "a.txt"}]}]) == ["📄a.txt"]


def test_citation_lines_handle_plain_text_blocks():
    assert documents.citation_lines([{"type": "text", "text": "..."}]) == []
    assert documents.citation_lines(None) == []


# --- 답변 경로 ----------------------------------------------------------------
def test_attachment_line_pattern_extracts_the_name():
    from tybot.answer import ATTACHMENT_RE

    m = ATTACHMENT_RE.match("[첨부:검수대기] 김해외동 기성금.pdf (pdf, 240KB)")
    assert m and m.group("name") == "김해외동 기성금.pdf"
    assert ATTACHMENT_RE.match("그냥 대화입니다") is None


def test_extracted_attachment_source_includes_original_slack_link(tmp_path):
    marker = RawLine(
        "2026-09-02 09:00",
        "사용자",
        "[첨부:변환·원본검수대기] 보고서.xlsx (xlsx, 10KB) · "
        "<https://example.slack.com/files/F1|원본 파일>",
        1,
    )
    extracted = RawLine(
        "2026-09-02 09:00", "사용자", "[첨부추출:보고서.xlsx] 기성금 3억", 2
    )
    doc = ArchiveDoc(
        tmp_path / "doc.md",
        "pilot",
        "#업무",
        "private",
        frozenset({"#업무"}),
        frozenset(),
        None,
        channel_id="C1",
        raw_lines=[marker, extracted],
    )

    links = _attachment_source_links([SearchHit(doc, extracted, 10)])
    assert links == ["📎<https://example.slack.com/files/F1|보고서.xlsx 원본>"]


# ---------------------------------------------------------------------------
# 만들어 낸 channel_id 가 같은 채널을 둘로 가르던 것
# ---------------------------------------------------------------------------


def _doc_with_id(channel: str, channel_id: str, date: str, text: str) -> str:
    return (
        "---\n"
        "workspace: tyit\n"
        f'channel: "{channel}"\n'
        f"channel_id: {channel_id}\n"
        "visibility: private\n"
        f'acl: ["{channel}"]\n'
        f"last_ingested: {date}T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
        f"> [{date} 09:00] 홍길동: {text}\n"
    )


def test_a_synthetic_channel_id_does_not_split_one_channel(tmp_path):
    """`legacy-…` 는 **디렉터리 이름용 자리표시자**다.

    그 값이 프론트매터에 실려 신원으로 쓰이면 같은 채널의 v1·v2 문서가 서로 다른
    채널이 된다. 2026-09-07 실측:

        '#팀-전산_abb155-공지' C0BS5HTLE2E     tyit 2026-09-03.md 200줄
        '#팀-전산_abb155-공지' legacy-a219b21… tyit 2026-09-07.md  60줄

    출처가 두 줄 나오고, 더 나쁘게는 **권한 메타가 따로 계산된다** —
    `visibility`·`acl`·`share_with` 를 그룹마다 정하므로 같은 채널이 한쪽으로는
    보이고 다른 쪽으로는 안 보일 수 있다.
    """
    from tybot.archive.store import ArchiveStore

    channel = "#팀-전산_abb155-공지"
    base = tmp_path / "channels" / "tyit"
    base.mkdir(parents=True)
    (base / "real.md").write_text(
        _doc_with_id(channel, "C0BS5HTLE2E", "2026-09-03", "예산 편성"), encoding="utf-8"
    )
    (base / "legacy.md").write_text(
        _doc_with_id(channel, "legacy-a219b21337d5", "2026-09-07", "예산 확정"),
        encoding="utf-8",
    )

    docs = ArchiveStore(tmp_path).docs()
    same = [d for d in docs if d.channel == channel]

    assert len(same) == 1, f"한 채널이 {len(same)}개 문서로 갈렸다"
    assert same[0].channel_id == "C0BS5HTLE2E", "진짜 Slack ID 로 묶여야 한다"
    assert len(same[0].raw_lines) == 2, "두 파일의 원문이 함께 들어와야 한다"


def test_two_real_ids_are_still_separate_channels(tmp_path):
    """진짜 ID 가 다르면 다른 채널이다. 이름이 같아도 합치지 않는다 —

    개명 이력으로 이름이 겹칠 수 있고, 그때 합치면 서로 다른 채널의 원문이 한 답변에
    섞인다(권한이 넘어간다).
    """
    from tybot.archive.store import ArchiveStore

    channel = "#팀-전산_abb155-공지"
    base = tmp_path / "channels" / "tyit"
    base.mkdir(parents=True)
    (base / "a.md").write_text(
        _doc_with_id(channel, "C111", "2026-09-03", "가"), encoding="utf-8"
    )
    (base / "b.md").write_text(
        _doc_with_id(channel, "C222", "2026-09-07", "나"), encoding="utf-8"
    )

    docs = [d for d in ArchiveStore(tmp_path).docs() if d.channel == channel]

    assert len(docs) == 2, "진짜 ID 가 다르면 다른 채널이다"


def test_a_synthetic_id_is_not_assigned_when_two_real_channels_share_a_name(tmp_path):
    """이름이 모호하면 합성 문서를 실제 채널 어느 쪽에도 섞지 않는다."""
    from tybot.archive.store import ArchiveStore, is_synthetic_channel_id

    channel = "#팀_전산(ABB155)_공지"
    base = tmp_path / "channels" / "tyit"
    base.mkdir(parents=True)
    for name, channel_id, text in (
        ("a.md", "C111", "첫 채널"),
        ("b.md", "C222", "둘째 채널"),
        ("legacy.md", "legacy-ambiguous", "옛 문서"),
    ):
        (base / name).write_text(
            _doc_with_id(channel, channel_id, "2026-09-07", text), encoding="utf-8"
        )

    docs = [d for d in ArchiveStore(tmp_path).docs() if d.channel == channel]

    assert len(docs) == 3
    legacy = next(d for d in docs if is_synthetic_channel_id(d.channel_id))
    assert [line.text for line in legacy.raw_lines] == ["옛 문서"]
    assert all(len(doc.raw_lines) == 1 for doc in docs)


def test_the_synthetic_prefix_lives_where_it_is_minted():
    """판정과 생성이 두 곳에 있으면 한쪽만 고쳐져 조용히 어긋난다."""
    from tybot.archive.store import SYNTHETIC_ID_PREFIX, is_synthetic_channel_id
    from tybot.archive.writer import SYNTHETIC_ID_PREFIX as MINTED

    assert MINTED == SYNTHETIC_ID_PREFIX, "생성과 판정의 접두사가 갈렸다"
    assert is_synthetic_channel_id(f"{SYNTHETIC_ID_PREFIX}abc123")
    assert not is_synthetic_channel_id("C0BS5HTLE2E")
    assert not is_synthetic_channel_id(None)
