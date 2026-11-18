"""B-80 PDF 페이지 렌더링 — **요청한 쪽만** PNG 로 그린다.

PDF 전체를 그리지 않는다. 로컬 추출·OCR 이 읽은 쪽은 그릴 이유도, 보낼 이유도 없다.

렌더러는 `pypdfium2` 다. 서버에 없으면 `RendererUnavailable` 로 닫는다 — 다른 방법으로
「대충」 그리지 않는다. 같은 쪽이라도 렌더러·배율이 바뀌면 바이트가 바뀌므로, 그 신원을
`renderer_version` 으로 좌표에 싣는다(승인·예약·결과가 모두 같은 값을 문다).
"""
from __future__ import annotations

import hashlib
import io

#: 배율 2.0 ≈ 144dpi. 표 숫자를 읽을 만큼이고 페이지 상한(바이트) 안에 든다.
RENDER_SCALE = 2.0


class RendererUnavailable(RuntimeError):
    """렌더러가 없다. 이 쪽은 이번에 다루지 않는다."""


def _pdfium():
    try:
        import pypdfium2
    except ImportError as exc:
        raise RendererUnavailable("pypdfium2 미설치 — PDF 페이지를 그릴 수 없습니다") from exc
    return pypdfium2


def renderer_version() -> str:
    pdfium = _pdfium()
    version = getattr(pdfium, "__version__", "") or getattr(pdfium, "V_PYPDFIUM2", "unknown")
    return f"pypdfium2:{version}:scale{RENDER_SCALE}:png"


def render_pages(pdf: bytes, pages, *, max_image_bytes: int) -> dict[int, bytes]:
    """요청한 쪽(1부터)만 PNG 로. 상한을 넘는 쪽은 결과에 넣지 않는다(잘라서 보내지 않는다)."""
    pdfium = _pdfium()
    document = pdfium.PdfDocument(pdf)
    out: dict[int, bytes] = {}
    try:
        total = len(document)
        for number in sorted(set(pages)):
            if not 1 <= number <= total:
                continue
            page = document[number - 1]
            try:
                image = page.render(scale=RENDER_SCALE).to_pil()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=True)
                data = buffer.getvalue()
            finally:
                page.close()
            if len(data) <= max_image_bytes:
                out[number] = data
    finally:
        document.close()
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["RENDER_SCALE", "RendererUnavailable", "render_pages", "renderer_version", "sha256"]
