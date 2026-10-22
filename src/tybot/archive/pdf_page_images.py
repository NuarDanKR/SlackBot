"""PDF 쪽마다 **이미지가 실제로 그려진 넓이**를 센다 (B-80 혼합 쪽).

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

## 리소스 목록이 아니라 **그리기 연산**을 따라간다

전에는 쪽의 `/XObject` 목록을 훑었다. 두 가지가 틀렸다.

1. **안 그려지는 리소스를 셌다.** `/XObject` 에 있다는 것은 쓸 수 있다는 뜻이지
   이 쪽에 그렸다는 뜻이 아니다. 템플릿에서 물려받은 미사용 이미지가 그대로
   넓이로 잡혔다.
2. **Form XObject 안의 이미지를 못 봤다.** 한글·워드가 내보낸 PDF 는 표·도장을
   Form 으로 한 겹 싸는 일이 흔하다. 쪽 콘텐츠에는 `/Fm0 Do` 만 있고 이미지는
   그 안에 있어서, 쪽만 보면 **이미지가 없는 쪽**으로 보였다.

지금은 콘텐츠 스트림을 **한 번 걸어 내려가며** `Do` 를 만날 때만 센다. Form 이면
그 안으로 들어가고, 들어갈 때 Form 의 `/Matrix` 를 현재 행렬에 곱한다.

## 무엇을 세고 무엇을 세지 않나

| 센다 | 세지 않는다 |
|---|---|
| **그려진** 이미지의 넓이 ÷ 쪽 넓이 | 그 안에 무엇이 그려져 있나 |
| Form 안에 중첩된 이미지 | 표인지 사진인지 로고인지 |
| 서로 다른 이미지 몇 장이 그려졌나 | 안 그려진 리소스 |

**무엇이 그려져 있는지는 모른다.** 그걸 알려면 읽어야 하고, 읽는 것은 OCR 이나
Vision 의 일이다. 이 모듈은 **「봐야 할 것이 있나」** 까지만 답한다.

판정은 한쪽으로만 틀린다 — 로고만 있는 쪽도 넓이가 크면 의심으로 잡힌다.
반대 방향(표를 놓치는 것)보다 **이쪽이 싸다.** 의심은 사람이 지우면 되고,
놓친 표는 아무도 모른다.

## 안전하게 멈춘다

사용자가 올린 PDF 다. Form 이 자기를 다시 부르거나 수만 겹으로 쌓여 있을 수
있고, 그러면 **변환 하나가 서버를 붙든다.** 세 가지로 묶는다.

| 묶는 것 | 어떻게 |
|---|---|
| 순환 참조 | 지금 내려온 경로의 Form 객체 번호를 들고 다닌다. 다시 나오면 안 들어간다 |
| 깊이 | `MAX_FORM_DEPTH` 를 넘으면 그 가지를 더 안 판다 |
| 전체 분량 | 연산 수가 `MAX_OPERATIONS` 를 넘으면 멈춘다 |

**멈춘 것을 「다 봤다」 로 적지 않는다.** 상한에 걸렸는데 그때까지 찾은 넓이가
기준에 못 미치면 **조사 실패**다 — 더 봤으면 넘었을 수도 있기 때문이다. 이미
기준을 넘겼으면 더 봐도 줄지 않으므로 그대로 쓴다.
"""
from __future__ import annotations

import io
import logging
import math
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

#: Form XObject 를 몇 겹까지 따라 들어가나.
MAX_FORM_DEPTH = 12

#: 한 쪽에서 볼 연산 수. 넘으면 멈춘다 — 사용자가 올린 PDF 다.
MAX_OPERATIONS = 200_000

#: 콘텐츠 스트림 하나에서 읽을 **디코드된** 바이트 상한.
#:
#: `ContentStream.operations` 는 **스트림 전체를 한 번에 토큰화한 뒤** 돌려준다
#: (`_parse_content_stream`). 그래서 `MAX_OPERATIONS` 는 **그 뒤에만** 듣는다 —
#: 사전 파싱 비용은 하나도 못 막는다. pypdf 의 디코드 상한이 75MB 라, 75MB 짜리
#: 스트림 하나가 토큰화되는 동안 **변환 하나가 서버를 붙든다.**
#:
#: 그래서 토큰화 **전에** 크기를 본다. 넘으면 파싱하지 않고 미검사로 둔다.
MAX_CONTENT_BYTES = 8 * 1024 * 1024

