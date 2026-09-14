"""얼마나 읽었나 — `partial` 과 페이지·시트 coverage (B-45 §5·§6).

설계: `docs/design/operational-warning-recovery-and-answer-progress.md`

이 파일이 지키는 것 둘.

1. **「본문이 나왔다」 와 「다 읽었다」 는 다르다.** 10쪽에서 3쪽만 읽혀도
   예전에는 성공이었고, 그 답에 우리 출처가 붙었다.
2. **모르는 개수는 `null`** 이다. 0 이나 100% 로 만들면 모르는 것을 안다고 적는
   셈이고, 그 순간 그 숫자는 근거가 아니다.
"""
from __future__ import annotations

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
    cov = cv.Coverage(unit="page", total=2, converted=1, missing=(2,))
    row = cov.to_json()
    assert set(row) == {
        "coverage_unit", "coverage_total", "coverage_converted",
        "missing_units", "quality_flags", "coverage_state",
    }
    assert all(isinstance(n, int) for n in row["missing_units"])


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
