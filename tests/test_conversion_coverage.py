"""얼마나 읽었나 — `partial` 과 페이지·시트 coverage (B-45 §5·§6).

설계: `docs/design/operational-warning-recovery-and-answer-progress.md`

이 파일이 지키는 것 둘.

1. **「본문이 나왔다」 와 「다 읽었다」 는 다르다.** 10쪽에서 3쪽만 읽혀도
   예전에는 성공이었고, 그 답에 우리 출처가 붙었다.
2. **모르는 개수는 `null`** 이다. 0 이나 100% 로 만들면 모르는 것을 안다고 적는
   셈이고, 그 순간 그 숫자는 근거가 아니다.
"""
from __future__ import annotations

import pytest

from tybot.archive import convert as cv


def test_a_fully_read_document_is_succeeded():
    cov = cv.Coverage(unit="page", total=3, converted=3)
    assert cov.state == cv.SUCCEEDED


def test_a_partly_read_document_is_partial_not_succeeded():
    cov = cv.Coverage(unit="page", total=10, converted=3, missing=(4, 5, 6))
    assert cov.state == cv.PARTIAL
    assert "확인 3/10쪽" in cov.summary()
    assert "미확인" in cov.summary()


def test_an_unknown_count_is_never_faked():
    """외부 도구가 상세를 안 주면 모른다. 0 도 100% 도 아니다."""
    cov = cv.Coverage()
    assert cov.state == cv.UNKNOWN
    assert cov.summary() == ""
    row = cov.to_json()
    assert row["coverage_total"] is None
    assert row["coverage_converted"] is None


def test_a_quality_flag_alone_makes_it_partial():
    """행 상한에 걸린 시트는 숫자로는 전부 읽은 것처럼 보인다."""
    cov = cv.Coverage(unit="sheet", total=2, converted=2)
    cov.note(cv.ROW_LIMIT_REACHED)
    assert cov.state == cv.PARTIAL


def test_coverage_carries_no_document_text():
    """추적에 본문이 섞이면 metadata 가 원문 사본이 된다."""
    cov = cv.Coverage(unit="page", total=3, converted=1, missing=(2,), unverified=(3,))
    row = cov.to_json()
    assert set(row) == {
        "coverage_unit", "coverage_total", "coverage_converted",
        "missing_units", "unverified_units", "quality_flags", "coverage_state",
    }
    assert all(isinstance(n, int) for n in row["missing_units"])
    # 그림 미해독 쪽도 **좌표뿐이다.** 무엇이 그려져 있었는지는 담지 않는다.
    assert all(isinstance(n, int) for n in row["unverified_units"])


def test_missing_units_are_capped():
    """미확인이 수천 개면 metadata 한 줄이 그것으로 찬다."""
    cov = cv.Coverage(unit="page", total=5000, converted=1, missing=tuple(range(2, 5000)))
    assert len(cov.to_json()["missing_units"]) <= 50


# --- 실제 변환 경로 ----------------------------------------------------------


def _pdf(pages: list[str]) -> bytes:
    """외부 의존 없이 최소 PDF 를 만든다.

    `reportlab` 에 기대면 그게 없는 곳에서 **검사가 조용히 skip 된다.**
    건너뛴 검사는 검사가 아니다 — 여기서 막으려는 것이 바로 「읽은 척」 이다.
    """
    import io as _io

    nl = b"\n"
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    content_ids: list[int] = []
    for text in pages:
        stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode() if text else b" "
        head = b"<< /Length %d >>" % len(stream)
        content_ids.append(
            add(head + nl + b"stream" + nl + stream + nl + b"endstream")
        )
    pages_id = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content_id)
        )
        for content_id in content_ids
    ]
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = _io.BytesIO()
    out.write(b"%PDF-1.4" + nl)
    offsets: list[int] = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj" % i + nl + body + nl + b"endobj" + nl)
    xref = out.tell()
    out.write(b"xref" + nl + b"0 %d" % (len(objects) + 1) + nl)
    out.write(b"0000000000 65535 f " + nl)
    for off in offsets:
        out.write(b"%010d 00000 n " % off + nl)
    out.write(
        b"trailer" + nl
        + b"<< /Size %d /Root %d 0 R >>" % (len(objects) + 1, catalog) + nl
        + b"startxref" + nl + b"%d" % xref + nl + b"%%EOF" + nl
    )
    return out.getvalue()


