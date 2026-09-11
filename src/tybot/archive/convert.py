"""첨부 문서 → 텍스트 변환.

실사용자가 올리는 건 xlsx·pdf·한글·워드·PPT 다. 개발자 텍스트 파일만 수집하면
맥락의 대부분을 놓친다. 그래서 변환한다 — 단 아래 규칙을 지킨다.

## 규칙
1. **변환본은 원문 라인으로 들어가되 `[첨부추출:파일명]` 로 표시한다.**
   사람이 "이건 자동 변환된 텍스트"임을 항상 알 수 있어야 한다.
2. **표는 구조를 살려 옮긴다**(시트명·행 단위). 요약·정리하지 않는다 — 그건 LLM 이 할 일이다.
3. **변환 실패는 조용히 넘기지 않는다.** 목록 줄은 남기고 경고를 올린다.
4. **OCR 결과는 변환 안내로 표시한다.** 금액·날짜·조문은 원본 확인이 필요하다.
5. 외부 변환기가 없으면 기본 파서로 내리거나 미변환 처리하고 경고를 남긴다.

## 지원
| 형식 | 방법 | 비고 |
|---|---|---|
| xlsx/xlsm | Hermes 유래 openpyxl 렌더러 | 표시값·표·시트·마스킹 메타 |
| docx | kordoc, python-docx 폴백 | 문단 + 표 |
| pptx/ppt | LibreOffice PDF + kordoc | 시각 배치·이미지 OCR, 기본 텍스트 폴백 |
| pdf | pypdf + kordoc OCR | 텍스트가 없거나 매우 짧으면 OCR |
| hwpx | kordoc, 안전 XML 폴백 | 표·배치 우선, XML은 텍스트 폴백 |
| hwp(구형 바이너리) | kordoc | 미설치 시 명시적 실패 |
| 이미지·도면 | 미변환 | OCR 미도입 |

## XML 안전
hwpx 는 사용자가 올린 zip 안의 XML 이다. 표준 파서는 외부 엔티티(XXE)와
엔티티 폭탄(billion laughs)에 취약하므로 `defusedxml` 로만 파싱한다.
없으면 변환하지 않는다(fail closed).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile

from .external_convert import (
    ExternalConversionError,
    ExternalConverterUnavailable,
    kordoc_lines,
    office_pdf_lines,
    xlsx_lines,
)

logger = logging.getLogger("tybot.convert")

# 파일 하나에서 가져올 줄 수 상한.
#
# 400 이던 것을 올렸다(2026-09-07). 가정산서 같은 표는 수천 행이고 **뒤에 합계가
# 있어서**, 앞 400줄만 남으면 정작 필요한 값이 빠진다. 그런데도 답변은 정상적으로
# 나가므로 「숫자를 못 읽는다」 로만 보인다.
#
# 올려도 답변 프롬프트가 커지지 않는다 — 근거로 들어가는 것은 **검색이 고른 줄**
# 뿐이다(`AnswerEngine._max_hits`). 상한을 올리는 값은 「검색이 그 행을 찾을 수
# 있게 하는 것」 이고, 비용은 아카이브 크기와 색인이다.
MAX_LINES = 20_000
# 줄 수와 별개로 문자 수도 묶는다. 한 셀에 긴 메모가 든 파일은 줄 수가 적어도
# 아카이브를 잡아먹는다. 텍스트 파일 상한(`files.MAX_TEXT_BYTES`)과 자릿수를 맞춘다.
MAX_TOTAL_CHARS = 300_000
# 접을 때 남길 머리와 꼬리. **꼬리를 남기는 것이 핵심이다** — 표의 합계가 거기 있다.
FOLD_HEAD = 12_000
FOLD_TAIL = 4_000
MAX_CELL = 200  # 셀 한 칸 길이 상한
CONVERTIBLE = {"xlsx", "xlsm", "docx", "doc", "pptx", "ppt", "pdf", "hwpx", "hwp"}


class ConvertError(RuntimeError):
    """변환 실패. 목록 줄은 남기고 경고로 올린다."""


def _clip(s: object) -> str:
    t = str(s).replace("\r", " ").replace("\n", " ").strip()
    return t if len(t) <= MAX_CELL else t[:MAX_CELL] + "…"


def _finish(lines: list[str]) -> list[str]:
    """상한을 넘으면 **가운데를 접는다.** 뒤를 자르지 않는다.

    표는 머리(헤더)와 꼬리(합계)가 둘 다 필요하다. 앞에서 잘라 내면 헤더는 남고
    합계가 사라지는데, 사람이 묻는 값은 대개 합계다.
    """
    lines = [ln for ln in lines if ln.strip()]
    total = len(lines)
    if total > MAX_LINES:
        dropped = total - FOLD_HEAD - FOLD_TAIL
        lines = [
            *lines[:FOLD_HEAD],
            f"…(가운데 {dropped}줄 생략, 총 {total}줄)",
            *lines[-FOLD_TAIL:],
        ]

    # 문자 수 상한. 줄 수가 적어도 셀에 긴 메모가 들면 여기서 걸린다.
    used = 0
    head: list[str] = []
    for line in lines:
        if used + len(line) > MAX_TOTAL_CHARS:
            head.append(f"…(문자 수 상한 {MAX_TOTAL_CHARS:,}자 도달, 이후 생략)")
            break
        head.append(line)
        used += len(line)
    return head


def _sheet_rows(wb) -> dict[str, tuple[list[tuple[int, list[str]]], int]]:
    """시트별로 (머리+꼬리 행, 전체 행 수).

    **한 번만 흘려 읽으면서 꼬리를 큐에 남긴다.** 앞에서 상한에 걸려 멈추면 표의
    합계 행을 아예 읽지 못하는데, 사람이 묻는 값은 대개 그 합계다(2026-09-07 실측:
    안전 상한이 꼬리 남기기를 무력화했다).

    행마다 절대 위치를 함께 준다 — 수식 폴백이 같은 칸끼리 맞추려면 위치가 필요하다.
    빈 셀은 빈 문자열로 남겨 자리를 지킨다.
    """
    from collections import deque

    out: dict[str, tuple[list[tuple[int, list[str]]], int]] = {}
    for ws in wb.worksheets:
        head: list[tuple[int, list[str]]] = []
        tail: deque[tuple[int, list[str]]] = deque(maxlen=FOLD_TAIL)
        total = 0
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            cells = [_clip(c) if c is not None else "" for c in row]
            if not any(cells):
                continue
            total += 1
            if len(head) < FOLD_HEAD:
                head.append((index, cells))
            else:
                tail.append((index, cells))
        picked = head + [item for item in tail if item[0] > (head[-1][0] if head else -1)]
        out[ws.title] = (picked, total)
    return out

def _xlsx_basic(data: bytes) -> list[str]:
    """엑셀 → 텍스트. **수식이 있는 칸을 비워 두지 않는다.**

    `data_only=True` 는 openpyxl 이 **저장 시 캐시된 값**을 준다. 파일이 계산 없이
    저장됐으면(스크립트 생성·일부 도구) 그 값이 `None` 이고, 그러면 합계 행이
    이름만 남는다 — `합계 | | ` 가 아니라 그냥 `합계` 로. 실제로 그랬다(2026-09-07):
    금액과 비율이 통째로 사라져 「표 인식 실패」 로 보였다.

    그래서 값이 빈 칸이 있으면 **수식을 한 번 더 읽어** 그 자리에 넣는다.
    `=SUM(B2:B120)` 은 값이 아니지만, 무엇을 계산하는 칸인지는 알려 준다 —
    빈칸보다 낫고, 값처럼 보이지도 않는다.

    값이 충분히 나온 파일은 두 번째 읽기를 하지 않는다(평소에는 비용이 없다).
    """
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise ConvertError("openpyxl 미설치 - pip install openpyxl") from e

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        values = _sheet_rows(wb)
        has_macro = bool(getattr(wb, "vba_archive", None))
    finally:
        wb.close()

    blanks = sum(
        1
        for rows, _ in values.values()
        for _, row in rows
        for cell in row
        if not cell
    )
    formulas: dict[str, list[list[str]]] = {}
    if blanks:
        wb2 = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            formulas = _sheet_rows(wb2)
        finally:
            wb2.close()

    out: list[str] = []
    for title, (rows, total) in values.items():
        out.append(f"[시트] {title}")
        source = dict(formulas.get(title, ([], 0))[0])
        shown = 0
        previous = -1
        for index, row in rows:
            if previous >= 0 and index > previous + 1 and shown >= FOLD_HEAD:
                # 가운데를 접었다는 사실을 그 자리에 남긴다. 뒤에 오는 것이 꼬리다.
                out.append(f"…(가운데 {total - len(rows)}줄 생략, 총 {total}줄)")
            previous = index
            shown += 1
            raw_row = source.get(index, [])
            cells: list[str] = []
            for j, cell in enumerate(row):
                if cell:
                    cells.append(cell)
                    continue
                raw = raw_row[j] if j < len(raw_row) else ""
                # 수식만 채운다. 원래 빈 칸은 그대로 비워 둔다.
                if raw.startswith("="):
                    cells.append(raw)
            if cells:
                out.append(" | ".join(cells))
    if has_macro:
        # **매크로 코드는 넣지 않는다.** 코드는 사실이 아니고, 근거로 쓰이면
        # 「그렇게 계산하기로 되어 있다」 를 「그렇게 계산됐다」 로 읽게 된다.
        # 있다는 사실만 남긴다 — 사람이 원본을 봐야 한다는 신호다.
        out.append("[안내] 이 파일에는 매크로가 있습니다. 계산 로직은 원본을 확인하세요.")
    return _finish(out)


def _xlsx(data: bytes) -> list[str]:
    """Use the Hermes-derived renderer; retain the old parser as an explicit fallback."""
    try:
        return _finish(xlsx_lines(data))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("Excel 정밀 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        return [f"[변환 안내] 정밀 표 변환 미사용: {exc}", *_xlsx_basic(data)]

def _docx_basic(data: bytes) -> list[str]:
    try:
        import docx
    except ImportError as e:
        raise ConvertError("python-docx 미설치 - pip install python-docx") from e

    d = docx.Document(io.BytesIO(data))
    out = [_clip(p.text) for p in d.paragraphs if p.text.strip()]
    for ti, table in enumerate(d.tables, 1):
        out.append(f"[표 {ti}]")
        for row in table.rows:
            cells = [_clip(c.text) for c in row.cells if c.text.strip()]
            if cells:
                out.append(" | ".join(cells))
    return _finish(out)


def _pptx_basic(data: bytes) -> list[str]:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise ConvertError("python-pptx 미설치 - pip install python-pptx") from e

    prs = Presentation(io.BytesIO(data))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"[슬라이드 {i}]")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for p in shape.text_frame.paragraphs:
                    txt = "".join(r.text for r in p.runs).strip()
                    if txt:
                        out.append(_clip(txt))
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [_clip(c.text) for c in row.cells if c.text.strip()]
                    if cells:
                        out.append(" | ".join(cells))
    return _finish(out)


def _docx(data: bytes) -> list[str]:
    try:
        return _finish(kordoc_lines(data, "docx"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("DOCX 정밀 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        return [f"[변환 안내] 문서 내 이미지 미해석: {exc}", *_docx_basic(data)]


def _pptx(data: bytes) -> list[str]:
    try:
        return _finish(office_pdf_lines(data, "pptx"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("PPTX 시각 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        return [f"[변환 안내] 슬라이드 이미지·도형 미해석: {exc}", *_pptx_basic(data)]


def _pdf(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ConvertError("pypdf 미설치 - pip install pypdf") from e

    reader = PdfReader(io.BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        raise ConvertError("암호가 걸린 PDF - 변환하지 않음")
    out: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 한 페이지 실패가 전체를 막지 않는다
            continue
        lines = [_clip(ln) for ln in text.splitlines() if ln.strip()]
        if lines:
            out.append(f"[{i}쪽]")
            out.extend(lines)
    extracted_chars = sum(len(line) for line in out if not line.startswith("["))
    if out and extracted_chars < 200:
        try:
            return _finish(kordoc_lines(data, "pdf", force_ocr=True))
        except (ExternalConverterUnavailable, ExternalConversionError) as exc:
            logger.warning("PDF 본문이 짧지만 OCR을 쓰지 못함: %s", exc)
            out.insert(0, f"[변환 안내] 이미지 본문 OCR 미사용: {exc}")
    if not out:
        try:
            return _finish(kordoc_lines(data, "pdf", force_ocr=True))
        except ExternalConverterUnavailable as exc:
            raise ConvertError(f"텍스트 레이어 없음 - OCR 변환기 미설치: {exc}") from exc
        except ExternalConversionError as exc:
            raise ConvertError(f"스캔 PDF OCR 실패: {exc}") from exc
    return _finish(out)


HWPX_TEXT_TAGS = ("t", "char")


def _hwpx_basic(data: bytes) -> list[str]:
    """hwpx 는 zip + XML 이다. 텍스트 노드만 순서대로 뽑는다.

    파싱은 defusedxml 로만 한다 - 사용자가 올린 XML 이므로 XXE·엔티티 폭탄 대상이다.
    """
    try:
        from defusedxml.common import DefusedXmlException
        from defusedxml.ElementTree import ParseError, fromstring
    except ImportError as e:
        raise ConvertError(
            "defusedxml 미설치 - 사용자 XML 을 표준 파서로 열지 않는다. pip install defusedxml"
        ) from e

    out: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.startswith("Contents/") and n.endswith(".xml")]
            if not names:
                raise ConvertError("hwpx 구조가 예상과 다르다")
            for name in sorted(names):
                try:
                    root = fromstring(z.read(name))
                except ParseError:
                    continue
                except DefusedXmlException as e:
                    # 외부 엔티티·엔티티 폭탄 등. 방어했다는 사실을 남기고 파일 전체를 거부한다.
                    logger.warning("악성 XML 구조 감지 - 변환 거부: %s", e)
                    raise ConvertError(f"안전하지 않은 XML 구조: {e.__class__.__name__}") from e
                buf: list[str] = []
                for el in root.iter():
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag in HWPX_TEXT_TAGS and el.text and el.text.strip():
                        buf.append(el.text.strip())
                if buf:
                    joined = re.sub(r"\s{2,}", " ", " ".join(buf))
                    out.extend(_clip(ln) for ln in joined.split("\n"))
    except zipfile.BadZipFile as e:
        raise ConvertError("hwpx 파일이 손상됐거나 구형 hwp 형식") from e
    if not out:
        raise ConvertError("hwpx 에서 텍스트를 찾지 못했다")
    return _finish(out)


def _hwpx(data: bytes) -> list[str]:
    try:
        return _finish(kordoc_lines(data, "hwpx"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("HWPX 정밀 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        return [f"[변환 안내] HWPX 표·배치 단순화: {exc}", *_hwpx_basic(data)]


def _hwp(data: bytes) -> list[str]:
    try:
        return _finish(kordoc_lines(data, "hwp"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        raise ConvertError(f"HWP 변환 실패: {exc}") from exc


def _legacy_office(data: bytes, suffix: str) -> list[str]:
    try:
        return _finish(office_pdf_lines(data, suffix))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        raise ConvertError(f"{suffix.upper()} 변환 실패: {exc}") from exc


_HANDLERS = {
    "xlsx": _xlsx, "xlsm": _xlsx,
    "docx": _docx, "doc": lambda data: _legacy_office(data, "doc"),
    "pptx": _pptx, "ppt": lambda data: _legacy_office(data, "ppt"),
    "pdf": _pdf,
    "hwpx": _hwpx, "hwp": _hwp,
}


def can_convert(filetype: str) -> bool:
    return filetype.lower() in _HANDLERS


def convert(filetype: str, data: bytes) -> list[str]:
    """확장자별 변환. 실패는 ConvertError 로 올린다."""
    fn = _HANDLERS.get(filetype.lower())
    if fn is None:
        raise ConvertError(f"'{filetype}' 은 변환 대상이 아니다")
    if not data:
        raise ConvertError("빈 파일")
    return fn(data)
