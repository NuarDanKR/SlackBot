"""엑셀 변환 — 수식과 합계가 살아 있는가 (2026-09-07).

사내 엑셀에는 식·함수·매크로가 많다. 그런데 변환이 둘을 놓치고 있었고,
**둘 다 오류를 내지 않았다** — 답변은 정상적으로 나가고 숫자만 없었다.

| 놓친 것 | 왜 |
|---|---|
| 수식 칸 | `data_only=True` 는 **저장 시 캐시된 값**을 준다. 계산 없이 저장된 파일은 `None` 이고 그 칸이 버려졌다 |
| 합계 행 | 400줄에서 앞부터 잘랐다. 표의 합계는 **뒤에** 있다 |
"""
from __future__ import annotations

import io

import pytest

from tybot.archive import convert


def _book(rows: list[list], *, title: str = "가정산") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- 수식 --------------------------------------------------------------------
def test_a_formula_cell_is_not_dropped():
    """계산 없이 저장된 파일에서 합계 행이 이름만 남던 것.

    금액과 비율이 통째로 사라져 「표 인식 실패」 로 보였다.
    """
    data = _book([
        ["공구", "기성금", "비율"],
        ["3공구", 320000000, 0.86],
        ["합계", "=SUM(B2:B2)", "=AVERAGE(C2:C2)"],
    ])

    lines = convert.convert("xlsx", data)

    total_row = next(line for line in lines if line.startswith("합계"))
    assert "=SUM(B2:B2)" in total_row, f"수식이 사라졌다: {total_row}"
    assert "=AVERAGE(C2:C2)" in total_row


def test_a_formula_is_not_dressed_up_as_a_value():
    """`=SUM(...)` 은 값이 아니다. 값처럼 보이면 봇이 그것을 금액으로 답한다."""
    data = _book([["합계", "=SUM(B1:B9)"]])

    lines = convert.convert("xlsx", data)

    assert any("=SUM" in line for line in lines), "수식 표시가 없다"
    assert not any("SUM(B1:B9) 원" in line for line in lines)


def test_an_empty_cell_stays_empty():
    """원래 빈 칸을 수식으로 채우지 않는다 — 없는 값을 만들면 안 된다."""
    data = _book([["가", None, "다"]])

    lines = convert.convert("xlsx", data)

    row = next(line for line in lines if line.startswith("<tr>"))
    assert row == "<tr><th>가</th><th></th><th>다</th></tr>"


def test_the_high_fidelity_renderer_is_called_once(monkeypatch):
    """정밀 변환기를 중복 실행하면 큰 보고서의 지연과 메모리가 두 배가 된다."""
    calls = {"n": 0}

    def render(_data):
        calls["n"] += 1
        return ["[시트] 가정산", "<table>", "</table>"]

    monkeypatch.setattr(convert, "xlsx_lines", render)
    convert.convert("xlsx", _book([["가", "나"], [1, 2]]))

    assert calls["n"] == 1


# --- 접기 --------------------------------------------------------------------
def test_the_total_row_survives_folding(monkeypatch):
    """상한을 넘으면 **가운데를 접는다.** 뒤를 자르지 않는다.

    앞에서 잘라 내면 헤더는 남고 합계가 사라지는데, 사람이 묻는 값은 대개 합계다.
    """
    monkeypatch.setattr(convert, "FOLD_HEAD", 3)
    monkeypatch.setattr(convert, "FOLD_TAIL", 2)
    monkeypatch.setattr(convert, "MAX_LINES", 10)
    rows = [["공구", "기성금"], *[[f"행{i}", i] for i in range(30)], ["합계", 435]]

    lines = convert.convert("xlsx", _book(rows))

    assert any("합계" in line for line in lines), "합계가 잘려 나갔다"
    assert any("가운데" in line and "생략" in line for line in lines), "접은 사실을 말해야 한다"
    assert any("공구" in line and "기성금" in line for line in lines), "헤더도 남아야 한다"