def test_a_pdf_with_blank_pages_is_partial():
    """그림만 있는 쪽인지 추출이 실패한 쪽인지 **모른다.** 읽었다고 세지 않는다."""
    # 글자가 200자 미만이면 OCR 폴백을 시도하고, 그게 없으면 `ocr_unavailable`
    # 플래그가 붙는다 — 여기서 보려는 것은 **쪽 누락**이므로 길게 둔다.
    data = _pdf(["First page body long enough that the extracted text stays well above the two hundred character floor that triggers the OCR fallback", "", "Third page body long enough that the extracted text stays well above the two hundred character floor that triggers the OCR fallback"])

    lines, cov = cv.convert_with_coverage("pdf", data)

    assert lines
    assert cov.unit == "page"
    assert cov.total == 3
    assert cov.converted == 2
    assert 2 in cov.missing
    assert cov.state == cv.PARTIAL


def test_a_fully_readable_pdf_is_not_marked_partial():
    """고친 쪽으로 너무 넓히면 멀쩡한 문서가 전부 미확인으로 보인다."""
    data = _pdf(["First page body long enough that the extracted text stays well above the two hundred character floor that triggers the OCR fallback", "Second page body long enough that the extracted text stays well above the two hundred character floor that triggers the OCR fallback"])

    _lines, cov = cv.convert_with_coverage("pdf", data)

    assert cov.state == cv.SUCCEEDED
    assert cov.missing == ()


def test_a_title_only_result_is_not_a_success():
    """제목 아래 빈 단락은 성공이 아니다(설계 §5)."""
    cov = cv.Coverage(unit="page", total=1, converted=1)
    assert cov.state == cv.SUCCEEDED  # 숫자만으로는 성공으로 보인다
    # `convert_with_coverage` 가 본문 유무를 한 번 더 본다.
    assert cv._has_body(["[1쪽]", "# 제목"]) is False
    assert cv._has_body(["[1쪽]", "본문 한 줄"]) is True


def test_coverage_is_only_collected_inside_a_conversion():
    """변환 밖에서 기록하면 다른 요청의 숫자가 섞인다."""
    assert cv.current_coverage() is None


# --- 화면에 닿는가 -----------------------------------------------------------


def test_a_partial_attachment_is_not_labelled_converted(tmp_path):
    """「변환 완료」 라고 하면 사람은 전부 읽은 줄 안다."""
    from tybot.attachment_review import Attachment, status_label, status_line

    item = Attachment(
        workspace="pilot", channel_id="C1", file_id="F1", name="정산.pdf",
        filetype="pdf", mimetype="application/pdf", size=1, status="converted",
        object_path=None, meta_path=tmp_path / "m.json", extracted=True,
        conversion_state="partial", coverage_unit="page",
        coverage_total=10, coverage_converted=3, missing_units=(4, 5),
    )

    assert status_label(item) == "부분 변환"
    assert "확인 3/10쪽" in status_line(item)


def test_an_old_attachment_without_coverage_still_reads(tmp_path):
    """구형 metadata 에는 필드가 없다. 그것 때문에 화면이 깨지면 안 된다."""
    from tybot.attachment_review import Attachment, status_label

    item = Attachment(
        workspace="pilot", channel_id="C1", file_id="F1", name="옛파일.pdf",
        filetype="pdf", mimetype="application/pdf", size=1, status="converted",
        object_path=None, meta_path=tmp_path / "m.json", extracted=True,
    )

    assert status_label(item) == "변환 완료"
    assert item.coverage_note == ""