#: 한 쪽에서 읽을 콘텐츠 바이트 합. Form 이 여러 겹이면 하나하나는 작아도
#: 합이 커진다.
MAX_PAGE_CONTENT_BYTES = 32 * 1024 * 1024

#: 조사 실패 코드. **빈 결과와 구별된다.**
#:
#: 전부 「0건」 이 아니라 **「모른다」** 다. 0건으로 적으면 그 쪽은 그림이 없는
#: 쪽으로 닫히고, 닫힌 쪽은 아무도 다시 안 본다.
ERROR_BUDGET = "traversal_budget"
#: `/XObject` 를 못 열었거나 `Do` 가 가리키는 이름이 없다.
ERROR_XOBJECT = "xobject_unreadable"
#: Form 의 콘텐츠 스트림을 못 열었다.
ERROR_FORM_STREAM = "form_stream_unreadable"
#: `cm`·`/Matrix` 에 숫자가 아닌 값이나 `inf`·`nan` 이 들어 있다.
ERROR_MATRIX = "matrix_unreadable"
#: 토큰화 전에 잰 크기가 상한을 넘었다. **미검사다.**
ERROR_TOO_LARGE = "content_too_large"

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class PageImages:
    """한 쪽의 이미지 조사. **내용은 담지 않는다.**"""

    page_number: int
    #: 이 쪽에 **실제로 그려진** 서로 다른 이미지 수.
    images: int = 0
    #: 그려진 이미지 넓이의 합 ÷ 쪽 넓이. 겹치면 1을 넘을 수 있어 묶지 않는다.
    area_ratio: float = 0.0
    #: 한 번의 그리기가 덮은 가장 큰 비율.
    largest_ratio: float = 0.0
    #: Form 을 몇 겹까지 들어갔나. 0 이면 쪽 콘텐츠에서 바로 그렸다.
    max_depth: int = 0
    #: 조사하지 못했으면 사유 코드. **빈 값이 「이미지 없음」 이 아니다.**
    error: str = ""

    @property
    def inspected(self) -> bool:
        return not self.error


def _page_area(page) -> float:
    """쪽의 넓이. 못 읽으면 A4 로 둔다 — 분모가 없으면 비율이 없다."""
    try:
        box = page.mediabox
        area = abs(float(box.width) * float(box.height))
    except Exception:  # noqa: BLE001 - 넓이를 못 읽는 것이 실패는 아니다
        return FALLBACK_PAGE_AREA
    return area if area > 1.0 else FALLBACK_PAGE_AREA


def _multiply(inner, outer) -> tuple[float, ...]:
    """PDF 행렬 곱 `inner × outer`. 안쪽이 먼저 적용된다."""
    a, b, c, d, e, f = inner
    oa, ob, oc, od, oe, of = outer
    return (
        a * oa + b * oc, a * ob + b * od,
        c * oa + d * oc, c * ob + d * od,
        e * oa + f * oc + oe, e * ob + f * od + of,
    )


def _unit_area(ctm) -> float:
    """단위 사각형이 이 행렬로 변환됐을 때의 넓이 = |a·d − b·c|."""
    return abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])


def _numbers(operands, count: int) -> list[float] | None:
    """숫자 `count` 개. 못 읽거나 **유한하지 않으면** `None`.

    `inf`·`nan` 이 섞인 PDF 가 있다. 그대로 쓰면 넓이가 `nan` 이 되고, `nan` 은
    모든 비교가 거짓이라 **조용히 0 처럼 행동한다** — 그림이 큰 쪽이 그림 없는
    쪽으로 닫힌다.
    """
    try:
        values = [float(x) for x in list(operands)[:count]]
    except (TypeError, ValueError):
        return None
    if len(values) != count or not all(math.isfinite(v) for v in values):
        return None
    return values


