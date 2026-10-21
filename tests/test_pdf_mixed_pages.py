"""혼합 쪽 — **글자가 조금 나왔다고 그림 안까지 읽은 것이 아니다** (B-80).

## 막으려는 일

업무보고 한 쪽이 이렇게 생겼다 — 위에 문단 두 줄, 아래에 **스캔해 붙인 기성
내역 표 한 장.** 전에는 문단 두 줄이 나왔다는 이유로 그 쪽이 `converted` 였고,
표는 아무 데도 안 남았다.

> 사람이 「기성금 얼마야」 라고 물으면 봇은 **「자료에 없습니다」** 라고 답한다.
> 그 쪽은 「다 읽음」 으로 닫혀 있어 **다시 보지도 않는다.**

그래서 그런 쪽은 `converted` 가 아니라 `unverified` 다 — 「얻은 것이 전부가
아니다」. `missing`(아무것도 못 얻음)과 **다른 갈래**로 둔다. 섞으면 본문이
나온 쪽이 못 읽은 쪽으로 세어져 그 내용이 답변에서 빠진다.

## 합성 자료만 쓴다

실제 사내 PDF 는 fixture 에 넣지 않는다. 글자와 이미지 XObject 를 직접 조립한
최소 PDF 로 돌린다 — 모델도 OCR 도 부르지 않는다.
"""
from __future__ import annotations

import io
import zlib

import pytest

from tybot.archive import convert as cv
from tybot.archive import pdf_page_images as pi

PAGE_W, PAGE_H = 595.0, 842.0


# --- 합성 PDF ------------------------------------------------------------------