def test_mixed_pdf_unverified_pages_are_visible_in_status(tmp_path):
    """Extracted text does not mean an image on the same page was understood."""
    from tybot.attachment_review import Attachment, status_line

    item = Attachment(
        workspace="pilot", channel_id="C1", file_id="F1", name="report.pdf",
        filetype="pdf", mimetype="application/pdf", size=1, status="converted",
        object_path=None, meta_path=tmp_path / "m.json", extracted=True,
        conversion_state="partial", coverage_unit="page", coverage_total=2,
        coverage_converted=2, unverified_units=(2,),
    )

    assert item.coverage_note == "확인 2/2쪽 · 그림 미해독 2쪽"
    assert "부분 변환" in status_line(item)
    assert "그림 미해독 2쪽" in status_line(item)


def test_unverified_pages_survive_metadata_scan(tmp_path):
    import json

    from tybot.attachment_review import scan, staging_root

    root = tmp_path / "archive"
    folder = staging_root(root) / "pilot" / "channels" / "C1" / "attachments" / "F1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({
        "name": "report.pdf", "status": "converted", "conversion_state": "partial",
        "coverage_unit": "page", "coverage_total": 2, "coverage_converted": 2,
        "unverified_units": [2],
    }), encoding="utf-8")

    (item,) = scan(root)
    assert item.unverified_units == (2,)
    assert "그림 미해독 2쪽" in item.coverage_note


def test_a_page_that_raises_is_counted_as_missing(monkeypatch):
    """손상된 쪽은 **조용히 건너뛰던** 자리다.

    한 쪽 실패가 전체를 막지 않는 것은 맞지만, 그 사실을 안 남기면 3쪽만 읽은
    10쪽 문서가 「성공」 이 된다. 빈 쪽(글자 없음)과는 다른 갈래라 따로 본다.
    """
    import pypdf

    real_reader = pypdf.PdfReader

    class Boom:
        def extract_text(self):
            raise RuntimeError("손상된 쪽")

    class Reader:
        is_encrypted = False

        def __init__(self, stream):
            self._inner = real_reader(stream)
            # 둘째 쪽만 터뜨린다. 나머지는 진짜 쪽이다.
            self.pages = [self._inner.pages[0], Boom(), self._inner.pages[2]]

    long = (
        "Page body long enough that the extracted text stays well above the two "
        "hundred character floor that triggers the OCR fallback path in convert"
    )
    data = _pdf([long, long, long])
    monkeypatch.setattr(pypdf, "PdfReader", Reader)

    _lines, cov = cv.convert_with_coverage("pdf", data)

    assert cov.total == 3
    assert cov.converted == 2
    assert 2 in cov.missing, "터진 쪽을 안 세면 부분 변환이 성공으로 보인다"
    assert cov.state == cv.PARTIAL


def test_a_title_only_conversion_is_flagged_through_the_real_path():
    """`_has_body()` 를 직접 부르는 검사만으로는 **배선**을 증명하지 못한다."""
    data = _pdf(["# Heading only"])

    lines, cov = cv.convert_with_coverage("pdf", data)

    assert lines, "본문 줄 자체는 나온다"
    assert cv.TITLE_ONLY in cov.flags
    assert cov.state == cv.PARTIAL


def test_old_metadata_without_coverage_reads_as_unknown_not_zero(tmp_path):
    """**구형 metadata 를 실제로 읽어** 확인한다.

    `Attachment` 를 손으로 만들면 `_as_count()` 를 지나지 않아, 없는 개수를
    0 으로 바꿔도 검사가 통과한다.
    """
    import json

    from tybot.attachment_review import scan, staging_root

    d = staging_root(tmp_path / "archive") / "pilot" / "channels" / "C1" / "attachments" / "F1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        json.dumps({"name": "옛파일.pdf", "status": "converted", "extracted": True}),
        encoding="utf-8",
    )

    (item,) = scan(tmp_path / "archive")

    assert item.coverage_total is None, "모르는 개수를 0 으로 바꾸면 안 된다"
    assert item.coverage_converted is None
    assert item.coverage_note == ""