class _Walk:
    """한 쪽을 걸어 내려가며 모으는 것. **좌표와 숫자뿐이다.**"""

    def __init__(self, area: float) -> None:
        self.area = area
        self.total = 0.0
        self.largest = 0.0
        self.max_depth = 0
        self.exhausted = False
        self._budget = MAX_OPERATIONS
        # 조사 중 만난 실패. **첫 번째를 보고한다** — 여러 개여도 사람이 할 일은
        # 「이 쪽은 못 봤다」 하나다.
        self.failures: list[str] = []
        self.content_bytes = 0
        # 서로 다른 이미지 몇 장인가. 같은 이미지를 두 번 그리면 넓이는 두 번
        # 더하지만 장수는 하나다.
        self.drawn: set = set()

    def fail(self, code: str) -> None:
        if code not in self.failures:
            self.failures.append(code)

    def may_read(self, size: int) -> bool:
        """이만큼 더 토큰화해도 되나. **파싱 전에 묻는다.**"""
        if size > MAX_CONTENT_BYTES:
            return False
        if self.content_bytes + size > MAX_PAGE_CONTENT_BYTES:
            return False
        self.content_bytes += size
        return True

    def spend(self) -> bool:
        self._budget -= 1
        if self._budget <= 0:
            self.exhausted = True
            return False
        return True

    def add(self, ratio: float, key) -> float:
        # 한 번의 그리기가 쪽보다 넓을 수 없다. 행렬이 이상한 PDF 에서 비율이
        # 폭주하면 그 숫자로 모든 쪽이 의심이 된다.
        ratio = min(max(ratio, 0.0), 1.0)
        self.total += ratio
        self.largest = max(self.largest, ratio)
        self.drawn.add(key)
        return ratio


def _decoded_size(source) -> int:
    """토큰화하기 **전에** 디코드된 크기를 잰다.

    `ContentStream.operations` 는 스트림 전체를 한 번에 토큰화한 뒤 돌려주므로,
    그 뒤에 거는 연산 상한으로는 **파싱 비용을 하나도 못 막는다.**
    """
    from pypdf.generic import ArrayObject

    obj = source.get_object()
    if isinstance(obj, ArrayObject):
        return sum(len(part.get_object().get_data()) for part in obj)
    return len(obj.get_data())


def _content_of(source, pdf, state: _Walk, *, on_error: str):
    """콘텐츠 스트림. **못 열거나 너무 크면 `None`** 이고 사유를 남긴다.

    전에는 실패를 조용히 삼켜 그 가지가 「이미지 0건」 으로 보였다.
    """
    from pypdf.generic import ContentStream

    if source is None:
        return None
    try:
        size = _decoded_size(source)
    except Exception:  # noqa: BLE001 - 못 여는 스트림은 **모르는 것**이다
        state.fail(on_error)
        return None
    if not state.may_read(size):
        # 파싱하지 않는다. **미검사로 둔다** — 「그림이 없다」 가 아니다.
        state.fail(ERROR_TOO_LARGE)
        return None
    try:
        return ContentStream(source, pdf)
    except Exception:  # noqa: BLE001
        state.fail(on_error)
        return None


def _resources(obj, inherited):
    """Form 이 자기 리소스를 들고 있으면 그것, 없으면 물려받은 것."""
    try:
        own = obj.get("/Resources")
    except Exception:  # noqa: BLE001
        return inherited
    return own.get_object() if own is not None else inherited


def _bbox_cap(obj, ctm, area: float) -> float:
    """Form 의 `/BBox` 가 변환된 넓이. 그 안쪽만 그려지므로 **상한**이다.

    클리핑을 제대로 계산하지는 않는다. 다만 Form 서브트리가 BBox 밖으로 나갈 수
    없다는 사실은 확실하므로, 그만큼은 과대평가를 깎을 수 있다.
    """
    try:
        box = _numbers(obj.get("/BBox"), 4)
    except Exception:  # noqa: BLE001 - BBox 가 없거나 읽히지 않는 Form 이 있다
        return float("inf")
    if box is None:
        return float("inf")
    width = abs(box[2] - box[0])
    height = abs(box[3] - box[1])
    if not width or not height or not area:
        return float("inf")
    return (width * height * _unit_area(ctm)) / area


