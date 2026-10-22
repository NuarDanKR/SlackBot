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
    """`/XObject` 에 있다는 것은 **쓸 수 있다**는 뜻이지 그렸다는 뜻이 아니다.

    템플릿에서 물려받은 미사용 이미지가 넓이로 잡히면, 아무것도 안 그린 쪽이
    의심으로 올라간다. 이제 `Do` 로 실제로 그린 것만 센다 — 그래서 `images` 도
    **0** 이다(2026-10-21 이전에는 리소스 개수였다).
    """
    got = pi.inspect(_pdf([{"text": LONG, "image": (0.9, 0.5), "draw_image": False}]))

    assert got[1].images == 0
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

    assert set(row) == {
        "page_number", "images", "area_ratio", "largest_ratio", "max_depth", "error",
    }
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


# --- Form XObject 안에 중첩된 이미지 (2026-10-21) --------------------------------
#
# 한글·워드가 내보낸 PDF 는 표·도장을 Form 으로 한 겹 싸는 일이 흔하다. 쪽
# 콘텐츠에는 `/Fm0 Do` 만 있고 이미지는 그 안에 있다. 리소스 목록만 훑던 때는
# 그런 쪽이 **이미지가 없는 쪽**으로 보였고, 스캔해 붙인 표가 그대로 묻혔다.


def _nested_pdf(page_ops: str, *, page_xobjects: list[str],
                forms: dict[str, dict], images: list[str]) -> bytes:
    """Form 중첩을 직접 조립한다. **객체 번호를 먼저 잡고** 본문을 쓴다.

    Form 끼리 서로를 가리킬 수 있어야 하므로(순환 시험) 번호를 미리 잡지 않으면
    앞선 Form 이 뒤의 번호를 적을 수 없다.
    """
    numbers: dict[str, int] = {}
    next_id = 1
    font_id, next_id = next_id, next_id + 1
    for name in images:
        numbers[name], next_id = next_id, next_id + 1
    for name in forms:
        numbers[name], next_id = next_id, next_id + 1
    content_id, next_id = next_id, next_id + 1
    page_id, next_id = next_id, next_id + 1
    pages_id, next_id = next_id, next_id + 1
    catalog_id, next_id = next_id, next_id + 1

    bodies: dict[int, bytes] = {}
    bodies[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    pixel = zlib.compress(b"\x80")
    for name in images:
        bodies[numbers[name]] = (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            b"/Length %d >>\nstream\n" % len(pixel) + pixel + b"\nendstream"
        )

    def xobject_dict(names) -> bytes:
        if not names:
            return b""
        inner = b" ".join(b"/%s %d 0 R" % (n.encode(), numbers[n]) for n in names)
        return b"/XObject << %s >> " % inner

    for name, spec in forms.items():
        ops = spec["content"].encode()
        bbox = spec.get("bbox", [0, 0, PAGE_W, PAGE_H])
        # `matrix_raw` 는 배열을 **그대로** 적는다. 숫자가 아닌 `/Matrix` 를
        # 만드는 데 쓴다 — 실제 PDF 에도 그런 것이 있다.
        matrix_raw = spec.get("matrix_raw")
        if matrix_raw is None:
            matrix = spec.get("matrix", [1, 0, 0, 1, 0, 0])
            matrix_raw = b" ".join(b"%.6f" % float(v) for v in matrix)
        # `broken_filter` 는 **모르는 필터**를 적는다. `get_data()` 가 터진다 —
        # 못 여는 Form 을 만드는 가장 작은 방법이다.
        #
        # 깨진 `/FlateDecode` 는 쓰지 않는다. pypdf 가 **조용히 복구해** 빈 데이터를
        # 돌려주고, 그러면 이 시험이 아무것도 안 지킨다(실제로 그랬다).
        filter_entry = b"/Filter /TyBotBrokenDecode " if spec.get("broken_filter") else b""
        bodies[numbers[name]] = (
            b"<< /Type /XObject /Subtype /Form "
            b"/BBox [%s] /Matrix [%s] %s/Resources << %s>> /Length %d >>\nstream\n"
            % (
                b" ".join(b"%.4f" % float(v) for v in bbox),
                matrix_raw,
                filter_entry,
                xobject_dict(spec.get("xobjects") or []),
                len(ops),
            )
            + ops + b"\nendstream"
        )

    ops = page_ops.encode() or b" "
    bodies[content_id] = b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"\nendstream"
    bodies[page_id] = (
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
        b"/Resources << /Font << /F1 %d 0 R >> %s>> /Contents %d 0 R >>"
        % (pages_id, int(PAGE_W), int(PAGE_H), font_id,
           xobject_dict(page_xobjects), content_id)
    )
    bodies[pages_id] = b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page_id
    bodies[catalog_id] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i in range(1, next_id):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + bodies[i] + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % next_id)
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (next_id, catalog_id, xref)
    )
    return out.getvalue()