def test_the_fold_note_reports_the_real_total(monkeypatch):
    """몇 줄을 접었는지 말하지 않으면 사람이 원본을 볼 판단을 못 한다."""
    monkeypatch.setattr(convert, "FOLD_HEAD", 2)
    monkeypatch.setattr(convert, "FOLD_TAIL", 1)
    monkeypatch.setattr(convert, "MAX_LINES", 5)

    lines = convert.convert("xlsx", _book([[f"행{i}", i] for i in range(50)]))

    note = next(line for line in lines if "생략" in line)
    assert "총 53줄" in note, note  # 시트 머리와 table 시작·끝도 검색 가능한 구조 줄이다


def test_the_row_limit_is_high_enough_for_real_tables():
    """400 이던 것을 올렸다. 가정산서는 수천 행이다.

    올려도 답변 프롬프트는 커지지 않는다 — 근거로 들어가는 것은 검색이 고른 줄뿐이다.
    """
    assert convert.MAX_LINES >= 20_000
    assert convert.MAX_TOTAL_CHARS >= 300_000


# --- 매크로 ------------------------------------------------------------------
def test_macro_code_is_never_archived():
    """매크로는 코드고 사실이 아니다.

    근거로 쓰이면 「그렇게 계산하기로 되어 있다」 를 「그렇게 계산됐다」 로 읽게 된다.
    """
    source = (
        __import__("pathlib").Path(convert.__file__).read_text(encoding="utf-8")
    )

    assert "vba_archive" in source, "매크로 유무는 확인한다"
    for leaked in ("vba_archive.read", "extract(", "Sub ", "VBAProject"):
        assert leaked not in source, f"매크로 내용을 꺼내려 한다: {leaked}"


def test_a_macro_file_says_so(tmp_path):
    """계산 로직이 있다는 사실은 남긴다 — 사람이 원본을 봐야 한다는 신호다."""
    pytest.importorskip("openpyxl")
    source = (
        __import__("pathlib").Path(convert.__file__).read_text(encoding="utf-8")
    )

    assert "매크로가 있습니다" in source


# --- 텍스트 파일도 같은 규칙 ------------------------------------------------
def test_a_csv_total_row_survives(monkeypatch):
    """`csv`·`tsv` 는 텍스트로 분류돼 다른 상한을 탄다.

    두 경로가 갈리면 「csv 는 되는데 xlsx 는 안 된다」 같은 설명할 수 없는 차이가
    생긴다. 같은 이유·같은 방식으로 접는다.
    """
    from tybot.archive import files

    monkeypatch.setattr(files, "MAX_TEXT_LINES", 6)
    monkeypatch.setattr(files, "TEXT_FOLD_HEAD", 3)
    monkeypatch.setattr(files, "TEXT_FOLD_TAIL", 2)
    rows = [f"항목{i},{i * 100}" for i in range(20)] + ["합계,19000"]
    raw = "\n".join(rows).encode("utf-8")

    out = files._decode_text(raw, len(raw))

    assert "합계,19000" in out, "합계가 잘려 나갔다"
    assert "가운데" in out and "생략" in out
    assert out.splitlines()[0] == "항목0,0", "머리도 남아야 한다"


def test_a_short_text_file_is_untouched():
    """상한 안에 들면 아무 표시도 붙이지 않는다. 없는 생략을 말하면 안 된다."""
    from tybot.archive import files

    raw = "가\n나\n다".encode()

    out = files._decode_text(raw, len(raw))

    assert out == "가\n나\n다"
    assert "생략" not in out


def test_the_two_paths_use_the_same_line_budget():
    """문서 변환과 텍스트 파일이 같은 값을 쓴다 — 갈리면 설명할 수 없는 차이가 된다."""
    from tybot.archive import convert, files

    assert files.MAX_TEXT_LINES == convert.MAX_LINES
    assert files.TEXT_FOLD_HEAD == convert.FOLD_HEAD
    assert files.TEXT_FOLD_TAIL == convert.FOLD_TAIL