def _walk(content, resources, ctm, state: _Walk, *, pdf, depth: int,
          path: frozenset) -> float:
    """콘텐츠 스트림 하나. 돌려주는 값은 **이 스트림이 더한 비율**이다."""
    state.max_depth = max(state.max_depth, depth)
    added = 0.0
    stack: list = []
    current = ctm
    try:
        operations = content.operations
    except Exception:  # noqa: BLE001 - 토큰화가 깨졌다. **모르는 것이다**
        state.fail(ERROR_FORM_STREAM if depth else ERROR_XOBJECT)
        return added
    for operands, operator in operations:
        if not state.spend():
            return added
        if operator == b"q":
            stack.append(current)
        elif operator == b"Q":
            current = stack.pop() if stack else ctm
        elif operator == b"cm":
            new = _numbers(operands, 6)
            if new is None:
                # 숫자가 아니거나 `inf`·`nan` 이다. 여기서부터 이 스트림의 좌표를
                # 믿을 수 없으므로 **그대로 두고 사유를 남긴다.**
                state.fail(ERROR_MATRIX)
            else:
                current = _multiply(new, current)
        elif operator == b"Do" and operands:
            added += _draw(operands[0], resources, current, state,
                           pdf=pdf, depth=depth, path=path)
    return added


def _draw(name, resources, ctm, state: _Walk, *, pdf, depth: int,
          path: frozenset) -> float:
    """`Do` 하나. 이미지면 넓이를 더하고, Form 이면 들어간다."""
    # **못 푼 이름을 0 으로 적지 않는다.** `Do` 가 있다는 것은 무언가를 그렸다는
    # 뜻이고, 그것이 무엇인지 모르면 그 쪽은 「그림이 없는 쪽」 이 아니다.
    try:
        xobjects = (resources or {}).get("/XObject")
        entries = xobjects.get_object() if xobjects is not None else None
        if entries is None or name not in entries:
            state.fail(ERROR_XOBJECT)
            return 0.0
        reference = entries.raw_get(name) if hasattr(entries, "raw_get") else None
        obj = entries[name].get_object()
        subtype = str(obj.get("/Subtype", ""))
    except Exception:  # noqa: BLE001
        state.fail(ERROR_XOBJECT)
        return 0.0

    if subtype == "/Image":
        area = _unit_area(ctm)
        if not math.isfinite(area):
            # 행렬이 폭주했다. 0 으로 적으면 큰 그림이 없는 것처럼 보인다.
            state.fail(ERROR_MATRIX)
            return 0.0
        ratio = area / state.area if state.area else 0.0
        # 같은 이미지를 두 번 그리면 넓이는 두 번, 장수는 한 번.
        key = getattr(reference, "idnum", None)
        return state.add(ratio, key if key is not None else f"{depth}:{name}")

    if subtype != "/Form":
        return 0.0
    if depth >= MAX_FORM_DEPTH:
        # 더 안 판다. **상한에 걸린 것을 「없다」 로 적지 않는다.**
        state.exhausted = True
        return 0.0
    marker = getattr(reference, "idnum", None)
    if marker is not None and marker in path:
        # Form 이 자기를 다시 부른다. 들어가면 안 돌아온다.
        return 0.0
    try:
        raw_matrix = obj.get("/Matrix")
        matrix = _numbers(raw_matrix, 6)
        if matrix is None:
            if raw_matrix is not None:
                # `/Matrix` 는 있는데 읽을 수 없다. 항등으로 가정하면 이 Form 의
                # 넓이가 통째로 틀린다 — **가정하지 않고 사유를 남긴다.**
                state.fail(ERROR_MATRIX)
            matrix = list(_IDENTITY)
        inner_ctm = _multiply(matrix, ctm)
    except Exception:  # noqa: BLE001
        state.fail(ERROR_MATRIX)
        return 0.0
    inner = _content_of(obj, pdf, state, on_error=ERROR_FORM_STREAM)
    if inner is None:
        # 사유는 `_content_of` 가 남겼다. **0 으로 닫지 않는다.**
        return 0.0
    before_total, before_largest = state.total, state.largest
    added = _walk(
        inner, _resources(obj, resources), inner_ctm, state, pdf=pdf,
        depth=depth + 1, path=path | ({marker} if marker is not None else set()),
    )
    cap = _bbox_cap(obj, inner_ctm, state.area)
    if added > cap:
        # BBox 밖은 그려지지 않는다. 그만큼 과대평가를 깎는다.
        state.total = before_total + cap
        state.largest = max(before_largest, min(cap, 1.0))
        return cap
    return added