def test_collection_writes_coverage_into_the_metadata(tmp_path, monkeypatch):
    """수집 경로가 coverage 를 안 남기면 화면에도 큐에도 아무것도 안 간다."""
    import json

    from tybot.archive import files as files_mod
    from tybot.archive.files import attachment_storage, stage_files

    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    monkeypatch.setattr(files_mod, "download_bytes", lambda *_: b"x")

    def fake_convert(filetype, data):
        cv._record("page", total=4, converted=1, missing=(2, 3, 4))
        return ["본문 한 줄"]

    monkeypatch.setattr(files_mod, "convert", fake_convert)

    stage_files(
        [{"id": "F1", "name": "정산.pdf", "filetype": "pdf", "size": 5,
          "url_private_download": "https://example.invalid/f"}],
        "xoxb-test",
        storage,
    )

    meta = json.loads((storage.staging_dir / "F1" / "metadata.json").read_text("utf-8"))

    assert meta["coverage_total"] == 4
    assert meta["coverage_converted"] == 1
    assert meta["missing_units"] == [2, 3, 4]
    assert meta["conversion_state"] == "partial", "부분을 성공으로 닫으면 안 된다"


# =============================================================================
# 폴백·슬라이드
# =============================================================================


def test_a_fallback_converter_is_not_equal_to_the_precise_one():
    """폴백이 성공해도 **동등하다고 표시하지 않는다**(설계 §6).

    글자는 나와도 도형·이미지 안의 값은 안 나온다. 그걸 성공으로 닫으면
    사람은 전부 본 줄 안다.
    """
    from tybot.archive import external_convert as ext

    cov = cv.Coverage()
    cov.note(cv.FALLBACK_CONVERTER)

    assert cov.state == cv.PARTIAL
    assert ext.ExternalConverterUnavailable.code == "converter_missing"


def test_a_slide_without_text_is_not_counted_as_read(monkeypatch):
    """그림만 있는 슬라이드를 읽은 것으로 세면, 그 그림이 표일 때 답이 틀린다."""
    pptx = pytest.importorskip("pptx")

    prs = pptx.Presentation()
    blank = prs.slide_layouts[6]
    with_text = prs.slide_layouts[5]
    slide = prs.slides.add_slide(with_text)
    slide.shapes.title.text = "3분기 기성 현황"
    prs.slides.add_slide(blank)

    import io as _io

    buf = _io.BytesIO()
    prs.save(buf)

    with cv.collect_coverage() as cov:
        cv._pptx_basic(buf.getvalue())

    assert cov.unit == "slide"
    assert cov.total == 2
    assert cov.converted == 1
    assert 2 in cov.missing


# =============================================================================
# 답변 「근거」 줄
# =============================================================================


def test_the_evidence_note_names_partly_read_attachments():
    """**실패와 다르다.** 이쪽은 답이 나가므로 범위를 안 밝히면 전부 본 줄 안다."""
    from tybot.answer import Answer

    answer = Answer(
        "기성금은 3억입니다.", ["#현장, 📄doc.md(2026-09-10)"], "m", 0.0, 3, "answered",
        terms=["기성금"],
        partial_attachments=["정산서.pdf (확인 3/10쪽)"],
    )

    note = answer.evidence_note()

    assert "일부만 읽은 첨부" in note
    assert "확인 3/10쪽" in note
    assert "자동 변환 실패" not in note, "못 읽은 것과 섞으면 할 일이 달라진다"


def test_the_evidence_note_separates_failure_from_partial():
    from tybot.answer import Answer

    answer = Answer(
        "정리했습니다.", ["#현장, 📄doc.md(2026-09-10)"], "m", 0.0, 3, "answered",
        terms=["기성금"],
        withheld=["암호문서.pdf"],
        partial_attachments=["정산서.pdf (확인 3/10쪽)"],
    )

    note = answer.evidence_note()

    assert "자동 변환 실패로 내용을 읽지 못한 첨부: 암호문서.pdf" in note
    assert "일부만 읽은 첨부: 정산서.pdf" in note


