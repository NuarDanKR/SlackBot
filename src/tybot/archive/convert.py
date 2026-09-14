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
| png/jpg/jpeg/webp | kordoc OCR | 검색용 텍스트 + PII 검사 |
| 기타 이미지·도면 | 미변환 | 원본 흔적만 보존 |

## XML 안전
hwpx 는 사용자가 올린 zip 안의 XML 이다. 표준 파서는 외부 엔티티(XXE)와
엔티티 폭탄(billion laughs)에 취약하므로 `defusedxml` 로만 파싱한다.
없으면 변환하지 않는다(fail closed).
"""
from __future__ import annotations

import contextlib
import contextvars
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass

from .external_convert import (
    ExternalConversionError,
    ExternalConverterUnavailable,
    kordoc_lines,
    office_pdf_lines,
    xlsx_lines,
)

logger = logging.getLogger("tybot.convert")


def _limit(name: str, default: int = 0) -> int:
    """상한 환경변수. **`0` 은 무제한이다.**

    기본이 무제한인 이유는 변환 손실이 되돌릴 수 없기 때문이다. 운영에서 한
    파일이 서버를 넘어뜨리면 그때 값을 건다 — 미리 걸어 두면 평소에 조용히
    자료가 사라지고, 사라진 줄도 모른다.
    """
    import os

    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s 값을 읽지 못해 무제한으로 둔다: %r", name, raw[:20])
        return 0


# 파일 하나에서 가져올 줄 수 상한.
#
# 400 이던 것을 올렸다(2026-09-07). 가정산서 같은 표는 수천 행이고 **뒤에 합계가
# 있어서**, 앞 400줄만 남으면 정작 필요한 값이 빠진다. 그런데도 답변은 정상적으로
# 나가므로 「숫자를 못 읽는다」 로만 보인다.
#
# 올려도 답변 프롬프트가 커지지 않는다 — 근거로 들어가는 것은 **검색이 고른 줄**
# 뿐이다(`AnswerEngine._max_hits`). 상한을 올리는 값은 「검색이 그 행을 찾을 수
# 있게 하는 것」 이고, 비용은 아카이브 크기와 색인이다.
# **변환 단계에서는 자르지 않는다**(2026-09-14 오너 결정).
#
# 여기서 자르면 **아카이브에 영구히 없어진다.** 답변 단계의 상한
# (`AnswerEngine._max_hits`·`MAX_EVIDENCE_CHARS`)과는 다르다 — 그쪽은 요청마다
# 다시 고르므로 손실이 아니고, 여기는 한 번 버리면 원본을 다시 올리기 전에는
# 되돌릴 수 없다.
#
# 회사 파일은 크다. 1,000행이 넘는 시트, 수백 쪽짜리 보고서가 보통이고, 그것을
# 잘라 넣으면 "봇이 숫자를 못 읽는다" 로 나타난다 — 실제로 그랬다.
#
# 상한은 **비상용으로만** 남긴다. `0` 이 기본이고 그 뜻은 무제한이다. 한 파일이
# 서버를 넘어뜨리는 상황이 실제로 생기면 그때 환경변수로 건다.
MAX_LINES = _limit("TYBOT_CONVERT_MAX_LINES")
# 줄 수와 별개로 문자 수도 묶는다. 한 셀에 긴 메모가 든 파일은 줄 수가 적어도
# 아카이브를 잡아먹는다. 텍스트 파일 상한(`files.MAX_TEXT_BYTES`)과 자릿수를 맞춘다.
MAX_TOTAL_CHARS = _limit("TYBOT_CONVERT_MAX_CHARS")
# 접을 때 남길 머리와 꼬리. **꼬리를 남기는 것이 핵심이다** — 표의 합계가 거기 있다.
FOLD_HEAD = _limit("TYBOT_CONVERT_FOLD_HEAD")
FOLD_TAIL = _limit("TYBOT_CONVERT_FOLD_TAIL")
# 셀 한 칸 길이. 긴 메모가 든 칸을 자르면 **그 칸의 뒷부분이 영영 없어진다.**
MAX_CELL = _limit("TYBOT_CONVERT_MAX_CELL")
CONVERTIBLE = {"xlsx", "xlsm", "docx", "doc", "pptx", "ppt", "pdf", "hwpx", "hwp"}


class ConvertError(RuntimeError):
    """변환 실패. 목록 줄은 남기고 경고로 올린다."""


def _clip(s: object) -> str:
    t = str(s).replace("\r", " ").replace("\n", " ").strip()
    if not MAX_CELL or len(t) <= MAX_CELL:
        return t
    _record("", flag=CELL_TRUNCATED)
    return t[:MAX_CELL] + "…"


def _finish(lines: list[str]) -> list[str]:
    """상한을 넘으면 **가운데를 접는다.** 뒤를 자르지 않는다.

    표는 머리(헤더)와 꼬리(합계)가 둘 다 필요하다. 앞에서 잘라 내면 헤더는 남고
    합계가 사라지는데, 사람이 묻는 값은 대개 합계다.
    """
    lines = [ln for ln in lines if ln.strip()]
    total = len(lines)
    if MAX_LINES and total > MAX_LINES:
        # 접은 것도 **덜 읽은 것**이다. 어느 형식이든 여기를 지나므로
        # 한 자리에서 남긴다.
        _record("", flag=LINE_LIMIT_REACHED)
        dropped = total - FOLD_HEAD - FOLD_TAIL
        lines = [
            *lines[:FOLD_HEAD],
            f"…(가운데 {dropped}줄 생략, 총 {total}줄)",
            *lines[-FOLD_TAIL:],
        ]

    # 문자 수 상한. **기본은 무제한** — 걸면 뒤가 통째로 사라진다.
    if not MAX_TOTAL_CHARS:
        return lines
    used = 0
    head: list[str] = []
    for line in lines:
        if used + len(line) > MAX_TOTAL_CHARS:
            _record("", flag=CHAR_LIMIT_REACHED)
            head.append(f"…(문자 수 상한 {MAX_TOTAL_CHARS:,}자 도달, 이후 생략)")
            break
        head.append(line)
        used += len(line)
    return head


def _sheet_rows(wb) -> dict[str, tuple[list[tuple[int, list[str]]], int]]:
    """시트별로 (남긴 행, 전체 행 수).

    **기본은 전부 남긴다**(2026-09-14). 상한을 걸면 그 시트의 가운데가 아카이브에
    영영 없어지고, 사람이 묻는 값이 하필 거기 있으면 「자료가 없다」 로 답한다.

    상한을 걸었을 때만 머리와 꼬리를 남긴다. **꼬리가 핵심이다** — 표의 합계가
    거기 있다. 앞에서 상한에 걸려 멈추면 합계를 아예 못 읽는다(2026-09-07 실측).

    행마다 절대 위치를 함께 준다 — 수식 폴백이 같은 칸끼리 맞추려면 위치가 필요하다.
    빈 셀은 빈 문자열로 남겨 자리를 지킨다.
    """
    from collections import deque

    out: dict[str, tuple[list[tuple[int, list[str]]], int]] = {}
    for ws in wb.worksheets:
        head: list[tuple[int, list[str]]] = []
        # `maxlen=0` 은 **전부 버린다.** 무제한을 0 으로 쓰는 우리 규칙과 뜻이
        # 정반대라, 상한이 없을 때는 큐를 쓰지 않는다.
        tail: deque[tuple[int, list[str]]] | None = (
            deque(maxlen=FOLD_TAIL) if FOLD_TAIL else None
        )
        total = 0
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            cells = [_clip(c) if c is not None else "" for c in row]
            if not any(cells):
                continue
            total += 1
            if not FOLD_HEAD or len(head) < FOLD_HEAD:
                head.append((index, cells))
            elif tail is not None:
                tail.append((index, cells))
        picked = head
        if tail:
            last = head[-1][0] if head else -1
            picked = head + [item for item in tail if item[0] > last]
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
    # **시트와 행을 센다.** 가운데를 접은 표는 「전부 읽었다」 가 아니다 —
    # 사람이 묻는 값이 하필 접힌 자리에 있으면 「자료가 없다」 로 답하게 된다.
    _record("sheet", total=len(values), converted=len(values))
    for title, (rows, total) in values.items():
        if total > len(rows):
            _record("", flag=ROW_LIMIT_REACHED)
        out.append(f"[시트] {title}")
        source = dict(formulas.get(title, ([], 0))[0])
        shown = 0
        previous = -1
        for index, row in rows:
            if FOLD_HEAD and previous >= 0 and index > previous + 1 and shown >= FOLD_HEAD:
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
    slides = list(prs.slides)
    empty: list[int] = []
    for i, slide in enumerate(slides, 1):
        before = len(out)
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
        if len(out) == before + 1:
            # 머리줄만 남았다 = 글자를 하나도 못 읽었다. **그림만 있는
            # 슬라이드는 읽은 것으로 세지 않는다** — 그 그림이 표일 수 있다.
            empty.append(i)
    _record(
        "slide",
        total=len(slides),
        converted=len(slides) - len(empty),
        missing=tuple(empty),
    )
    return _finish(out)


def _docx(data: bytes) -> list[str]:
    try:
        return _finish(kordoc_lines(data, "docx"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("DOCX 정밀 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        _record("", flag=FALLBACK_CONVERTER)
        return [f"[변환 안내] 문서 내 이미지 미해석: {exc}", *_docx_basic(data)]


def _pptx(data: bytes) -> list[str]:
    try:
        return _finish(office_pdf_lines(data, "pptx"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        logger.warning("PPTX 시각 변환기를 쓰지 못해 기본 변환으로 전환: %s", exc)
        # 폴백은 정밀 변환과 **동등하지 않다**(설계 §6). 글자는 나와도 도형·
        # 이미지 안의 값은 안 나온다 — 그걸 성공으로 닫으면 사람은 전부 본 줄 안다.
        _record("", flag=FALLBACK_CONVERTER)
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
    # **쪽마다 읽었는지 센다.** 한 쪽이 실패해도 전체를 막지 않는 것은 맞지만,
    # 그 사실을 아무 데도 안 남기면 3쪽만 읽은 10쪽 문서가 「성공」 이 된다.
    total_pages = len(reader.pages)
    read_pages: list[int] = []
    missing_pages: list[int] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 한 페이지 실패가 전체를 막지 않는다
            missing_pages.append(i)
            continue
        lines = [_clip(ln) for ln in text.splitlines() if ln.strip()]
        if lines:
            out.append(f"[{i}쪽]")
            out.extend(lines)
            read_pages.append(i)
        else:
            # 글자가 없는 쪽. 그림만 있는 쪽일 수도, 추출이 실패한 것일 수도
            # 있다 — **어느 쪽인지 모르므로 읽었다고 세지 않는다.**
            missing_pages.append(i)
    _record(
        "page",
        total=total_pages,
        converted=len(read_pages),
        missing=tuple(missing_pages),
    )
    extracted_chars = sum(len(line) for line in out if not line.startswith("["))
    if out and extracted_chars < 200:
        try:
            return _finish(kordoc_lines(data, "pdf", force_ocr=True))
        except (ExternalConverterUnavailable, ExternalConversionError) as exc:
            logger.warning("PDF 본문이 짧지만 OCR을 쓰지 못함: %s", exc)
            out.insert(0, f"[변환 안내] 이미지 본문 OCR 미사용: {exc}")
            # 글자가 모자란데 OCR 도 못 썼다. 읽은 것이 원본의 전부라고 말할 수 없다.
            _record("", flag=OCR_UNAVAILABLE)
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
        _record("", flag=FALLBACK_CONVERTER)
        return [f"[변환 안내] HWPX 표·배치 단순화: {exc}", *_hwpx_basic(data)]


def _hwp(data: bytes) -> list[str]:
    try:
        return _finish(kordoc_lines(data, "hwp"))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        raise ConvertError(f"HWP 변환 실패: {exc}") from exc


def _image(data: bytes, suffix: str) -> list[str]:
    """이미지의 글자를 로컬 OCR로 추출한다.

    원본 이미지는 검색 아카이브에 넣지 않는다. 이 결과가 writer의 PII 검사를
    통과한 뒤에만 검색과 시각 질의 경로에서 사용할 수 있다.
    """
    try:
        return _finish(kordoc_lines(data, suffix, force_ocr=True))
    except (ExternalConverterUnavailable, ExternalConversionError) as exc:
        raise ConvertError(f"이미지 OCR 실패: {exc}") from exc


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
    "png": lambda data: _image(data, "png"),
    "jpg": lambda data: _image(data, "jpg"),
    "jpeg": lambda data: _image(data, "jpeg"),
    "webp": lambda data: _image(data, "webp"),
}


# --- 얼마나 읽었나 (B-45 §5·§6) ----------------------------------------------
#
# **「본문이 나왔다」 와 「다 읽었다」 는 다르다.** 10쪽 PDF 에서 3쪽만 읽혀도 지금은
# 성공이고, 그 답에 우리 출처가 붙는다. 사람은 전부 본 줄 안다.
#
# 모르는 개수는 `None` 이다. **0 이나 100% 로 만들지 않는다** — 모르는 것을 안다고
# 적으면 그 숫자는 더 이상 근거가 아니다. 외부 변환기가 상세를 안 주면 `None` 이고,
# 그때는 `partial` 도 `succeeded` 도 단정하지 않는다.
_coverage: contextvars.ContextVar[Coverage | None] = contextvars.ContextVar(
    "tybot_convert_coverage", default=None
)

SUCCEEDED = "succeeded"
PARTIAL = "partial"
UNKNOWN = "unknown"

# 품질 플래그. 숫자로 표현 못 하는 손실을 이름으로 남긴다.
ROW_LIMIT_REACHED = "row_limit_reached"
LINE_LIMIT_REACHED = "line_limit_reached"
CELL_TRUNCATED = "cell_truncated"
CHAR_LIMIT_REACHED = "char_limit_reached"
# 정밀 변환기를 못 써서 기본 파서로 내려갔다. **동등하지 않다**(설계 §6) —
# 글자는 나와도 표·도형·이미지 안의 값은 안 나온다.
FALLBACK_CONVERTER = "fallback_converter"
# 렌더 과정에서 단위(슬라이드·쪽)가 줄었다. 원본에 있던 것이 산출물에 없다.
RENDER_LOST_UNITS = "render_lost_units"
OCR_UNAVAILABLE = "ocr_unavailable"
TITLE_ONLY = "title_only"


@dataclass
class Coverage:
    """이번 변환이 원본의 얼마를 읽었나.

    `unit` 은 세는 단위다(`page`·`sheet`). 셀 수 없는 형식에서는 빈 문자열이고
    개수도 `None` 이다 — HWP 처럼 외부 도구가 상세를 안 주는 경우다.
    """

    unit: str = ""
    total: int | None = None
    converted: int | None = None
    # 못 읽은 단위 번호. **본문은 넣지 않는다** — 좌표만이다.
    missing: tuple[int, ...] = ()
    flags: tuple[str, ...] = ()

    def note(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags = (*self.flags, flag)

    @property
    def state(self) -> str:
        """`succeeded` · `partial` · `unknown`.

        플래그만 있어도 `partial` 이다 — 행 상한에 걸린 시트는 숫자로는 전부
        읽은 것처럼 보이지만 실제로는 잘렸다.
        """
        if self.missing or self.flags:
            return PARTIAL
        if self.total is None or self.converted is None:
            return UNKNOWN
        return SUCCEEDED if self.converted >= self.total else PARTIAL

    def summary(self) -> str:
        """사람에게 보일 한 줄. 모르면 모른다고 쓴다."""
        if self.total is None or self.converted is None:
            return "확인 범위 미상" if self.flags else ""
        unit = {"page": "쪽", "sheet": "시트"}.get(self.unit, "개")
        line = f"확인 {self.converted}/{self.total}{unit}"
        if self.missing:
            shown = ", ".join(str(n) for n in self.missing[:8])
            more = f" 외 {len(self.missing) - 8}" if len(self.missing) > 8 else ""
            line += f" · 미확인 {shown}{more}{unit}"
        return line

    def to_json(self) -> dict:
        """metadata 에 실을 모양. 모르는 값은 `null` 로 남는다."""
        return {
            "coverage_unit": self.unit or None,
            "coverage_total": self.total,
            "coverage_converted": self.converted,
            "missing_units": list(self.missing[:50]),
            "quality_flags": list(self.flags),
            "coverage_state": self.state,
        }


def current_coverage() -> Coverage | None:
    """지금 변환의 coverage. 변환 밖에서는 `None`."""
    return _coverage.get()


def _record(unit: str, *, total: int | None = None, converted: int | None = None,
            missing=(), flag: str = "") -> None:
    """변환 함수들이 부르는 기록 자리. 수집 중이 아니면 아무 일도 안 한다."""
    cov = _coverage.get()
    if cov is None:
        return
    if unit:
        cov.unit = unit
    if total is not None:
        cov.total = total
    if converted is not None:
        cov.converted = converted
    if missing:
        cov.missing = tuple(dict.fromkeys((*cov.missing, *missing)))
    if flag:
        cov.note(flag)


@contextlib.contextmanager
def collect_coverage():
    """이 블록 안의 변환이 얼마나 읽었는지 모은다.

    **호출부가 `convert()` 를 그대로 부를 수 있게** 하는 이음매다. 호출부를
    `convert_with_coverage()` 로 바꾸면 그 함수를 갈아 끼우던 테스트가 조용히
    무력해진다 — 실제로 그랬다(`test_image_is_ocr_converted_...`).
    """
    cov = Coverage()
    token = _coverage.set(cov)
    try:
        yield cov
    finally:
        _coverage.reset(token)


def convert_with_coverage(filetype: str, data: bytes) -> tuple[list[str], Coverage]:
    """변환 + **얼마나 읽었는지**.

    `convert()` 의 반환형(`list[str]`)을 바꾸지 않는 이유는 호출부가 여럿이고,
    그중 하나라도 놓치면 조용히 깨지기 때문이다. 필요한 쪽만 이 함수를 쓴다.
    """
    with collect_coverage() as cov:
        lines = convert(filetype, data)
    if lines and not _has_body(lines):
        # 제목만 있고 본문이 없다. **성공이 아니다**(설계 §5).
        cov.note(TITLE_ONLY)
    return lines, cov


# 구조 표시 줄. 이것만 있으면 본문을 읽은 것이 아니다.
_MARKER_PREFIXES = ("[", "#")


def _has_body(lines: list[str]) -> bool:
    return any(
        line.strip() and not line.strip().startswith(_MARKER_PREFIXES)
        for line in lines
    )


def can_convert(filetype: str) -> bool:
    return filetype.lower() in _HANDLERS


def convert(filetype: str, data: bytes) -> list[str]:
    """확장자별 변환. 실패는 ConvertError 로 올린다."""
    fn = _HANDLERS.get(filetype.lower())
    if fn is None:
        raise ConvertError(f"'{filetype}' 은 변환 대상이 아니다")
    if not data:
        raise ConvertError("빈 파일")
    if os.getenv("TYBOT_CONVERT_SPOOL"):
        from pathlib import Path

        from .conversion_worker import request

        try:
            return request(Path(os.environ["TYBOT_CONVERT_SPOOL"]), filetype.lower(), data)
        except ExternalConversionError as exc:
            raise ConvertError(str(exc)) from exc
    return convert_local(filetype, data)


def convert_local(filetype: str, data: bytes) -> list[str]:
    """Worker entry point; never routes back into the spool client."""
    fn = _HANDLERS.get(filetype.lower())
    if fn is None or not data:
        raise ConvertError("Unsupported or empty document")
    return fn(data)