def inspect_page(page, number: int) -> PageImages:
    """쪽 하나. **예외를 밖으로 내지 않는다** — 조사 실패도 결과다."""
    state = _Walk(_page_area(page))
    try:
        resources = page.get("/Resources") or {}
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        content = _content_of(page.get_contents(), page.pdf,
                              state, on_error=ERROR_XOBJECT)
        if content is not None:
            _walk(content, resources, _IDENTITY, state,
                  pdf=page.pdf, depth=0, path=frozenset())
    except Exception as exc:  # noqa: BLE001 - 한 쪽 실패가 변환을 막지 않는다
        return PageImages(page_number=number, error=type(exc).__name__)

    if state.total < DEFAULT_IMAGE_AREA_RATIO and (state.exhausted or state.failures):
        # 끝까지 못 봤거나 중간에 못 읽은 것이 있고, 그때까지 찾은 것은 기준에
        # 못 미친다. **더 봤으면 넘었을 수도 있다** — 모르는 것을 「괜찮다」 로
        # 적지 않는다.
        #
        # 이미 기준을 넘겼으면 그대로 쓴다. 못 본 부분은 넓이를 **늘리기만** 하지
        # 줄이지 않으므로, 그 쪽이 의심이라는 결론은 바뀌지 않는다.
        code = state.failures[0] if state.failures else ERROR_BUDGET
        return PageImages(page_number=number, error=code, max_depth=state.max_depth)
    return PageImages(
        page_number=number, images=len(state.drawn),
        area_ratio=round(state.total, 4), largest_ratio=round(state.largest, 4),
        max_depth=state.max_depth,
    )


def inspect(data: bytes) -> dict[int, PageImages]:
    """PDF 전체. 못 읽으면 **빈 표가 아니라** 쪽마다 사유를 남긴다."""
    try:
        from pypdf import PdfReader

        pages = list(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:  # noqa: BLE001
        # **본문도 파일명도 남기지 않는다.** 예외 종류뿐이다(원칙 5).
        logger.warning("PDF 이미지 조사 실패 code=%s", type(exc).__name__)
        return {}
    out: dict[int, PageImages] = {}
    for number, page in enumerate(pages, 1):
        try:
            out[number] = inspect_page(page, number)
        except Exception as exc:  # noqa: BLE001 - 한 쪽이 터져도 나머지는 센다
            # 여기까지 오면 `inspect_page` 의 방어를 넘은 것이다. **그 쪽을
            # 목록에서 빼면 「이미지 없음」 으로 읽힌다** — 사유로 남긴다.
            logger.warning("PDF %d쪽 이미지 조사 실패 code=%s", number, type(exc).__name__)
            out[number] = PageImages(page_number=number, error=type(exc).__name__)
    return out


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
    "ERROR_BUDGET",
    "ERROR_FORM_STREAM",
    "ERROR_MATRIX",
    "ERROR_TOO_LARGE",
    "ERROR_XOBJECT",
    "FALLBACK_PAGE_AREA",
    "MAX_CONTENT_BYTES",
    "MAX_FORM_DEPTH",
    "MAX_OPERATIONS",
    "MAX_PAGE_CONTENT_BYTES",
    "PageImages",
    "image_heavy",
    "inspect",
    "inspect_page",
]