def _pdf(pages: list[dict]) -> bytes:
    """글자·이미지를 지정한 쪽들로 최소 PDF 를 만든다.

    `reportlab` 에 기대면 그게 없는 곳에서 **검사가 조용히 skip 된다.**
    건너뛴 검사는 검사가 아니다 — 여기서 막으려는 것이 바로 「읽은 척」 이다.

    각 쪽: `{"text": "...", "image": (너비비율, 높이비율)}`.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_refs: list[tuple[int, int, int | None]] = []
    for spec in pages:
        text = spec.get("text") or ""
        image = spec.get("image")
        stream = b""
        if text:
            stream += f"BT /F1 12 Tf 40 800 Td ({text}) Tj ET\n".encode()
        image_id = None
        if image:
            wide, tall = image
            # 1x1 회색 픽셀을 쪽의 (wide x tall) 만큼 늘려 그린다.
            pixel = zlib.compress(b"\x80")
            image_id = add(
                b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                b"/Length %d >>\nstream\n" % len(pixel) + pixel + b"\nendstream"
            )
            if spec.get("draw_image", True):
                stream += (
                    f"q {PAGE_W * wide:.2f} 0 0 {PAGE_H * tall:.2f} 20 20 cm /Im1 Do Q\n"
                ).encode()
        if not stream:
            stream = b" "
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_refs.append((content, font, image_id))

    pages_id = len(objects) + len(page_refs) + 1
    page_ids = []
    for content, font_id, image_id in page_refs:
        xobject = (
            b"/XObject << /Im1 %d 0 R >> " % image_id if image_id else b""
        )
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> %s>> /Contents %d 0 R >>"
            % (pages_id, int(PAGE_W), int(PAGE_H), font_id, xobject, content)
        ))
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog, xref)
    )
    return out.getvalue()


# 200자 미만이면 `_pdf` 가 **스캔본 의심**으로 보고 쪽별 OCR 경로로 간다.
# 여기서 보려는 것은 그 경로가 아니라 **글자가 멀쩡히 나온 쪽의 그림**이다.
LONG = (
    "Page body long enough that the extracted text stays well above the two "
    "hundred character floor that triggers the OCR fallback path in convert, "
    "repeated here so a single page document also clears that floor on its own."
)
assert len(LONG) > 200, "이 길이가 조건이다"


# --- 이미지 조사 ------------------------------------------------------------------


def test_a_page_without_images_is_not_image_heavy():
    """멀쩡한 텍스트 쪽을 의심으로 만들면 **모든 문서가 미확인**이 된다."""
    got = pi.inspect(_pdf([{"text": LONG}]))

    assert got[1].images == 0
    assert got[1].inspected is True
    assert pi.image_heavy(got[1]) is False


def test_a_small_logo_does_not_make_the_page_suspect():
    """머리글 로고는 보통 쪽의 2~3%다. 그것까지 의심하면 신호가 소음이 된다."""
    got = pi.inspect(_pdf([{"text": LONG, "image": (0.08, 0.03)}]))

    assert got[1].images == 1
    assert got[1].largest_ratio < pi.DEFAULT_IMAGE_AREA_RATIO
    assert pi.image_heavy(got[1]) is False


def test_a_pasted_table_image_is_image_heavy():
    """스캔해 붙인 표. 쪽의 절반을 덮는다."""
    got = pi.inspect(_pdf([{"text": LONG, "image": (0.9, 0.5)}]))

    assert got[1].images == 1
    assert got[1].largest_ratio >= pi.DEFAULT_IMAGE_AREA_RATIO
    assert pi.image_heavy(got[1]) is True


def test_an_unused_image_resource_is_not_a_drawn_image():
    got = pi.inspect(_pdf([{"text": LONG, "image": (0.9, 0.5), "draw_image": False}]))

    assert got[1].images == 1
    assert got[1].area_ratio == 0
    assert got[1].inspected is True
    assert pi.image_heavy(got[1]) is False


def test_many_small_images_add_up():
    """작은 그림 여러 장이 쪽을 덮는 경우도 있다. **합도 본다.**"""
    found = pi.PageImages(page_number=1, images=6, area_ratio=0.42, largest_ratio=0.07)

    assert pi.image_heavy(found) is True


def test_a_failed_inspection_is_not_called_suspect():
    """조사 실패와 「그림이 크다」 는 다르다. 섞으면 구별이 안 된다."""
    broken = pi.PageImages(page_number=1, error="KeyError")

    assert broken.inspected is False
    assert pi.image_heavy(broken) is False


def test_missing_image_census_marks_coverage_partial(monkeypatch):
    monkeypatch.setattr(pi, "inspect", lambda _data: {})

    _lines, cov = cv.convert_with_coverage("pdf", _pdf([{"text": LONG}]))

    assert cov.converted == 1
    assert cv.IMAGE_SCAN_FAILED in cov.flags
    assert cov.state == cv.PARTIAL


def test_the_inspection_carries_no_image_content():
    """무엇이 그려져 있는지는 **모른다.** 결과에 픽셀이 섞이면 안 된다."""
    from dataclasses import asdict

    got = pi.inspect(_pdf([{"text": LONG, "image": (0.9, 0.5)}]))
    row = asdict(got[1])

    assert set(row) == {"page_number", "images", "area_ratio", "largest_ratio", "error"}
    assert all(isinstance(v, int | float | str) for v in row.values())


def test_a_broken_pdf_does_not_raise():
    """조사가 터지면 변환 전체가 멈춘다. 빈 표를 돌려준다."""
    assert pi.inspect(b"not a pdf") == {}


def test_a_page_inspection_exception_is_reported(monkeypatch):
    def fail(_page, _number):
        raise ValueError("bad page")

    monkeypatch.setattr(pi, "inspect_page", fail)
    got = pi.inspect(_pdf([{"text": LONG}]))

    assert got[1].inspected is False
    assert got[1].error == "ValueError"


# --- coverage 반영 ----------------------------------------------------------------


def test_a_mixed_page_is_unverified_not_converted():
    """**이것이 B-80 의 그 일이다.** 문단은 읽혔고 표는 안 읽혔다."""
    data = _pdf([{"text": LONG}, {"text": LONG, "image": (0.9, 0.5)}])

    lines, cov = cv.convert_with_coverage("pdf", data)

    assert cov.total == 2
    assert cov.converted == 1, "그림 쪽을 읽은 쪽으로 세면 안 된다"
    assert cov.unverified == (2,)
    assert cov.missing == (), "본문이 나온 쪽을 못 읽은 쪽으로 세면 그 내용이 빠진다"
    assert cv.IMAGE_CONTENT_UNREAD in cov.flags
    assert cov.state == cv.PARTIAL
    # 그 쪽의 글자는 **버리지 않는다.**
    assert "[2쪽]" in lines


def test_a_clean_text_pdf_stays_succeeded():
    """고친 쪽으로 너무 넓히면 멀쩡한 문서가 전부 미확인으로 보인다."""
    data = _pdf([{"text": LONG}, {"text": LONG}])

    _lines, cov = cv.convert_with_coverage("pdf", data)

    assert cov.state == cv.SUCCEEDED
    assert cov.unverified == ()
    assert cv.IMAGE_CONTENT_UNREAD not in cov.flags


def test_a_logo_on_every_page_does_not_turn_the_document_partial():
    data = _pdf([{"text": LONG, "image": (0.08, 0.03)}] * 3)

    _lines, cov = cv.convert_with_coverage("pdf", data)

    assert cov.state == cv.SUCCEEDED
    assert cov.converted == 3


def test_the_summary_tells_the_two_apart():
    """「미확인」 과 「그림 미해독」 은 사람이 할 일이 다르다."""
    cov = cv.Coverage(unit="page", total=5, converted=2, missing=(4,), unverified=(3,))

    said = cov.summary()

    assert "확인 2/5쪽" in said
    assert "미확인 4쪽" in said
    assert "그림 미해독 3쪽" in said


def test_the_notice_names_the_pages_to_look_at():
    """쪽 번호가 없으면 **어느 쪽을 열어 봐야 하는지** 모른다."""
    data = _pdf([{"text": LONG, "image": (0.9, 0.5)}, {"text": LONG}])

    lines, _cov = cv.convert_with_coverage("pdf", data)

    notice = [ln for ln in lines if ln.startswith("[변환 안내] 그림 안을")]
    assert notice and "1" in notice[0]


def test_the_notice_carries_no_document_text():
    """안내 줄이 운영 로그·목록으로 나간다. 좌표만 담는다(원칙 5)."""
    data = _pdf([{"text": f"기성금 3억 2천만원 {LONG}", "image": (0.9, 0.5)}])

    lines, cov = cv.convert_with_coverage("pdf", data)

    notice = next(ln for ln in lines if ln.startswith("[변환 안내] 그림 안을"))
    assert "기성금" not in notice
    assert "기성금" not in str(cov.to_json())


# --- 승인·예산 없이 외부 호출을 시작하지 않는다 ---------------------------------------


def test_marking_a_page_unverified_calls_no_model():
    """**coverage 는 판정이지 전송이 아니다.**

    여기서 Vision 을 부르면 승인·예산·provider 검사를 전부 건너뛴 전송이 된다.
    쪽을 후보로 **표시만** 하고, 보내는 일은 승인을 쥔 worker 가 한다.
    """
    import tybot.archive.vision_extract as vx

    def boom(*_a, **_k):  # pragma: no cover - 불리면 안 된다
        raise AssertionError("coverage 판정이 Vision 을 불렀다")

    original = vx.extract_page
    vx.extract_page = boom
    try:
        _lines, cov = cv.convert_with_coverage(
            "pdf", _pdf([{"text": LONG, "image": (0.9, 0.5)}]),
        )
    finally:
        vx.extract_page = original

    assert cov.unverified == (1,)


def test_the_converter_never_imports_the_vision_worker():
    """변환기가 승인 경로를 들고 있으면 **언젠가 거기서 부른다.**"""
    from pathlib import Path

    source = Path("src/tybot/archive/convert.py").read_text(encoding="utf-8")

    for forbidden in ("vision_extract", "vision_policy", "vision_store", "extract_page"):
        assert forbidden not in source


def test_the_image_census_makes_no_network_or_model_call():
    from pathlib import Path

    source = Path("src/tybot/archive/pdf_page_images.py").read_text(encoding="utf-8")

    for forbidden in ("requests", "urllib", "socket", "anthropic", "router",
                      "vision_", "http"):
        assert forbidden not in source


# --- 실패 기록을 덮지 않는다 ---------------------------------------------------------


def test_an_unverified_page_never_becomes_succeeded():
    """부분 결과를 완료로 올리면 그 쪽은 다시 안 본다."""
    for cov in (
        cv.Coverage(unit="page", total=1, converted=1, unverified=(1,)),
        cv.Coverage(unit="page", total=2, converted=2, unverified=(2,)),
    ):
        assert cov.state == cv.PARTIAL


def test_a_failed_image_scan_is_flagged_not_assumed_clean():
    """조사를 못 했으면 **「그림이 없다」 고 말할 수 없다.**"""
    cov = cv.Coverage(unit="page", total=2, converted=2)
    cov.note(cv.IMAGE_SCAN_FAILED)

    assert cov.state == cv.PARTIAL


@pytest.mark.parametrize("ratio", [0.0, 0.05, 0.09])
def test_below_the_threshold_stays_converted(ratio):
    found = pi.PageImages(page_number=1, images=1, area_ratio=ratio, largest_ratio=ratio)
    assert pi.image_heavy(found) is False


@pytest.mark.parametrize("ratio", [0.10, 0.5, 1.0])
def test_at_or_above_the_threshold_is_suspect(ratio):
    found = pi.PageImages(page_number=1, images=1, area_ratio=ratio, largest_ratio=ratio)
    assert pi.image_heavy(found) is True