TEXT_OP = f"BT /F1 12 Tf 40 800 Td ({LONG}) Tj ET\n"


def _draw(name: str, wide: float, tall: float, x: float = 20, y: float = 20) -> str:
    return f"q {PAGE_W * wide:.2f} 0 0 {PAGE_H * tall:.2f} {x} {y} cm /{name} Do Q\n"


def test_an_image_inside_a_form_is_found():
    """**이번에 고친 그 자리다.** 쪽 콘텐츠에는 `/Fm1 Do` 만 있다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.9, 0.5),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1]}},
        images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].images == 1, "Form 안의 이미지를 못 봤다"
    assert got[1].max_depth == 1
    assert got[1].largest_ratio >= pi.DEFAULT_IMAGE_AREA_RATIO
    assert pi.image_heavy(got[1]) is True


def test_the_form_matrix_scales_the_nested_image():
    """Form 의 `/Matrix` 를 안 곱하면 넓이가 틀린다.

    같은 이미지를 같은 `cm` 으로 그려도 Form 이 1/10 로 줄여 놓았으면 실제로
    덮는 자리는 1/100 이다.
    """
    def ratio(scale: float) -> float:
        got = pi.inspect(_nested_pdf(
            TEXT_OP + _draw("Fm1", 1.0, 1.0, 0, 0),
            page_xobjects=["Fm1"],
            forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                           "bbox": [0, 0, 1, 1],
                           "matrix": [scale, 0, 0, scale, 0, 0]}},
            images=["Im1"],
        ))
        return got[1].largest_ratio

    full = ratio(1.0)
    tenth = ratio(0.1)

    assert full >= pi.DEFAULT_IMAGE_AREA_RATIO
    assert tenth < full
    assert tenth == pytest.approx(full * 0.01, rel=0.05)


def test_a_logo_inside_a_form_still_does_not_make_the_page_suspect():
    """중첩을 보게 했다고 작은 그림까지 의심으로 만들면 신호가 소음이 된다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.08, 0.03, 10, 10),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1]}},
        images=["Im1"],
    ))

    assert got[1].images == 1
    assert pi.image_heavy(got[1]) is False


def test_images_two_forms_deep_are_found():
    """Form 안의 Form. 한 겹만 보면 또 놓친다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.9, 0.5),
        page_xobjects=["Fm1"],
        forms={
            "Fm1": {"content": "/Fm2 Do\n", "xobjects": ["Fm2"], "bbox": [0, 0, 1, 1]},
            "Fm2": {"content": "/Im1 Do\n", "xobjects": ["Im1"], "bbox": [0, 0, 1, 1]},
        },
        images=["Im1"],
    ))

    assert got[1].images == 1
    assert got[1].max_depth == 2
    assert pi.image_heavy(got[1]) is True


def test_an_unused_resource_inside_a_form_is_not_counted():
    """Form 안에서도 **그린 것만** 센다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.9, 0.5),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1", "Im2"],
                       "bbox": [0, 0, 1, 1]}},
        images=["Im1", "Im2"],
    ))

    assert got[1].images == 1


def test_the_same_image_drawn_twice_counts_once_but_adds_area():
    """장수와 넓이는 다른 질문이다."""
    half = _draw("Im1", 0.4, 0.4, 0, 0)
    got = pi.inspect(_nested_pdf(
        TEXT_OP + half + half, page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].images == 1
    assert got[1].area_ratio == pytest.approx(got[1].largest_ratio * 2, rel=0.01)


def test_a_self_referencing_form_does_not_hang():
    """Form 이 자기를 다시 부른다. 들어가면 안 돌아온다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.5, 0.5, 0, 0),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n/Fm1 Do\n",
                       "xobjects": ["Im1", "Fm1"], "bbox": [0, 0, 1, 1]}},
        images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].images == 1
    # **깊이 상한이 아니라 순환 방어가 멈춘 것이다.** 상한에 기대면 그 전까지
    # 같은 가지를 12번 판다 — 그 비용이 큰 PDF 에서 그대로 드러난다.
    assert got[1].max_depth == 1


def test_a_mutual_form_cycle_does_not_hang():
    """Fm1 → Fm2 → Fm1. 자기 참조만 막으면 이쪽으로 돈다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 0.5, 0.5, 0, 0),
        page_xobjects=["Fm1"],
        forms={
            "Fm1": {"content": "/Fm2 Do\n", "xobjects": ["Fm2"], "bbox": [0, 0, 1, 1]},
            "Fm2": {"content": "/Im1 Do\n/Fm1 Do\n",
                    "xobjects": ["Im1", "Fm1"], "bbox": [0, 0, 1, 1]},
        },
        images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].images == 1
    # Fm1 → Fm2 에서 멈춘다. 자기 참조만 막으면 여기서 12겹까지 돈다.
    assert got[1].max_depth == 2


