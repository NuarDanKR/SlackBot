"""PDF 쪽마다 **이미지가 얼마나 차지하나**를 센다 (B-80 혼합 쪽).

## 왜 필요한가

`_pdf_text_layer()` 는 글자가 나오면 그 쪽을 읽은 것으로 센다. 이미지 안의
내용은 **보지도 않는다.**

업무보고 한 쪽이 이렇게 생겼다고 하자 — 위에 문단 두 줄, 아래에 **스캔해 붙인
기성 내역 표 한 장.** 텍스트 레이어에서는 문단 두 줄이 나오고, 그래서 그 쪽은
`converted` 다. 표는 아무 데도 안 남고, 아무도 없어진 줄 모른다.

> 사람이 「기성금 얼마야」 라고 물으면 봇은 **「자료에 없습니다」** 라고 답한다.
> 그 쪽은 「다 읽음」 으로 닫혀 있어 다시 보지도 않는다.

그래서 **글자가 나온 쪽도 이미지를 센다.** 큰 이미지가 있으면 그 쪽은
「읽었다」 가 아니라 **「글자는 읽었고 그림 안은 모른다」** 다.

## 무엇을 세고 무엇을 세지 않나

| 센다 | 세지 않는다 |
|---|---|
| 이미지 XObject 개수 | 그 안에 무엇이 그려져 있나 |
| 이미지가 덮은 넓이 / 쪽 넓이 | 표인지 사진인지 로고인지 |
| 가장 큰 이미지 한 장의 비율 | 픽셀·OCR·모델 호출 |

**무엇이 그려져 있는지는 모른다.** 그걸 알려면 읽어야 하고, 읽는 것은 OCR 이나
Vision 의 일이다. 이 모듈은 **「봐야 할 것이 있나」** 까지만 답한다.

그러니 판정은 한쪽으로만 틀린다 — 로고만 있는 쪽도 넓이가 크면 의심으로 잡힌다.
반대 방향(표를 놓치는 것)보다 **이쪽이 싸다.** 의심은 사람이 지우면 되고,
놓친 표는 아무도 모른다.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

logger = logging.getLogger("tybot.archive.pdf_images")

#: 이미지가 쪽의 이 비율 이상을 덮으면 **그 안을 못 읽었다고 본다.**
#:
#: 머리글 로고는 보통 2~3%다. 스캔해 붙인 표·도면은 쪽의 상당 부분을 차지한다.
#: 10% 는 둘 사이에서 **놓치는 쪽보다 의심하는 쪽**으로 기울여 잡은 값이다.
DEFAULT_IMAGE_AREA_RATIO = 0.10

#: 쪽 넓이를 못 구했을 때 쓸 기본(A4, 72dpi 포인트). 비율의 분모가 없으면
#: **아무 쪽도 의심하지 못한다** — 모르는 것을 「괜찮다」 로 읽는 쪽이 더 나쁘다.
FALLBACK_PAGE_AREA = 595.0 * 842.0


@dataclass(frozen=True)
class PageImages:
    """한 쪽의 이미지 조사. **내용은 담지 않는다.**"""

    page_number: int
    images: int = 0
    #: 이미지가 덮은 넓이 ÷ 쪽 넓이. 겹치면 1을 넘을 수 있어 묶지 않는다.
    area_ratio: float = 0.0
    #: 가장 큰 이미지 한 장의 비율.
    largest_ratio: float = 0.0
    #: 조사하지 못했으면 사유 코드. **빈 값이 「이미지 없음」 이 아니다.**
    error: str = ""

    @property
    def inspected(self) -> bool:
        return not self.error


def _page_area(page) -> float:
    """쪽의 넓이. 못 읽으면 A4 로 둔다 — 분모가 없으면 비율이 없다."""
    try:
        box = page.mediabox
        width = float(box.width)
        height = float(box.height)
    except Exception:  # noqa: BLE001 - 넓이를 못 읽는 것이 실패는 아니다
        return FALLBACK_PAGE_AREA
    area = abs(width * height)
    return area if area > 1.0 else FALLBACK_PAGE_AREA


def _drawn_area(page, xobject_name: str) -> float | None:
    """그 이미지가 **그려진** 넓이. 변환 행렬을 못 읽으면 `None`.

    이미지의 픽셀 크기가 아니라 **쪽 위에 놓인 크기**가 중요하다. 300dpi 로
    스캔한 표는 픽셀로는 거대하지만 쪽에서 차지하는 자리는 쪽 넓이를 넘지 않고,
    반대로 작은 아이콘을 크게 늘려 놓은 쪽도 있다.
    """
    try:
        from pypdf.generic import ContentStream, NameObject
    except ImportError:  # pragma: no cover - pypdf 가 있어야 여기까지 온다
        return None
    try:
        content = ContentStream(page.get_contents(), page.pdf)
    except Exception:  # noqa: BLE001 - 콘텐츠를 못 읽는 쪽이 있다
        return None
    # `cm` 으로 쌓인 현재 변환 행렬을 따라가며 `Do` 가 어느 크기로 그리는지 본다.
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: list[list[float]] = []
    drawn = 0.0
    wanted = NameObject(f"/{xobject_name.lstrip('/')}")
    for operands, operator in content.operations:
        if operator == b"q":
            stack.append(list(ctm))
        elif operator == b"Q":
            ctm = stack.pop() if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif operator == b"cm" and len(operands) >= 6:
            try:
                new = [float(x) for x in operands[:6]]
            except (TypeError, ValueError):
                continue
            ctm = [
                new[0] * ctm[0] + new[1] * ctm[2], new[0] * ctm[1] + new[1] * ctm[3],
                new[2] * ctm[0] + new[3] * ctm[2], new[2] * ctm[1] + new[3] * ctm[3],
                new[4] * ctm[0] + new[5] * ctm[2] + ctm[4],
                new[4] * ctm[1] + new[5] * ctm[3] + ctm[5],
            ]
        elif operator == b"Do" and operands and operands[0] == wanted:
            # 단위 사각형이 현재 행렬로 변환된 넓이 = |a·d − b·c|.
            drawn += abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])
    return drawn


def inspect_page(page, number: int) -> PageImages:
    """쪽 하나. **예외를 밖으로 내지 않는다** — 조사 실패도 결과다."""
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return PageImages(page_number=number)
        entries = xobjects.get_object()
    except Exception as exc:  # noqa: BLE001
        return PageImages(page_number=number, error=type(exc).__name__)

    area = _page_area(page)
    count = 0
    total = 0.0
    largest = 0.0
    error = ""
    for name in list(entries):
        try:
            obj = entries[name].get_object()
            if str(obj.get("/Subtype", "")) != "/Image":
                continue
            count += 1
            drawn = _drawn_area(page, str(name))
            if drawn is None:
                # 픽셀 크기는 실제 배치 넓이가 아니다. 추정치는 남기되 조사
                # 실패로 표시해 이 쪽을 완료로 닫지 않는다.
                error = "image_draw_unknown"
                width = float(obj.get("/Width", 0) or 0)
                height = float(obj.get("/Height", 0) or 0)
                drawn = min(width * height, area) if width and height else 0.0
            ratio = drawn / area if area else 0.0
            total += ratio
            largest = max(largest, ratio)
        except Exception:  # noqa: BLE001 - 한 이미지 실패가 쪽 조사를 막지 않는다
            error = "image_inspection_failed"
            continue
    return PageImages(page_number=number, images=count,
                      area_ratio=round(total, 4), largest_ratio=round(largest, 4), error=error)


def inspect(data: bytes) -> dict[int, PageImages]:
    """PDF 전체. 못 읽으면 **빈 표가 아니라** 쪽마다 사유를 남긴다."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF 이미지 조사 실패 code=%s", type(exc).__name__)
        return {}
    found = {}
    for number, page in enumerate(pages, 1):
        try:
            found[number] = inspect_page(page, number)
        except Exception as exc:  # noqa: BLE001 - 한 쪽의 조사 실패를 변환 실패로 확대하지 않는다
            found[number] = PageImages(page_number=number, error=type(exc).__name__)
    return found


def image_heavy(found: PageImages, *, ratio: float = DEFAULT_IMAGE_AREA_RATIO) -> bool:
    """이 쪽의 이미지 안을 **못 읽었다고 봐야 하나.**

    조사 자체가 실패한 쪽은 `False` 다 — 모르는 것을 의심으로 바꾸면 그 쪽은
    조사 실패인지 그림이 큰 것인지 구별되지 않는다. 조사 실패는 따로 센다.
    """
    if not found.inspected or not found.images:
        return False
    return found.area_ratio >= ratio or found.largest_ratio >= ratio


__all__ = [
    "DEFAULT_IMAGE_AREA_RATIO",
    "FALLBACK_PAGE_AREA",
    "PageImages",
    "image_heavy",
    "inspect",
    "inspect_page",
]