def test_a_slide_count_is_read_from_the_pptx_itself():
    """python-pptx 를 부르지 않고 zip 항목만 센다 — 필요한 것은 개수 하나다."""
    pptx = pytest.importorskip("pptx")
    import io as _io

    prs = pptx.Presentation()
    for _ in range(3):
        prs.slides.add_slide(prs.slide_layouts[6])
    buf = _io.BytesIO()
    prs.save(buf)

    from tybot.archive.external_convert import _slide_count

    assert _slide_count(buf.getvalue()) == 3
    assert _slide_count(b"not a zip") is None


def test_the_external_ppt_path_records_slides_without_claiming_it_read_them():
    """**그려진 것과 읽은 것은 다르다.**

    그림만 있는 슬라이드는 PDF 쪽으로는 남지만 kordoc 이 아무것도 못 뽑을 수
    있다. 그걸 「읽었다」 로 세면 모르는 것을 안다고 적는 셈이다.
    """
    pptx = pytest.importorskip("pptx")
    import io as _io

    from tybot.archive import external_convert as ext

    prs = pptx.Presentation()
    for _ in range(4):
        prs.slides.add_slide(prs.slide_layouts[6])
    buf = _io.BytesIO()
    prs.save(buf)

    with cv.collect_coverage() as cov:
        ext._record_render_coverage(buf.getvalue(), _pdf(["a", "b", "c", "d"]), "pptx")

    assert cov.unit == "slide"
    assert cov.total == 4
    assert cov.converted is None, "그려졌다고 읽은 것이 아니다"
    assert cov.state == cv.UNKNOWN


def test_a_slide_lost_during_rendering_is_flagged():
    """원본에 있던 슬라이드가 산출물에 없으면 그 사실이 남아야 한다."""
    pptx = pytest.importorskip("pptx")
    import io as _io

    from tybot.archive import external_convert as ext

    prs = pptx.Presentation()
    for _ in range(4):
        prs.slides.add_slide(prs.slide_layouts[6])
    buf = _io.BytesIO()
    prs.save(buf)

    with cv.collect_coverage() as cov:
        # 렌더 결과가 2쪽뿐이다 — 두 슬라이드가 사라졌다.
        ext._record_render_coverage(buf.getvalue(), _pdf(["a", "b"]), "pptx")

    assert cv.RENDER_LOST_UNITS in cov.flags
    assert cov.state == cv.PARTIAL


def test_render_coverage_is_only_for_slide_formats():
    """docx 를 슬라이드로 세면 그 숫자가 거짓이 된다."""
    from tybot.archive import external_convert as ext

    with cv.collect_coverage() as cov:
        ext._record_render_coverage(b"whatever", _pdf(["a"]), "docx")

    assert cov.unit == ""
    assert cov.total is None


def test_a_screening_code_is_shown_as_words_not_as_a_code(tmp_path):
    """`sensitive-term-mentioned` 가 Slack 답변에 그대로 나갔다(2026-09-22).

    코드는 로그·감사·콘솔의 것이다. 읽는 사람은 그 말이 무슨 뜻인지도, 자기가
    뭘 해야 하는지도 알 수 없다. 모르는 코드는 코드 그대로 보인다 — 조용히
    사라지는 것보다 낫다.
    """
    from tybot.attachment_review import Attachment, status_line

    def _item(*codes: str) -> Attachment:
        return Attachment(
            workspace="pilot", channel_id="C1", file_id="F1", name="주간보고.xlsx",
            filetype="xlsx", mimetype="application/vnd.ms-excel", size=1,
            status="converted", object_path=None, meta_path=tmp_path / "m.json",
            extracted=True, screen_result="passed_with_notice", screen_codes=codes,
        )

    said = status_line(_item("sensitive-term-mentioned"))
    assert "민감 용어 언급" in said
    assert "sensitive-term-mentioned" not in said
    assert "미확인코드" in status_line(_item("미확인코드"))