def test_the_depth_limit_stops_the_descent(monkeypatch):
    """수만 겹으로 쌓인 PDF 가 변환 하나를 붙들면 안 된다."""
    monkeypatch.setattr(pi, "MAX_FORM_DEPTH", 2)
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", 1.0, 1.0, 0, 0),
        page_xobjects=["Fm1"],
        forms={
            "Fm1": {"content": "/Fm2 Do\n", "xobjects": ["Fm2"], "bbox": [0, 0, 1, 1]},
            "Fm2": {"content": "/Fm3 Do\n", "xobjects": ["Fm3"], "bbox": [0, 0, 1, 1]},
            "Fm3": {"content": "/Im1 Do\n", "xobjects": ["Im1"], "bbox": [0, 0, 1, 1]},
        },
        images=["Im1"],
    ))

    # 깊이 상한에 걸려 Im1 을 못 봤다. **「이미지 없음」 으로 적지 않는다.**
    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_BUDGET


def test_an_exhausted_budget_that_already_found_a_big_image_is_kept(monkeypatch):
    """더 봐도 넓이는 줄지 않는다. 찾은 것은 그대로 쓴다."""
    monkeypatch.setattr(pi, "MAX_OPERATIONS", 5)
    got = pi.inspect(_nested_pdf(
        _draw("Im1", 0.9, 0.9, 0, 0) + TEXT_OP * 50,
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is True
    assert pi.image_heavy(got[1]) is True


def test_an_exhausted_budget_without_enough_area_is_a_failure(monkeypatch):
    """끝까지 못 봤고 찾은 것은 기준에 못 미친다. **모르는 것이다.**"""
    monkeypatch.setattr(pi, "MAX_OPERATIONS", 5)
    got = pi.inspect(_nested_pdf(
        TEXT_OP * 50 + _draw("Im1", 0.9, 0.9, 0, 0),
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_BUDGET
    assert pi.image_heavy(got[1]) is False


def test_the_form_bbox_caps_an_overstated_area():
    """Form 은 BBox 밖으로 못 나간다. 그만큼 과대평가를 깎는다.

    안쪽에서 쪽 전체를 그리는 `cm` 을 써도, Form 이 쪽의 1/4 짜리 BBox 라면
    실제로 덮는 자리는 그 안이다.
    """
    got = pi.inspect(_nested_pdf(
        TEXT_OP + "q 1 0 0 1 0 0 cm /Fm1 Do Q\n",
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": _draw("Im1", 1.0, 1.0, 0, 0),
                       "xobjects": ["Im1"],
                       "bbox": [0, 0, PAGE_W * 0.5, PAGE_H * 0.5]}},
        images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].area_ratio <= 0.26


def test_a_missing_xobject_only_loses_that_branch():
    """없는 이름을 그리는 PDF 가 있다. 쪽 전체 조사를 막으면 안 된다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Im1", 0.9, 0.5, 0, 0) + "q 1 0 0 1 0 0 cm /Missing Do Q\n",
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].images == 1


def test_the_nested_inspection_carries_no_image_content():
    """중첩을 따라가도 담는 것은 **숫자뿐**이다(원칙 5)."""
    from dataclasses import asdict

    got = pi.inspect(_nested_pdf(
        f"BT /F1 12 Tf 40 800 Td (기성금 3억 2천만원 {LONG}) Tj ET\n"
        + _draw("Fm1", 0.9, 0.5),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1]}},
        images=["Im1"],
    ))
    row = asdict(got[1])

    assert "기성금" not in str(row)
    assert all(isinstance(v, int | float | str) for v in row.values())


def test_the_traversal_makes_no_network_or_model_call():
    from pathlib import Path

    source = Path("src/tybot/archive/pdf_page_images.py").read_text(encoding="utf-8")

    for forbidden in ("requests", "urllib", "socket", "anthropic", "router",
                      "vision_", "http", "subprocess"):
        assert forbidden not in source


# --- 못 읽은 것을 0건으로 적지 않는다 (2026-10-22) --------------------------------
#
# 전에는 `/XObject` 조회 실패·Form 스트림 파싱 실패·이상한 행렬을 전부 `return 0.0`
# 으로 삼켰다. 그 쪽은 **「그림이 없는 쪽」** 으로 닫히고, 닫힌 쪽은 아무도 다시
# 보지 않는다. 지금은 전부 **조사 실패**로 남는다.

SMALL = 0.02  # 기준(10%) 아래로 두어야 실패가 결과를 바꾸는지 보인다


def test_a_missing_xobject_name_is_an_inspection_failure():
    """`Do` 가 있다는 것은 **무언가를 그렸다**는 뜻이다.

    그것이 무엇인지 모르면 그 쪽은 「그림이 없는 쪽」 이 아니다.
    """
    got = pi.inspect(_nested_pdf(
        TEXT_OP + "q 100 0 0 100 0 0 cm /Missing Do Q\n",
        page_xobjects=[], forms={}, images=[],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_XOBJECT
    assert pi.image_heavy(got[1]) is False


def test_a_page_with_no_draw_at_all_is_still_a_clean_zero():
    """고친 쪽으로 너무 넓히면 **아무것도 안 그린 쪽**까지 실패가 된다."""
    got = pi.inspect(_nested_pdf(TEXT_OP, page_xobjects=[], forms={}, images=[]))

    assert got[1].inspected is True
    assert got[1].images == 0
    assert got[1].error == ""


def test_a_missing_name_beside_a_big_image_keeps_the_verdict():
    """못 본 부분은 넓이를 **늘리기만** 한다. 이미 의심이면 결론이 안 바뀐다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Im1", 0.9, 0.5, 0, 0) + "q 1 0 0 1 0 0 cm /Missing Do Q\n",
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is True
    assert pi.image_heavy(got[1]) is True


def test_an_unreadable_form_stream_is_an_inspection_failure():
    """압축했다고 적고 압축 안 한 바이트를 둔 Form. `get_data()` 가 터진다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", SMALL, SMALL, 0, 0),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1], "broken_filter": True}},
        images=["Im1"],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_FORM_STREAM


def test_an_unreadable_form_matrix_is_an_inspection_failure():
    """`/Matrix` 를 항등으로 가정하면 이 Form 의 넓이가 통째로 틀린다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", SMALL, SMALL, 0, 0),
        page_xobjects=["Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n", "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1],
                       "matrix_raw": b"(x) 0 0 1 0 0"}},
        images=["Im1"],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_MATRIX


def test_a_non_numeric_cm_operand_is_an_inspection_failure():
    """쪽 콘텐츠의 `cm` 도 같다. 여기부터 좌표를 믿을 수 없다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + "q (x) 0 0 1 0 0 cm /Im1 Do Q\n",
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_MATRIX


def test_a_non_finite_matrix_value_is_not_treated_as_zero():
    """`nan` 은 모든 비교가 거짓이라 **조용히 0 처럼 행동한다.**

    그대로 두면 그림이 큰 쪽이 그림 없는 쪽으로 닫힌다.
    """
    assert pi._numbers([float("nan"), 0, 0, 1, 0, 0], 6) is None
    assert pi._numbers([float("inf"), 0, 0, 1, 0, 0], 6) is None
    assert pi._numbers([1, 0, 0, 1, 0, 0], 6) == [1, 0, 0, 1, 0, 0]


def test_every_failure_code_is_distinct():
    """코드가 겹치면 원인 둘이 한 줄로 합쳐진다."""
    codes = {pi.ERROR_BUDGET, pi.ERROR_XOBJECT, pi.ERROR_FORM_STREAM,
             pi.ERROR_MATRIX, pi.ERROR_TOO_LARGE}

    assert len(codes) == 5
    assert all(isinstance(c, str) and c for c in codes)


# --- 사전 파싱 비용은 연산 상한이 못 막는다 ------------------------------------------
#
# `ContentStream.operations` 는 **스트림 전체를 한 번에 토큰화한 뒤** 돌려준다
# (`_parse_content_stream`). 우리 `MAX_OPERATIONS` 는 그 리스트를 **걸어갈 때**
# 줄어들므로, 토큰화가 끝나기 전에는 한 번도 안 불린다.
#
# pypdf 의 디코드 상한이 75MB 라, 그 크기의 스트림 하나가 토큰화되는 동안
# 변환 하나가 서버를 붙든다. 그래서 **크기를 먼저 본다.**


def test_an_oversized_stream_is_not_parsed_at_all(monkeypatch):
    """상한을 넘으면 `ContentStream` 을 **만들지도 않는다.**

    연산 상한만 믿으면 이 자리에서 이미 토큰화가 끝나 있다.
    """
    import pypdf.generic as generic

    built: list[int] = []
    real = generic.ContentStream

    class _Counting(real):
        def __init__(self, stream, pdf, forced_encoding=None):
            built.append(1)
            super().__init__(stream, pdf, forced_encoding)

    monkeypatch.setattr(generic, "ContentStream", _Counting)
    monkeypatch.setattr(pi, "MAX_CONTENT_BYTES", 8)

    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Im1", 0.9, 0.5, 0, 0),
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert built == [], "상한을 넘겼는데 토큰화했다"
    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_TOO_LARGE
    assert pi.image_heavy(got[1]) is False


def test_a_normal_stream_is_still_parsed(monkeypatch):
    """크기 상한을 넣었다고 멀쩡한 쪽까지 미검사로 만들면 안 된다."""
    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Im1", 0.9, 0.5, 0, 0),
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))

    assert got[1].inspected is True
    assert got[1].images == 1


def test_the_page_content_budget_adds_up_across_forms(monkeypatch):
    """Form 하나하나는 작아도 **합이 커진다.**"""
    monkeypatch.setattr(pi, "MAX_PAGE_CONTENT_BYTES", 40)

    got = pi.inspect(_nested_pdf(
        TEXT_OP + _draw("Fm1", SMALL, SMALL, 0, 0) + _draw("Fm2", SMALL, SMALL, 0, 0),
        page_xobjects=["Fm1", "Fm2"],
        forms={
            "Fm1": {"content": "/Im1 Do\n" * 10, "xobjects": ["Im1"],
                    "bbox": [0, 0, 1, 1]},
            "Fm2": {"content": "/Im1 Do\n" * 10, "xobjects": ["Im1"],
                    "bbox": [0, 0, 1, 1]},
        },
        images=["Im1"],
    ))

    assert got[1].inspected is False
    assert got[1].error == pi.ERROR_TOO_LARGE


def test_an_oversized_stream_beside_a_big_image_keeps_the_verdict(monkeypatch):
    """이미 의심이면 못 본 부분이 결론을 바꾸지 않는다."""
    monkeypatch.setattr(pi, "MAX_CONTENT_BYTES", 400)

    got = pi.inspect(_nested_pdf(
        _draw("Im1", 0.9, 0.9, 0, 0) + _draw("Fm1", SMALL, SMALL, 0, 0),
        page_xobjects=["Im1", "Fm1"],
        forms={"Fm1": {"content": "/Im1 Do\n" * 200, "xobjects": ["Im1"],
                       "bbox": [0, 0, 1, 1]}},
        images=["Im1"],
    ))

    assert got[1].inspected is True
    assert pi.image_heavy(got[1]) is True


def test_the_size_cap_is_read_at_call_time():
    """상수를 모듈에서 읽어야 운영에서 조정하고 시험에서 바꿀 수 있다."""
    assert pi.MAX_CONTENT_BYTES > 0
    assert pi.MAX_PAGE_CONTENT_BYTES >= pi.MAX_CONTENT_BYTES


def test_failure_results_carry_no_document_text():
    """실패 경로의 결과도 **숫자와 코드뿐**이다(원칙 5)."""
    from dataclasses import asdict

    got = pi.inspect(_nested_pdf(
        f"BT /F1 12 Tf 40 800 Td (기성금 3억 2천만원 {LONG}) Tj ET\n"
        + "q (x) 0 0 1 0 0 cm /Im1 Do Q\n",
        page_xobjects=["Im1"], forms={}, images=["Im1"],
    ))
    row = asdict(got[1])

    assert got[1].error == pi.ERROR_MATRIX
    assert "기성금" not in str(row)
    assert all(isinstance(v, int | float | str) for v in row.values())
