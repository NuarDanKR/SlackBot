"""B-80 PDF 렌더 smoke test — **실제 pypdfium2** 로 실제 PDF 를 그린다.

pypdfium2 가 없는 환경에서는 건너뛴다(사유를 남긴다). 서버 배포는 `requirements.txt` 에
`pypdfium2==4.30.0` 이 고정돼 있다 — 없으면 B-80 은 렌더링 없이 닫힌다(`RendererUnavailable`).
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

pdfium = pytest.importorskip("pypdfium2", reason="pypdfium2 미설치 — 실제 렌더 smoke 미실행")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

from tybot.archive import vision_pdf_render as render  # noqa: E402


def _scan_like_pdf(pages: int = 3) -> bytes:
    """쪽마다 숫자 표가 그려진 **이미지 PDF**(스캔본과 같은 모양)."""
    images = []
    for number in range(1, pages + 1):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 560, 200), outline="black", width=3)
        draw.text((60, 60), f"PAGE {number} TOTAL 1,200", fill="black")
        images.append(image)
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=72)
    return buffer.getvalue()


def test_only_the_requested_pages_are_rendered_as_png():
    pdf = _scan_like_pdf(3)
    out = render.render_pages(pdf, [2], max_image_bytes=5_000_000)
    assert set(out) == {2}
    png = out[2]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(io.BytesIO(png))
    # 72dpi 600x800 쪽을 배율 2.0 으로 → 1200x1600
    assert image.size == (1200, 1600)
    # 흰 종이만 나온 것이 아니다 — 그린 표·글자가 있다
    assert image.convert("L").getextrema()[0] < 128


def test_out_of_range_and_oversized_pages_are_not_sent():
    pdf = _scan_like_pdf(2)
    assert render.render_pages(pdf, [0, 5], max_image_bytes=5_000_000) == {}
    assert render.render_pages(pdf, [1], max_image_bytes=10) == {}  # 자르지 않고 뺀다


def test_the_renderer_identity_names_the_library_and_scale():
    version = render.renderer_version()
    assert version.startswith("pypdfium2:") and f"scale{render.RENDER_SCALE}" in version


def test_the_renderer_is_pinned_for_deployment():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "pypdfium2==4.30.0" in requirements.splitlines()
