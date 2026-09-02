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