# --- 쪽별 OCR (2026-10-15 서버 사례) -----------------------------------------
#
# 기본 추출 0/3쪽인데 OCR 본문이 2,543자 나왔고, metadata 는 `partial` ·
# 미확인 [1,2,3] 이었다. 본문은 있는데 **어느 쪽에서 나왔는지 아무도 몰랐다** —
# 문서를 통째로 OCR 했기 때문이다. 그래서 쪽마다 떼어 내 OCR 한다.


class _Kordoc:
    """쪽별 OCR 스텁. **한 쪽짜리 PDF 로 불리는지까지 본다.**

    호출 횟수만 세면 「통째로 한 번」 과 「쪽마다 한 번」 을 구별하지 못한다.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.page_counts: list[int] = []

    def __call__(self, data, suffix, *, force_ocr=False):
        import io as _io

        from pypdf import PdfReader

        assert suffix == "pdf" and force_ocr, "OCR 폴백은 강제 OCR 이어야 한다"
        self.page_counts.append(len(PdfReader(_io.BytesIO(data)).pages))
        result = self.results.pop(0) if self.results else None
        if isinstance(result, Exception):
            raise result
        return [cv.OCR_NOTICE, *(result or [])]


BODY = "스캔 쪽에서 읽은 본문 " + "가" * 60


def test_ocr_body_does_not_make_every_page_succeeded(monkeypatch):
    """**OCR 본문이 있다는 사실은 다 읽었다는 뜻이 아니다.**

    서버에서 난 그 일이다. 1쪽만 읽히고 2·3쪽은 못 읽었는데 본문은 나온다.
    """
    kordoc = _Kordoc(
        [BODY],
        cv.ExternalConversionError("kordoc 결과에 읽을 수 있는 본문이 없습니다"),
        cv.ExternalConversionError("kordoc 결과에 읽을 수 있는 본문이 없습니다"),
    )
    monkeypatch.setattr(cv, "kordoc_lines", kordoc)

    lines, cov = cv.convert_with_coverage("pdf", _pdf(["", "", ""]))

    assert kordoc.page_counts == [1, 1, 1], "쪽마다 떼어 내 OCR 해야 어느 쪽인지 안다"
    assert cov.total == 3
    assert cov.converted == 1, "본문이 나왔다고 세 쪽을 다 읽은 것이 아니다"
    assert cov.missing == (2, 3)
    assert cov.state == cv.PARTIAL
    assert any(BODY in line for line in lines), "읽힌 쪽의 본문은 남는다"
    assert lines.count(cv.OCR_NOTICE) == 1, "OCR 안내가 쪽마다 반복되면 본문이 묻힌다"


def test_every_page_ocred_is_a_full_read(monkeypatch):
    """고친 쪽으로 너무 넓히면 멀쩡히 OCR 된 스캔본이 전부 미확인으로 보인다."""
    kordoc = _Kordoc([BODY], [BODY], [BODY])
    monkeypatch.setattr(cv, "kordoc_lines", kordoc)

    _lines, cov = cv.convert_with_coverage("pdf", _pdf(["", "", ""]))

    assert cov.converted == 3
    assert cov.missing == ()
    assert cov.state == cv.SUCCEEDED


def test_a_mixed_text_and_scan_pdf_only_ocrs_the_scanned_page(monkeypatch):
    """글자가 있는 쪽을 다시 OCR 하면 비용이 쪽수만큼 늘고 본문이 바뀐다."""
    long = (
        "Page body long enough that the extracted text stays well above the two "
        "hundred character floor that triggers the OCR fallback path in convert"
    )
    kordoc = _Kordoc([BODY])
    monkeypatch.setattr(cv, "kordoc_lines", kordoc)

    lines, cov = cv.convert_with_coverage("pdf", _pdf([long, "", long]))

    assert kordoc.page_counts == [1], "텍스트가 있는 쪽은 OCR 대상이 아니다"
    assert cov.converted == 3
    assert cov.state == cv.SUCCEEDED
    assert lines.index("[1쪽]") < lines.index("[2쪽]") < lines.index("[3쪽]")


def test_a_scan_with_no_readable_page_is_refused(monkeypatch):
    """빈 결과만 돌아왔다. **본문이 없으면 변환본도 없다** — 성공으로 닫지 않는다."""
    monkeypatch.setattr(cv, "kordoc_lines", _Kordoc(
        *[cv.ExternalConversionError("읽을 수 있는 본문이 없습니다")] * 2
    ))

    with pytest.raises(cv.ConvertError, match="본문을 얻지 못했다"):
        cv.convert_with_coverage("pdf", _pdf(["", ""]))


def test_an_ocr_free_host_flags_the_pages_it_could_not_check(monkeypatch):
    """OCR 이 없는 서버에서 **읽은 것이 원본의 전부라고 말할 수 없다.**"""
    def unavailable(*_a, **_k):
        raise cv.ExternalConverterUnavailable("kordoc 미설치")

    monkeypatch.setattr(cv, "kordoc_lines", unavailable)
    long = (
        "Page body long enough that the extracted text stays well above the two "
        "hundred character floor that triggers the OCR fallback path in convert"
    )

    _lines, cov = cv.convert_with_coverage("pdf", _pdf([long, "", long]))

    assert cv.OCR_UNAVAILABLE in cov.flags
    assert cov.converted == 2
    assert cov.missing == (2,)
    assert cov.state == cv.PARTIAL


def test_the_ocr_page_cap_is_recorded_not_silent(monkeypatch):
    """상한에 걸린 쪽은 **시도조차 안 한 쪽**이다. 조용히 사라지면 안 된다."""
    monkeypatch.setattr(cv, "MAX_PDF_OCR_PAGES", 1)
    kordoc = _Kordoc([BODY])
    monkeypatch.setattr(cv, "kordoc_lines", kordoc)

    lines, cov = cv.convert_with_coverage("pdf", _pdf(["", "", ""]))

    assert kordoc.page_counts == [1], "상한을 넘겨 부르면 상한이 아니다"
    assert cv.OCR_PAGE_LIMIT_REACHED in cov.flags
    assert cov.converted == 1
    assert cov.missing == (2, 3)
    assert any("OCR 상한" in line for line in lines)


def test_a_text_pdf_never_calls_the_ocr_converter(monkeypatch):
    """회귀: 글자가 멀쩡한 PDF 를 쪽마다 OCR 하면 변환 시간이 쪽수만큼 늘어난다."""
    def boom(*_a, **_k):
        raise AssertionError("텍스트 PDF 에서 OCR 을 부르면 안 된다")

    monkeypatch.setattr(cv, "kordoc_lines", boom)
    long = (
        "Page body long enough that the extracted text stays well above the two "
        "hundred character floor that triggers the OCR fallback path in convert"
    )

    _lines, cov = cv.convert_with_coverage("pdf", _pdf([long, long]))

    assert cov.state == cv.SUCCEEDED
    assert cov.converted == 2


def test_thin_text_is_not_full_coverage_when_required_ocr_fails(monkeypatch):
    kordoc = _Kordoc(cv.ExternalConversionError("fixture failure"), [BODY])
    monkeypatch.setattr(cv, "kordoc_lines", kordoc)
    lines, cov = cv.convert_with_coverage("pdf", _pdf(["Header only", ""]))
    assert "Header only" in "\n".join(lines)
    assert cov.converted == 1
    assert cov.missing == (1,)
    assert cov.state == cv.PARTIAL


def test_thin_text_beyond_ocr_cap_stays_unverified(monkeypatch):
    monkeypatch.setattr(cv, "MAX_PDF_OCR_PAGES", 1)
    monkeypatch.setattr(cv, "kordoc_lines", _Kordoc([BODY]))
    _lines, cov = cv.convert_with_coverage("pdf", _pdf(["Header one", "Header two"]))
    assert cov.converted == 1
    assert cov.missing == (2,)
    assert cv.OCR_PAGE_LIMIT_REACHED in cov.flags
