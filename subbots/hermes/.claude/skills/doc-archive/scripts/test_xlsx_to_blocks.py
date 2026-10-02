#!/usr/bin/env python3
"""
xlsx_to_blocks.py 시험. **임시 폴더에서만 돌고 저장소·캐시는 안 건드린다.**

  python .claude/skills/doc-archive/scripts/test_xlsx_to_blocks.py

종료코드: 0 전부 통과 / 1 실패 있음
"""
import datetime as dt
import json
import sys
import tempfile
import zipfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))

# 종료코드 2 = **못 쟀음.** 맨 import 로 두면 openpyxl 이 없는 기계에서
# ModuleNotFoundError 로 죽어 **종료코드 1**, 즉 「시험이 깨졌다」와 같은 신호가 된다.
# openpyxl 은 VM 에 일부러 안 깐다 — 아래 `xlsx_to_blocks` 의 가드와 같은 규약이고
# `scripts/check-optional-dep-signal.js` 가 그것을 지킨다.
try:
    import openpyxl
except ImportError:
    print(
        "openpyxl 이 없습니다. pip install -r "
        ".claude/skills/doc-archive/scripts/requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)
import xlsx_to_blocks  # 안전망 시험이 _strip_rows 를 몽키패치하려면 모듈 자체가 있어야 한다
from xlsx_to_blocks import (
    render_value, sheet_to_table, build_blocks, data_bounds, EmptyFormulaError,
    _sheet_bottoms, _count_value_cells, _count_value_cells_bytes, _strip_rows,
    _sheet_titles, _sheet_related_parts, _row_refs, trim_ghost_rows,
)

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def make_book(path):
    """산정내역(정상) · 이자계산(숨긴 행 1) · backdata(큰 시트) · 숨긴시트"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "산정내역"
    ws["A1"] = "구 분"
    ws["B1"] = "약정액"
    ws.merge_cells("A1:A2")
    ws["B2"] = 1520.4
    ws["B2"].number_format = "#,##0"
    ws["C2"] = 0.125
    ws["C2"].number_format = "0.0%"

    ws2 = wb.create_sheet("이자계산")
    ws2["A1"] = "항목"
    ws2["A2"] = "보이는 행"
    ws2["A3"] = "숨긴 행"
    ws2.row_dimensions[3].hidden = True
    ws2.row_dimensions[4].hidden = True   # 빈 숨긴 행 — 내용 있는 것과 갈려야 한다

    ws3 = wb.create_sheet("backdata")
    for i in range(1, 12):
        ws3.cell(row=i, column=1, value=i)

    ws4 = wb.create_sheet("폐기안")
    ws4["A1"] = "쓰지 않는 시나리오"
    ws4.sheet_state = "hidden"

    wb.save(path)


def make_ghost_book(path, ghost_row=500, ghost_col="A", extra_sheets=False):
    """정상 3행짜리 시트에 **값도 서식도 없는 유령 셀**을 하나 끼운 파일.

    openpyxl 로는 이런 셀을 쓸 수 없다 — 값을 주면 값이 있는 셀이 되고,
    안 주면 아예 안 써진다. 그래서 정상 파일을 저장한 뒤 zip 안의
    `sheet1.xml` 에 `<row><c/></row>` 를 손으로 끼운다. 실물에서는 엑셀이
    쓰다 지운 자리에 이런 것이 남는다 (2026-08-25 실측 두 건).
    """
    import re
    import zipfile

    base = Path(path).with_suffix(".base.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "현황"
    ws["A1"] = "구 분"
    ws["B1"] = "금액"
    ws["A2"] = "대출잔액"
    ws["B2"] = 82538.6
    ws["A3"] = "보증"
    ws["B3"] = 9747.4
    if extra_sheets:
        ws2 = wb.create_sheet("메모")
        ws2["A1"] = "비고"
    wb.save(base)

    ghost = f'<row r="{ghost_row}"><c r="{ghost_col}{ghost_row}"/></row>'
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                s = data.decode("utf-8")
                s = s.replace("</sheetData>", ghost + "</sheetData>")
                s = re.sub(r'<dimension ref="[^"]*"/>', '<dimension ref="A1:B3"/>', s)
                data = s.encode("utf-8")
            zout.writestr(item, data)
    base.unlink()


def make_ghost_rows_book(path, n_ghost=300, first_ghost=50, merge_to=None,
                         comment_at=None, out_of_order=False, unnumbered_text=None):
    """값 3행짜리 시트 뒤에 **값 없는 <row> 를 여러 줄** 붙인 파일.

    `make_ghost_book` 은 유령 셀을 하나 끼우는 것이라 로드가 안 터진다.
    이쪽은 행을 여러 줄 붙여 「잘라낼 것이 있는」 상태를 만든다.

    merge_to        — 그 행까지 걸치는 병합셀을 넣는다 (구조물 바닥)
    comment_at      — 그 행에 셀 메모를 단다 (구조물 바닥)
    out_of_order    — 유령 행을 번호 역순으로 쓴다
    unnumbered_text — 값 3행 바로 뒤에 **`r` 이 없는 <row>** 를 하나 끼우고 그
                      안에 이 글자를 값으로 넣는다. `r` 은 ECMA-376 에서 선택이라
                      없으면 「앞에 몇 행이 있었나」로 번호가 정해진다 — 앞을 자르면
                      이 행이 다른 행 번호로 밀린다. openpyxl 로는 이런 행을 쓸 수
                      없어(늘 `r` 을 적는다) zip 안 XML 에 손으로 끼운다.
    """
    import re
    import zipfile

    base = Path(path).with_suffix(".base.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "현황"
    ws["A1"] = "구 분"
    ws["B1"] = "금액"
    ws["A2"] = "대출잔액"
    ws["B2"] = 82538.6
    ws["A3"] = "보증"
    ws["B3"] = 9747.4
    if comment_at:
        ws[f"A{comment_at}"].comment = openpyxl.comments.Comment("아래쪽 메모", "시험")
    if merge_to:
        ws.merge_cells(f"D1:D{merge_to}")
    wb.save(base)

    rows = range(first_ghost, first_ghost + n_ghost)
    if out_of_order:
        rows = reversed(list(rows))
    ghost = "".join(
        f'<row r="{r}"><c r="A{r}" s="0"/><c r="B{r}" s="0"/></row>' for r in rows
    )
    # 셀에도 `r` 을 안 적는다 — 실물에서 위치로만 적히는 행은 셀 주소도 위치로 적힌다.
    unnum = (f'<row><c t="inlineStr"><is><t>{unnumbered_text}</t></is></c></row>'
             if unnumbered_text else "")
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                s = data.decode("utf-8")
                s = s.replace("</sheetData>", unnum + ghost + "</sheetData>")
                s = re.sub(r'<dimension ref="[^"]*"/>',
                           f'<dimension ref="A1:B{first_ghost + n_ghost - 1}"/>', s)
                data = s.encode("utf-8")
            zout.writestr(item, data)
    base.unlink()


print("[1/45] 표시 서식이 있는 셀은 화면 값으로 렌더된다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_book(p)
    wb = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb["산정내역"]["B2"])
    if text == "1,520" and known:
        ok("1520.4 + '#,##0' → '1,520'")
    else:
        bad(f"1520.4 + '#,##0' → {text!r} (해석됨={known}) — '1,520' 이어야 합니다")
    text, known = render_value(wb["산정내역"]["C2"])
    if text == "12.5%" and known:
        ok("0.125 + '0.0%' → '12.5%'")
    else:
        bad(f"0.125 + '0.0%' → {text!r} (해석됨={known})")

print("[2/45] 모르는 서식은 원값을 쓰고 '해석 못 함' 으로 센다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 7.25
    # 과학적 표기 — 장식(대괄호·밑줄·별표·따옴표·백슬래시)을 걷어내도 E+00 이 남아
    # #,##0 류 어느 패턴과도 안 맞는다. (원래 여기 쓰던 '[$-412]#,##0.00_);...'
    # 는 라운드 2 수정으로 [$-412] 장식을 벗기면 '#,##0.00' 이 되어 이제는 정말
    # 해석되는 서식이다 — 그래서 진짜로 못 알아보는 서식으로 바꿨다.)
    wb.active["A1"].number_format = "0.00E+00"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if not known and "7.25" in text:
        ok("모르는 서식 → 원값 + 해석 못 함")
    else:
        bad(f"모르는 서식 → {text!r} (해석됨={known}) — 원값이고 해석 못 함이어야 합니다")

print("[3/45] 숨긴 행은 표에 없다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_book(p)
    wb = openpyxl.load_workbook(p, data_only=True)
    html = sheet_to_table(wb["이자계산"])
    if "보이는 행" in html and "숨긴 행" not in html:
        ok("숨긴 행이 빠졌다")
    else:
        bad(f"숨긴 행 처리가 틀렸습니다: {html}")

print("[4/45] 병합셀은 colspan/rowspan 으로 나온다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_book(p)
    wb = openpyxl.load_workbook(p, data_only=True)
    html = sheet_to_table(wb["산정내역"])
    if 'rowspan="2"' in html:
        ok("A1:A2 → rowspan=\"2\"")
    else:
        bad(f"병합이 반영되지 않았습니다: {html}")

print("[5/45] 시트마다 블록 하나 · 헤더가 계약 모양이다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_book(p)
    blocks, meta = build_blocks(p, "2026-08-05", "260805_시험.xlsx", max_rows=100)
    names = [b["name"] for b in blocks]
    if names == ["산정내역", "이자계산", "backdata"]:
        ok(f"블록 3개 · 순서 그대로 · 숨긴 시트 제외: {names}")
    else:
        bad(f"블록이 {names} 입니다 — ['산정내역','이자계산','backdata'] 여야 합니다")
    want = "**2026-08-05 · 260805_시험.xlsx — 시트 2/3: 이자계산**"
    if blocks[1]["header"] == want:
        ok("헤더가 계약 모양이다")
    else:
        bad(f"헤더가 {blocks[1]['header']!r} 입니다 — {want!r} 여야 합니다")
    if meta["hidden_sheets"] == ["폐기안"]:
        ok("숨긴 시트가 메타에 기록됐다")
    else:
        bad(f"meta['hidden_sheets'] = {meta['hidden_sheets']}")
    # 숨긴 행 2개(3행 내용 있음 · 4행 빈 줄) 중 내용 있는 것은 1개다.
    # 두 숫자가 같으면 세는 코드가 빈 줄을 안 가르고 있다는 뜻이다.
    if (meta["hidden_rows"], meta["hidden_rows_with_content"]) == (2, 1):
        ok("숨긴 행 2 · 내용 있는 것 1")
    else:
        bad(f"숨긴 행 {meta['hidden_rows']} · 내용 있는 것 {meta['hidden_rows_with_content']} — (2, 1) 이어야 합니다")

print("[6/45] 상한을 넘는 시트는 빠지고 메타에 남는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_book(p)
    blocks, meta = build_blocks(p, "2026-08-05", "260805_시험.xlsx", max_rows=5)
    names = [b["name"] for b in blocks]
    if "backdata" not in names and meta["skipped_sheets"] == [{"name": "backdata", "rows": 11}]:
        ok("11행 시트가 상한 5에서 빠지고 메타에 남았다")
    else:
        bad(f"names={names} skipped={meta['skipped_sheets']}")

print("[7/45] 수식인데 캐시값이 없으면 변환을 세운다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.active["A2"] = "=A1+1"
    wb.save(p)
    try:
        build_blocks(p, "2026-08-05", "t.xlsx", max_rows=100)
        bad("캐시값 없는 수식이 있는데 그냥 통과했습니다")
    except EmptyFormulaError as e:
        if "1" in str(e):
            ok(f"세웠다: {e}")
        else:
            bad(f"세웠지만 개수가 안 적혔습니다: {e}")

print("[8/45] 계산 결과가 **빈 문자열**인 수식은 「캐시값 없음」이 아니다")
# `IF(A="","",…)` 나 빈 칸을 찾아온 VLOOKUP 은 정당하게 빈 문자열을 낸다. 엑셀은 그것을
# `t="str"` 에 `<v/>` 로 담는데 openpyxl 은 `<v/>` 도 캐시가 아예 없는 것도 똑같이
# None 으로 돌려준다. 값 하나로 판정하면 멀쩡한 파일이 통째로 막힌다 —
# 2026-08-25 실측으로 8건 364셀이 전부 이 경우였다(전부 거짓 양성).
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "base.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.active["A2"] = '=IF(A1=1,"","x")'
    wb.save(src)

    # openpyxl 은 `<c r="A2"><f>…</f><v /></c>` 로 쓴다 — 빈 <v> 는 이미 있고 t 가 없다.
    # 엑셀이 「계산된 빈 문자열」을 담는 모양은 거기에 t="str" 이 붙은 것이다.
    import re as _re
    import zipfile as _zip
    p = Path(td) / "t.xlsx"
    with _zip.ZipFile(src) as zin:          # 윈도우에서는 닫아야 임시 폴더가 지워진다
        target = next(n for n in zin.namelist()
                      if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        xml = zin.read(target).decode("utf-8")
        patched, cnt = _re.subn(
            r'(<c r="A2")([^>]*>)(<f>[^<]*</f><v ?/>)',
            r'\1 t="str"\2\3', xml, count=1)
        items = [(i.filename, patched.encode("utf-8") if i.filename == target
                  else zin.read(i.filename)) for i in zin.infolist()]
    if cnt != 1:
        bad(f"시험 파일을 못 만들었습니다 — A2 수식 셀을 못 찾음 (cnt={cnt})")
    else:
        with _zip.ZipFile(p, "w", _zip.ZIP_DEFLATED) as zo:
            for name, data in items:
                zo.writestr(name, data)
        try:
            build_blocks(p, "2026-08-05", "t.xlsx", max_rows=100)
            ok("빈 문자열 결과는 캐시값 없음으로 안 본다")
        except EmptyFormulaError as e:
            bad(f"멀쩡한 파일을 세웠습니다 (거짓 양성): {e}")

print("[9/45] yyyy-mm-dd 서식의 날짜 셀은 화면 그대로 렌더된다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = dt.datetime(2026, 4, 21)
    wb.active["A1"].number_format = "yyyy-mm-dd"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "2026-04-21" and known:
        ok("yyyy-mm-dd → '2026-04-21'")
    else:
        bad(f"yyyy-mm-dd → {text!r} (해석됨={known}) — '2026-04-21' 이어야 합니다")

print("[10/45] yyyy/mm/dd(aaa) 서식은 한글 요일까지 화면 그대로 렌더된다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    d = dt.datetime(2026, 4, 21)
    wb.active["A1"] = d
    wb.active["A1"].number_format = "yyyy/mm/dd(aaa)"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    weekday = "월화수목금토일"[d.weekday()]  # datetime.weekday(): 월=0 ... 일=6
    want = f"2026/04/21({weekday})"
    if text == want and known:
        ok(f"yyyy/mm/dd(aaa) → {want!r}")
    else:
        bad(f"yyyy/mm/dd(aaa) → {text!r} (해석됨={known}) — {want!r} 이어야 합니다")

print("[11/45] 지원하지 않는 날짜 서식은 원값 + '해석 못 함' 으로 센다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = dt.datetime(2026, 4, 21)
    wb.active["A1"].number_format = "dddd, mmmm d, yyyy"  # 영문 요일 전체 표기 — 일부러 안 다룸
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if not known:
        ok(f"모르는 날짜 서식 → 해석 못 함: {text!r}")
    else:
        bad(f"모르는 날짜 서식인데 해석됨={known} (text={text!r}) — known=False 여야 합니다")

print("[12/45] 은행식 회계 서식(_-* 계열)도 화면 값으로 렌더된다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1520.4
    wb.active["A1"].number_format = '_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-'
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "1,520" and known:
        ok("회계 서식(_-* 계열) → '1,520'")
    else:
        bad(f"회계 서식 → {text!r} (해석됨={known}) — '1,520' 이어야 합니다")

print("[13/45] 괄호 음수 회계 서식(#,##0_) 계열)도 화면 값으로 렌더된다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1520.4
    wb.active["A1"].number_format = "#,##0_);\\(#,##0\\)"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "1,520" and known:
        ok("괄호 회계 서식 → '1,520'")
    else:
        bad(f"괄호 회계 서식 → {text!r} (해석됨={known}) — '1,520' 이어야 합니다")

print("[14/45] 숫자 뒤 따옴표 리터럴은 그 자리에 그대로 남는다 (1,520억)")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1520.4
    wb.active["A1"].number_format = '#,##0"억"'
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "1,520억" and known:
        ok("#,##0\"억\" → '1,520억'")
    else:
        bad(f"#,##0\"억\" → {text!r} (해석됨={known}) — '1,520억' 이어야 합니다")

print("[15/45] 이스케이프한 통화 기호(₩)는 앞에 그대로 남는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1520.4
    wb.active["A1"].number_format = "\\₩#,##0"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "₩1,520" and known:
        ok("\\₩#,##0 → '₩1,520'")
    else:
        bad(f"\\₩#,##0 → {text!r} (해석됨={known}) — '₩1,520' 이어야 합니다")

print("[16/45] $ 도 ₩ 와 같은 대우 — 이스케이프 없이도 그대로 남는다")
# ₩ 는 \로 이스케이프해야 서식 문법에서 살아남지만, $ 는 엑셀이 이스케이프 없이도
# 그대로 찍어 주는 몇 안 되는 리터럴 문자다(+·-·(·)·:·공백·숫자 등과 같은 부류).
# 결정: 두 통화 기호를 똑같이 "그 자리에 남긴다" 쪽으로 맞춘다 — 우선순위 1번
# (리터럴 보존)을 항상 먼저 시도하고, 안 되면만 known=False 로 떨어지는 게
# 이 라운드의 원칙이라 통화 기호를 굳이 다르게 취급할 이유가 없다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1520.4
    wb.active["A1"].number_format = "$#,##0.00"
    wb.save(p)
    wb2 = openpyxl.load_workbook(p, data_only=True)
    text, known = render_value(wb2.active["A1"])
    if text == "$1,520.40" and known:
        ok("$#,##0.00 → '$1,520.40' (₩ 와 같은 처리)")
    else:
        bad(f"$#,##0.00 → {text!r} (해석됨={known}) — '$1,520.40' 이어야 합니다")

print("[17/45] 리터럴을 믿고 놓을 수 없는 서식은 known=False 로 세어진다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "시트1"
    # 숫자 자리표시자 뭉치가 둘로 갈라지는 서식 — 어느 쪽에 리터럴을 붙일지
    # 신뢰할 수 없어 known=False 로 떨어져야 한다(분수 서식류와 같은 처리).
    ws["A1"] = 1520.4
    ws["A1"].number_format = '#,##0"m"#,##0'
    wb.save(p)
    text, known = render_value(openpyxl.load_workbook(p, data_only=True).active["A1"])
    if known:
        bad(f"자리표시자 뭉치가 둘인 서식인데 해석됨={known} (text={text!r}) — known=False 여야 합니다")
    else:
        ok(f"자리표시자 뭉치가 둘인 서식 → 해석 못 함: {text!r}")
    blocks, meta = build_blocks(p, "2026-08-05", "t.xlsx", max_rows=100)
    if meta["unformatted_cells"] == 1:
        ok("meta['unformatted_cells'] == 1 — sheet_to_table 경로에서도 세어졌다")
    else:
        bad(f"meta['unformatted_cells'] = {meta['unformatted_cells']} — 1 이어야 합니다")

print("[18/45] 훑기 시험 — 서식표 전부가 정확히 둘 중 하나로만 떨어진다")
# (서식, 값, 기대 표시문자열 또는 None) — None 이면 known=False 만 요구하고
# 텍스트는 안 본다. 세 번째 결과(엉뚱한 텍스트 + known=True 조합 등)는 절대
# 없어야 한다는 게 이 시험의 요점이다.
SWEEP_CASES = [
    ("#,##0", 1520.4, "1,520"),
    ("#,##0.0", 1520.4, "1,520.4"),
    ("0.0%", 0.125, "12.5%"),
    ('0.0"%"', 12.5, "12.5%"),  # 곱하지 말고 % 글자만 — Finding 4 의 회귀 지점
    ('#,##0"억"', 1520.4, "1,520억"),
    ('#,##0"00"', 1520.4, "1,52000"),  # 리터럴 "00" 이 자리표시자에 안 먹힌다
    ('0"개"', 7, "7개"),
    ('"합계 "#,##0', 1520.4, "합계 1,520"),
    ("\\₩#,##0", 1520.4, "₩1,520"),
    ("$#,##0.00", 1520.4, "$1,520.40"),
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', 1520.4, "1,520"),
    ("#,##0_);\\(#,##0\\)", 1520.4, "1,520"),
    ("0.00E+00", 1520.4, None),  # 과학적 표기 — 미지원
    ("#,##0,", 1520400, None),  # 끝 콤마는 1000 단위 축약 — 배율 미구현이라 모른다
    ('#,##0,,"백만"', 1520400, None),  # 콤마 둘(백만 단위 축약) + 리터럴 — 역시 모른다
    # 서식에 이미 사용자 영역 문자가 들어 있는 경우. 리터럴을 잠시 빼 둘 때 쓰는
    # 자리표시자와 같은 영역이라, 막지 않으면 되돌릴 때 '단위단위1,520' 이 된다.
    # 엑셀 UI 로는 만들 수 없는 서식이지만 자리표시자 방식이 스스로 낸 구멍이라 막는다.
    ('"단위"' + chr(0xE000) + "#,##0", 1520.4, None),
    ("@", "abc", "abc"),  # 문자열 셀은 서식과 무관하게 원문 그대로
    # ── 반올림 방향 (파이썬은 짝수 쪽, 엑셀은 0에서 먼 쪽) ──
    ("#,##0", 20336.5, "20,337"),  # 파이썬 f-string 은 20,336
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', 1520.5, "1,521"),  # 실물에서 가장 많은 서식
    ("#,##0.0", 0.25, "0.3"),  # f-string 은 0.2
    ("#,##0", -2.5, "-3"),  # 음수도 0에서 먼 쪽 (f-string 은 -2)
    ("0%", 0.125, "13%"),  # 100을 곱한 뒤 반올림 — f-string 은 12%
    # ── [$…] 안의 통화 기호는 화면에 찍힌다 ──
    ("[$$-409]#,##0.00", 1520.4, "$1,520.40"),
    ("[$₩-412]#,##0", 1520.4, "₩1,520"),
    ("[$€-2]#,##0.00", 1520.4, "€1,520.40"),
    ("[$-412]#,##0", 1520.4, "1,520"),  # 기호 자리가 비었으면 그건 진짜 안 보인다
    ("[Red]#,##0", 1520.4, "1,520"),  # 색은 여전히 안 보인다
    # ── 조건 대괄호는 구역을 고르라는 뜻이라 해석하지 않는다 ──
    ('[<0]"손실 "#,##0;#,##0', 1520, None),  # 지우면 양수에 '손실 1,520' 이 찍힌다
    ('[>=1000]#,##0"천";#,##0', 5, None),
    # ── 구역(;) 가르기는 리터럴 밖에서만 ──
    ('#,##0"원;부가세별도"', 1520.4, "1,520원;부가세별도"),
    # ── 구역은 값의 부호로 고른다 (양수 1 · 음수 2 · 0 3 · 문자 4) ──
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', -1520.4, "-1,520"),
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', 0, "-"),  # 0 구역엔 숫자 자리가 없다
    ("#,##0_);\\(#,##0\\)", -1520.4, "(1,520)"),  # 음수 구역은 절댓값에 적용
    ("#,##0_);\\(#,##0\\)", 0, "0"),  # 구역이 둘뿐이면 0 은 1번을 쓴다
    ("0.0%;(0.0%)", -0.125, "(12.5%)"),
    ("#,##0;#,##0", -1520.4, "1,520"),  # 엑셀이 일부러 부호를 감춘 서식
    ("#,##0;;", -1520.4, ""),  # 빈 구역 = 아무것도 안 보인다
    ("#,##0;;", 0, ""),
    ("#,##0", -1520.4, "-1,520"),  # 구역이 하나면 빼기 기호가 자동으로 붙는다
    # ── 한자·한글 숫자 치환은 구현하지 않는다 (안 보이는 장식이 아니다) ──
    ("[DBNum1]#,##0", 1520.4, None),
    ("[DBNum4]#,##0", 1520.4, None),
    # ── 문자 구역(네 번째)이 글자를 감싼다 ──
    ('#,##0;;;"[["@"]]"', "본문", "[[본문]]"),
]

sweep_failed = 0
with tempfile.TemporaryDirectory() as td:
    for i, (fmt, value, expected) in enumerate(SWEEP_CASES):
        p = Path(td) / f"sweep{i}.xlsx"
        wb = openpyxl.Workbook()
        wb.active["A1"] = value
        wb.active["A1"].number_format = fmt
        wb.save(p)
        text, known = render_value(openpyxl.load_workbook(p, data_only=True).active["A1"])
        if expected is None:
            row_ok = not known
            detail = f"known={known} (text={text!r}) — known=False 여야 합니다"
        else:
            row_ok = (text == expected) and known
            detail = f"{text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다"
        if row_ok:
            ok(f"{fmt!r} on {value!r} → {text!r} (해석됨={known})")
        else:
            sweep_failed += 1
            bad(f"{fmt!r} on {value!r} → {detail}")

if sweep_failed == 0:
    ok(f"훑기 {len(SWEEP_CASES)}행 전부 정확히 통과")
else:
    bad(f"훑기 {sweep_failed}/{len(SWEEP_CASES)}행 실패")


class StubCell:
    """openpyxl 로는 만들어 낼 수 없는 값까지 훑기 위한 최소 셀. render_value 는
    `.value` 와 `.number_format` 만 본다.

    왜 필요한가: ① 파이썬 datetime.date 를 저장하면 openpyxl 은 읽을 때
    datetime.datetime 으로 바꿔 돌려주므로 순수 date 는 파일을 거쳐서는 시험할 수
    없고, ② [19/32] 가 요구하는 「어떤 분기도 못 알아보는 값」은 애초에 엑셀 파일에
    담을 수가 없다."""

    def __init__(self, value, number_format="General"):
        self.value = value
        self.number_format = number_format


print("[19/45] 값 형 훑기 — 값의 형(type)마다 정확히 둘 중 하나로만 떨어진다")
# [17/32] 이 「서식」을 훑는다면 이쪽은 「값의 형」을 훑는다. 다섯 번 되풀이된 결함이
# 전부 이 축에서 샜다 — 서식은 멀쩡한데 openpyxl 이 돌려준 형을 아무도 안 봐서
# 파이썬 str() 이 그대로 나가고 known=True 가 찍혔다(경과시간·참거짓이 그 마지막
# 두 개였다). 각 행은 정확히 둘 중 하나여야 한다: 엑셀 화면 글자 그대로 +
# known=True, 아니면 known=False. 세 번째(엉뚱한 글자 + known=True)는 이 판정
# 구조로는 나올 수 없다. 형 이름도 함께 대조하는데, openpyxl 이 돌려주는 형이
# 언젠가 바뀌면 시험의 전제가 낡은 것이라 그 사실이 먼저 드러나야 하기 때문이다.
from openpyxl.cell.rich_text import CellRichText, TextBlock  # noqa: E402
from openpyxl.cell.text import InlineFont  # noqa: E402

type_failed = 0
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "types.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    # A1 은 일부러 안 쓴다 — 빈 셀
    ws["A2"] = "abc"
    ws["A2"].number_format = "@"
    ws["A3"] = True
    ws["A4"] = False
    ws["A5"] = 42
    ws["A6"] = 1520.4
    ws["A6"].number_format = "#,##0"
    ws["A7"] = dt.datetime(2026, 4, 21)
    ws["A7"].number_format = "yyyy-mm-dd"
    ws["A8"] = dt.time(13, 5, 9)
    ws["A9"] = dt.timedelta(days=1.5)
    ws["A9"].number_format = "[h]:mm:ss"
    # 일부러 별난 값 — 서식 있는 문자열. rich_text=True 로 열면 openpyxl 이 str 이
    # 아니라 list 의 하위 클래스인 CellRichText 를 돌려준다. 아무도 생각 못 한 형이
    # 실제로 온다는 것을 보이는 자리라, 여기서는 known=False 로 세어져야 한다.
    ws["A10"] = CellRichText([TextBlock(InlineFont(b=True), "굵게"), "보통"])
    wb.save(p)
    ws2 = openpyxl.load_workbook(p, data_only=True, rich_text=True).active

    # (설명, 셀, 기대하는 파이썬 형 이름, 기대 표시문자열 또는 None)
    TYPE_CASES = [
        ("빈 셀", ws2["A1"], "NoneType", ""),
        ("문자열", ws2["A2"], "str", "abc"),
        ("참", ws2["A3"], "bool", "TRUE"),  # 엑셀 화면은 대문자다 — 파이썬 'True' 가 아니다
        ("거짓", ws2["A4"], "bool", "FALSE"),
        ("정수", ws2["A5"], "int", "42"),
        ("실수", ws2["A6"], "float", "1,520"),
        ("날짜시각", ws2["A7"], "datetime", "2026-04-21"),
        ("시각", ws2["A8"], "time", None),
        ("경과시간", ws2["A9"], "timedelta", None),  # 결정: 렌더 안 하고 센다
        ("서식 있는 문자열(별난 값)", ws2["A10"], "CellRichText", None),
        # 파일을 거치면 datetime 이 되므로 순수 date 는 셀을 직접 만들어 본다
        ("날짜", StubCell(dt.date(2026, 4, 21), "yyyy-mm-dd"), "date", "2026-04-21"),
    ]

    for label, cell, want_type, expected in TYPE_CASES:
        got_type = type(cell.value).__name__
        if got_type != want_type:
            type_failed += 1
            bad(
                f"{label}: 값의 형이 {got_type} 입니다 — {want_type} 이어야 합니다 "
                "(시험의 전제가 낡았습니다)"
            )
            continue
        text, known = render_value(cell)
        if expected is None:
            row_ok = known is False
            detail = f"known={known} (text={text!r}) — known=False 여야 합니다"
        else:
            row_ok = (text == expected) and known is True
            detail = f"{text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다"
        if row_ok:
            ok(f"{label}({want_type}) → {text!r} (해석됨={known})")
        else:
            type_failed += 1
            bad(f"{label}({want_type}) → {detail}")

if type_failed == 0:
    ok(f"값 형 훑기 {len(TYPE_CASES)}행 전부 정확히 통과")
else:
    bad(f"값 형 훑기 {type_failed}/{len(TYPE_CASES)}행 실패")

print("[20/45] 기본값 방향 — 아무 분기도 못 알아본 값은 반드시 known=False 다")
# 이 시험 하나가 render_value 마지막 줄의 **방향**을 붙잡는다. 다섯 번 되풀이된
# 결함의 뿌리가 그 줄이 «못 알아본 것은 전부 known=True» 였던 것이라, 누가 그걸
# 다시 넓히면 여기가 빨개져야 한다. 그러려면 앞으로 어떤 분기가 추가돼도 계속
# 미지수로 남을 값이 필요해서, 이 파일 안에서만 쓰는 형을 하나 만들어 넣는다.


class NobodyKnowsThis:
    def __str__(self):
        return "알 수 없는 무엇"


text, known = render_value(StubCell(NobodyKnowsThis()))
if known is False:
    ok(f"모르는 형 → known=False (text={text!r})")
else:
    bad(
        f"모르는 형인데 해석됨={known} (text={text!r}) — render_value 의 마지막 분기가 "
        "known=True 로 다시 넓혀졌습니다. 기본값은 반드시 False 여야 합니다"
    )

print("[21/45] 반올림은 엑셀과 같은 방향 — 0에서 먼 쪽으로 올린다")
# 파이썬 f-string 은 「짝수 쪽으로」 반올림해서 20336.5 를 20,336 으로 찍고,
# 엑셀은 20,337 로 찍는다. .5 는 이진수로 정확히 표현되므로 이 차이는 부동소수점
# 잡음이 아니라 늘 같게 나오는 결정적 차이다. 실물 32개 파일에서 494셀이 이 하나로
# 어긋났고 전부 known=True 로 나갔다.
ROUND_CASES = [
    ("#,##0", 20336.5, "20,337"),
    ("#,##0", 2.5, "3"),
    ("#,##0", 3.5, "4"),  # 짝수 반올림이면 4 로 같다 — 반쪽만 고쳐도 여기는 초록이다
    ("#,##0", -20336.5, "-20,337"),
    ("#,##0.0", 0.25, "0.3"),
    ("#,##0.00", 1.005, "1.01"),  # str(v) 를 거쳐야 맞는다 (이진수 원값은 1.00499…)
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', 1520.5, "1,521"),
    ("0.0%", 0.1225, "12.3%"),  # 곱한 뒤 반올림
    ("0%", 0.125, "13%"),
]
round_failed = 0
for fmt, value, expected in ROUND_CASES:
    text, known = render_value(StubCell(value, fmt))
    if text == expected and known is True:
        ok(f"{fmt!r} on {value!r} → {text!r}")
    else:
        round_failed += 1
        bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다")
if round_failed == 0:
    ok(f"반올림 {len(ROUND_CASES)}행 전부 통과")

print("[22/45] [$…] 안의 통화 기호는 지우지 않고 제자리에 남긴다")
# `[$-412]` 는 지역 표시라 화면에 안 보이지만, `[$$-409]`·`[$₩-412]` 의 `[$` 와 `-`
# 사이 글자는 화면에 **찍힌다**. 지우고 known=True 를 달면 엑셀이 `$1,520.40` 을
# 보여주는 자리에 `1,520.40` 이 「확인된 값」으로 나간다 — 라운드 2 에서 `억` 을
# 지웠던 것과 같은 결함이다.
for fmt, expected in [
    ("[$$-409]#,##0.00", "$1,520.40"),
    ("[$₩-412]#,##0", "₩1,520"),
    ("[$-412]#,##0", "1,520"),
]:
    text, known = render_value(StubCell(1520.4, fmt))
    if text == expected and known is True:
        ok(f"{fmt!r} → {text!r}")
    else:
        bad(f"{fmt!r} → {text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다")

print("[23/45] 조건 대괄호가 있는 서식은 해석하지 않는다")
# `[<0]"손실 "#,##0;#,##0` 은 「음수면 첫 구역」이라는 뜻인데 이 변환기는 늘 첫 구역만
# 쓴다. 조건을 장식으로 보고 지우면 **양수 1520 에 '손실 1,520'** 이 찍힌다.
for fmt, value in [('[<0]"손실 "#,##0;#,##0', 1520), ('[>=1000]#,##0"천";#,##0', 5)]:
    text, known = render_value(StubCell(value, fmt))
    if known is False:
        ok(f"{fmt!r} on {value!r} → 해석 못 함: {text!r}")
    else:
        bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — known=False 여야 합니다")

print("[24/45] 해석 못 한 값의 원값은 자리수가 안 잘린다")
# 이 자리는 참값을 남기려고 있는 곳인데 예전 `f"{v:g}"` 는 유효숫자 6자리에서 끊어
# 39660821185.75(실측에서 실제로 나온 값)를 3.96608e+10 으로 바꿨다.
text, known = render_value(StubCell(39660821185.75, "0.00E+00"))
if known is False and text == "39660821185.75":
    ok(f"39660821185.75 + 모르는 서식 → {text!r}")
else:
    bad(f"→ {text!r} (해석됨={known}) — '39660821185.75' · known=False 여야 합니다")

print("[25/45] 구역(;) 은 리터럴 밖에서만 가른다")
# `split(";")[0]` 은 따옴표 안의 세미콜론까지 구분자로 읽어 첫 구역을 `#,##0"원` 으로
# 만들었고, 그 결과 `1,520"원` 이 known=True 로 나갔다.
text, known = render_value(StubCell(1520.4, '#,##0"원;부가세별도"'))
if text == "1,520원;부가세별도" and known is True:
    ok(f'#,##0"원;부가세별도" → {text!r}')
else:
    bad(f"→ {text!r} (해석됨={known}) — '1,520원;부가세별도' · known=True 여야 합니다")

print("[26/45] 날짜도 리터럴을 먼저 들어낸 뒤 토큰을 훑는다")
# 숫자 쪽은 라운드 3 에서 순서를 고쳤는데 날짜 경로만 그 수정이 안 닿아 있었다.
# 따옴표를 먼저 벗기면 리터럴 속 `dd` 가 진짜 자리표시자와 안 갈려 21 로 치환된다.
for fmt, expected in [
    ('yyyy-mm-dd"(dd)"', "2026-04-21(dd)"),
    ('yyyy"년" mm"월" dd"일"', "2026년 04월 21일"),
    ("yyyy-mm-dd", "2026-04-21"),
]:
    text, known = render_value(StubCell(dt.datetime(2026, 4, 21), fmt))
    if text == expected and known is True:
        ok(f"{fmt!r} → {text!r}")
    else:
        bad(f"{fmt!r} → {text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다")

print("[27/45] 구역은 값의 부호로 고른다 — 양수 1 · 음수 2 · 0 3 · 문자 4")
# 일곱 번째 사례. 조건 대괄호(`[<0]`)를 막을 때 **조건을 안 쓴 쪽**을 그대로 두었는데
# 그쪽이 훨씬 흔했다 — 회계 서식의 0 과 괄호 음수는 실측에서 각각 5만·1만 셀대다.
# 세 값(양수·음수·0)을 한 서식에 함께 걸어 「1번 구역만 쓴다」가 못 지나가게 한다.
SECTION_CASES = [
    # (서식, 양수, 음수, 0)
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', "1,520", "-1,520", "-"),
    ('_-* #,##0.00_-;-* #,##0.00_-;_-* "-"??_-;_-@_-', "1,520.40", "-1,520.40", "-"),
    ("#,##0_);\\(#,##0\\)", "1,520", "(1,520)", "0"),  # 구역이 둘 — 0 은 1번으로
    ("#,##0;#,##0", "1,520", "1,520", "0"),  # 부호를 일부러 감춘 서식
    ("#,##0;;", "1,520", "", ""),  # 빈 구역은 화면도 빈칸이다
    ("#,##0", "1,520", "-1,520", "0"),  # 구역이 하나면 빼기 기호가 자동으로 붙는다
]
section_failed = 0
for fmt, want_pos, want_neg, want_zero in SECTION_CASES:
    for value, want in ((1520.4, want_pos), (-1520.4, want_neg), (0, want_zero)):
        text, known = render_value(StubCell(value, fmt))
        if text == want and known is True:
            ok(f"{fmt!r} on {value!r} → {text!r}")
        else:
            section_failed += 1
            bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — {want!r} · known=True 여야 합니다")

# 퍼센트는 곱하기까지 함께 걸린다 — 음수 구역을 골라도 절댓값에 100을 곱해야 한다.
for value, want in ((0.125, "12.5%"), (-0.125, "(12.5%)"), (0, "0.0%")):
    text, known = render_value(StubCell(value, "0.0%;(0.0%)"))
    if text == want and known is True:
        ok(f"'0.0%;(0.0%)' on {value!r} → {text!r}")
    else:
        section_failed += 1
        bad(f"'0.0%;(0.0%)' on {value!r} → {text!r} (해석됨={known}) — {want!r} 여야 합니다")

# 조건이 붙으면 고르는 규칙 자체가 부호가 아니게 된다 — **어느 구역에 있든** 해석 안 한다.
# 두 번째 줄이 요점이다: 조건이 2번 구역에 있고 값이 양수면, 고른 구역(1번)만 봐서는
# 조건이 안 보인다. 그래도 엑셀은 그 조건으로 구역을 고르므로 우리 판정은 못 믿는다.
for fmt, value in [
    ('[<0]"손실 "#,##0;#,##0', 1520),
    ('[<0]"손실 "#,##0;#,##0', -1520),
    ("#,##0;[Red][<-1000]\\(#,##0\\)", 1520),  # 조건이 2번 구역 · 값은 양수
    ("#,##0;[Red][<-1000]\\(#,##0\\)", -1520),
]:
    text, known = render_value(StubCell(value, fmt))
    if known is False:
        ok(f"조건이 붙은 서식 {fmt!r} on {value!r} → 해석 못 함: {text!r}")
    else:
        section_failed += 1
        bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — known=False 여야 합니다")

# 한자·한글 숫자 치환은 안 보이는 장식이 아니다 (1,520 이 아니라 一千五百二十).
for fmt in ("[DBNum1]#,##0", "[DBNum4]#,##0", "[dbnum2]#,##0"):
    text, known = render_value(StubCell(1520.4, fmt))
    if known is False:
        ok(f"{fmt!r} → 해석 못 함: {text!r}")
    else:
        section_failed += 1
        bad(f"{fmt!r} → {text!r} (해석됨={known}) — known=False 여야 합니다")

# 네 번째 구역(문자)이 글자를 감싼다. 구역이 없으면 예전대로 글자 그대로다.
for fmt, value, want, want_known in [
    ('#,##0;;;"[["@"]]"', "본문", "[[본문]]", True),
    ('_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', "합계", "합계", True),  # `_-@_-` → 글자 그대로
    ("#,##0", "본문", "본문", True),  # 문자 구역이 없다
    ('#,##0;;;', "본문", "", True),  # 문자를 안 보이게 한 것
    ('#,##0;;;"머리"', "본문", "본문", False),  # @ 가 없다 — 어디 놓을지 모른다
]:
    text, known = render_value(StubCell(value, fmt))
    if text == want and known is want_known:
        ok(f"{fmt!r} on {value!r} → {text!r} (해석됨={known})")
    else:
        section_failed += 1
        bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — {want!r} · known={want_known} 여야 합니다")

if section_failed == 0:
    ok("구역 고르기 전부 통과")

print("[28/45] General 은 엑셀 화면 글자로 찍는다 — 파이썬 repr 이 새면 안 된다")
# 여덟 번째 사례. Finding 9 가 known=False 쪽 원값 표기를 `f"{v:g}"` → `str(v)` 로
# 고친 것은 맞았는데, **같은 식이 known=True 쪽에도 물려 있었다.** 두 자리는 계약이
# 정반대다 — 하나는 저장된 참값을 안 잘리게 남기는 자리, 하나는 엑셀 화면 글자를
# 내는 자리다. 그래서 `1520.4 * 3` 이 `4561.200000000001` 로, 1e-05 가 `1e-05` 로
# 나갔고 전부 known=True 라 세어지지도 않았다. 서식을 안 준 셀은 전부 General 이다.
#
# 평문/지수 경계는 추론하지 않고 LibreOffice 로 쟀다(「보이는 대로」 CSV 내보내기).
# 십진 지수 -9~14 는 언제나 평문, -10 이하와 16 이상은 언제나 지수 표기,
# **15 는 갈린다**(1e15~9e15 평문 · 9.9e15 부터 지수) — 그래서 15 는 known=False 다.
GENERAL_CASES = [
    # (값, 기대 표시문자열 또는 None(=known=False))
    (0.30000000000000004, "0.3"),
    (0.21000000000000002, "0.21"),
    (4561.200000000001, "4561.2"),  # 1520.4 × 3 — 산정 내역에 흔한 계산 결과다
    (0.3333333333333333, "0.333333333333333"),  # 유효숫자 15자리
    (1e-05, None),  # 지수 -5 — 아래 「경계는 면이 둘」 참조
    (1e16, None),  # LibreOffice 는 1E+016, 엑셀은 1E+16 — 글자를 확정할 수 없다
    (123456789012345.67, "123456789012346"),
    (2 / 3, "0.666666666666667"),
    (0, "0"),
    (-0.0, "0"),  # 화면은 0 이다
    (42, "42"),
    (-1234.5, "-1234.5"),
    # ── 잰 경계 (아래쪽은 면이 둘이다) ──
    # 지수만 훑으면 -9 까지 평문으로 보이는데, 지수 × 유효숫자 개수로 격자를 만들어
    # 재면 15자리까지 평문인 것은 **지수 -4 까지**다. 그 아래로는 자릿수가 줄어든다
    # (-5 는 12자리 · -6 은 11 · -7 은 10 · -8 은 9 · -9 는 8자리까지). 규칙을 하나
    # 더 붙이지 않고 안전한 끝(-4)을 쓰기로 했으므로 -5 아래는 전부 센다.
    (0.0001, "0.0001"),  # 지수 -4 — 평문 구간의 아래 끝
    (0.000123456789012345, "0.000123456789012345"),  # 지수 -4 · 유효숫자 15자리도 평문
    (1.2345678901234e-05, None),  # 연이율 ÷ 365 같은 값. 화면은 1.2345678901234E-05
    (1e-9, None),
    (1e-10, None),
    (99999999999999.9, "99999999999999.9"),  # 지수 13
    (1e15, None),  # 지수 15 — 갈리는 구간이라 안 그린다
    (9.9e15, None),
    # 15번째 자리가 아슬아슬한 값 — LibreOffice 는 16자리를 거쳐 줄여(이중 반올림)
    # `…984` 를 찍고, 이진수 원값에서 곧바로 줄이면 `…983` 이다. 어느 쪽이 엑셀인지
    # 잴 수단이 없어 그리지 않고 센다. 실물에 가까운 값 3,100개에서 1.7%였다.
    (1256.5289256198348, None),
    (29560868258.391247, None),
]
general_failed = 0
for value, expected in GENERAL_CASES:
    text, known = render_value(StubCell(value, "General"))
    if expected is None:
        row_ok = known is False
        detail = f"known={known} (text={text!r}) — known=False 여야 합니다"
    else:
        row_ok = (text == expected) and known is True
        detail = f"{text!r} (해석됨={known}) — {expected!r} · known=True 여야 합니다"
    if row_ok:
        ok(f"General on {value!r} → {text!r} (해석됨={known})")
    else:
        general_failed += 1
        bad(f"General on {value!r} → {detail}")

# `@` 는 숫자에 걸리면 General 과 같은 대우다.
text, known = render_value(StubCell(4561.200000000001, "@"))
if text == "4561.2" and known is True:
    ok("'@' 에 걸린 숫자도 General 과 같다")
else:
    general_failed += 1
    bad(f"'@' on 4561.200000000001 → {text!r} (해석됨={known}) — '4561.2' 여야 합니다")

# **Finding 9 의 보증**: known=False 자리는 여전히 저장된 참값을 한 자리도 안 자른다.
# 여기가 무너지면 「해석 못 했다」고 세면서 원값까지 잃는다 — 두 계약이 다시 한
# 식으로 합쳐졌다는 뜻이다.
for value, fmt in [(39660821185.75, "0.00E+00"), (1e16, "General"), (1e-05, "General")]:
    text, known = render_value(StubCell(value, fmt))
    if known is False and text == str(value):
        ok(f"known=False 는 원값 그대로: {value!r} → {text!r}")
    else:
        general_failed += 1
        bad(f"{value!r} + {fmt!r} → {text!r} (해석됨={known}) — {str(value)!r} · known=False 여야 합니다")

if general_failed == 0:
    ok("General 훑기 전부 통과")

print("[29/45] 서식 글자 자체가 셀 값으로 나가면 안 된다")
# 아홉 번째 사례이자 **구역 고르기가 만든 후퇴**다. 예전에는 구역을 원문과 글자
# 그대로 견줘 General 인지 봤는데, `[Red]General` 처럼 안 보이는 대괄호가 앞에
# 붙으면 그 비교가 어긋나 「리터럴만인 구역」으로 떨어졌다 — 그러면 서식 글자
# `General` 이 그대로 셀 값이 됐고 known=True 였다. 봇은 "그 값은 General 입니다"
# 라고 답한다. 구역 고르기 전(173aa72)에는 known=False 로 세어지던 자리다.
TOKEN_LEAK_CASES = [
    ("[Red]General", "1520.4", True),
    ("[Blue]General", "1520.4", True),
    ("[Red]@", "1520.4", True),
    ("[Blue]@", "1520.4", True),
    ("[Red]General;[Blue]General", "1520.4", True),  # 구역 둘 다 색이 붙은 General
    ("General", "1520.4", True),  # 원래 되던 것이 그대로 되나
    # 되돌린 장식이 리터럴이 아니라 서식 토큰이면 세는 쪽으로. `$General` 이
    # 셀 값으로 나가면 안 된다 (같은 결함의 다른 얼굴).
    ("[$$-409]General", None, False),
]
leak_failed = 0
for fmt, want, want_known in TOKEN_LEAK_CASES:
    text, known = render_value(StubCell(1520.4, fmt))
    row_ok = (known is want_known) and (want is None or text == want)
    # 어떤 경우에도 서식 토큰이 값으로 나가서는 안 된다
    if known and text in ("General", "@", "$General"):
        row_ok = False
    if row_ok:
        ok(f"{fmt!r} → {text!r} (해석됨={known})")
    else:
        leak_failed += 1
        bad(f"{fmt!r} → {text!r} (해석됨={known}) — "
            f"{want!r} · known={want_known} 여야 합니다 (서식 글자가 값으로 새면 안 됩니다)")

# 참거짓도 서식을 보고 판정한다 — `;;;` 은 화면을 빈칸으로 만드는 서식이다.
for fmt, want_known in [("General", True), ("@", True), (";;;", False), ("#,##0", False)]:
    text, known = render_value(StubCell(True, fmt))
    if text == "TRUE" and known is want_known:
        ok(f"참거짓 + {fmt!r} → {text!r} (해석됨={known})")
    else:
        leak_failed += 1
        bad(f"참거짓 + {fmt!r} → {text!r} (해석됨={known}) — 'TRUE' · known={want_known} 여야 합니다")

if leak_failed == 0:
    ok("서식 토큰 누출 훑기 전부 통과")

print("[30/45] 실물 대조가 잡은 셋 — 없는 쉼표 · 지역 설정 날짜 · 위장한 General")
# 앞의 여덟 사례는 전부 **우리가 만들어 낸 값**으로 잡았다. 이 둘은 슬랙에 실제로
# 올라온 xlsx 24개(셀 26,426개)를 LibreOffice 와 셀 단위로 견줘서야 나왔다.
CORPUS_CASES = [
    # ① 천단위 쉼표는 자리표시자에 `,` 가 있을 때만 찍는다. 늘 찍고 있었다.
    ("0_);[Red]\(0\)", 38417, "38417", True),   # 실물 모양: 어느 이자 시트의 정수 칸
    ("0.0", 31682.41537208194, "31682.4", True),  # 실물 모양: 어느 현황표의 긴 소수
    ("0", 1520.4, "1520", True),
    ("0.00", 1520.4, "1520.40", True),
    ("#,##0", 1520.4, "1,520", True),             # 쉼표가 있는 서식은 그대로
    ("#,##0.0", 1520.4, "1,520.4", True),
    ("0_);[Red]\(0\)", -38417, "(38417)", True),  # 음수 구역에서도 마찬가지
]
corpus_failed = 0
for fmt, value, want, want_known in CORPUS_CASES:
    text, known = render_value(StubCell(value, fmt))
    if text == want and known is want_known:
        ok(f"{fmt!r} on {value!r} → {text!r}")
    else:
        corpus_failed += 1
        bad(f"{fmt!r} on {value!r} → {text!r} (해석됨={known}) — {want!r} · known={want_known} 여야 합니다")

# ② 엑셀 내장 서식 14번. openpyxl 은 `mm-dd-yy` 라는 글자로 주지만 엑셀은 **보는
#    사람의 지역 설정**으로 찍는다 — 한국에서 열면 2024-01-28 이다. 글자 그대로
#    읽으면 01-28-24 라는 미국식 날짜가 known=True 로 나간다. 실물에 390셀 있었다.
text, known = render_value(StubCell(dt.datetime(2024, 1, 28), "mm-dd-yy"))
if known is False:
    ok(f"'mm-dd-yy'(내장 14번) → 해석 못 함: {text!r}")
else:
    corpus_failed += 1
    bad(f"'mm-dd-yy' → {text!r} (해석됨={known}) — 지역 설정을 따르는 서식이라 known=False 여야 합니다")
# 반대쪽 — 지역과 무관하게 자리가 정해진 서식은 그대로 해석한다
for fmt, want in [("yyyy-mm-dd", "2024-01-28"), ("yyyy/mm/dd", "2024/01/28")]:
    text, known = render_value(StubCell(dt.datetime(2024, 1, 28), fmt))
    if text == want and known is True:
        ok(f"{fmt!r} → {text!r}")
    else:
        corpus_failed += 1
        bad(f"{fmt!r} → {text!r} (해석됨={known}) — {want!r} 여야 합니다")

# ③ openpyxl 이 못 푼 내장 서식은 `number_format` 이 `General` 이라고 **거짓말**을 한다.
#    27~36·50~58번은 한국·일본어 로케일 전용 내장 날짜인데 openpyxl 표에 없어서
#    조용히 `'General'` 이 나온다. 31번이 `yyyy"년" mm"월" dd"일"` 이고, 어느 사업장의
#    산정내역 엑셀 `대출이자` 시트 P13 이 정확히 그 상태다 — 화면은 `2025년 03월 14일` 인데 우리는
#    일련번호 `45730` 을 known=True 로 냈다. 서식 번호가 0 이 아니면 모르는 것이다.
with tempfile.TemporaryDirectory() as td:
    import re as _re2
    import zipfile as _zip
    _p1, _p2 = Path(td) / "a.xlsx", Path(td) / "b.xlsx"
    _wb = openpyxl.Workbook()
    _wb.active["A1"] = 45730
    _wb.active["A1"].number_format = "0.00"  # xf 자리를 하나 만들어 둔다
    _wb.save(_p1)
    # 그 xf 의 numFmtId 를 31(한국어 내장 날짜)로 바꿔 실물과 같은 상태를 만든다.
    # 스텁이 아니라 **진짜 파일 왕복**이라야 「openpyxl 이 정말 General 로 준다」가 증명된다.
    _zin, _zout = _zip.ZipFile(_p1), _zip.ZipFile(_p2, "w")
    for _it in _zin.infolist():
        _d = _zin.read(_it.filename)
        if _it.filename == "xl/styles.xml":
            _s = _d.decode("utf-8")
            _end = _s.index("</cellXfs>")
            _at = _s.rindex('<xf numFmtId="', 0, _end) + len('<xf numFmtId="')
            _stop = _s.index('"', _at)
            _d = (_s[:_at] + "31" + _s[_stop:]).encode("utf-8")
        _zout.writestr(_it, _d)
    _zin.close()
    _zout.close()
    # 파일이 아니라 메모리에서 연다 — 윈도우는 열린 핸들이 남으면 임시 폴더를 못 지운다
    import io as _io
    _wb2 = openpyxl.load_workbook(_io.BytesIO(_p2.read_bytes()), data_only=True)
    _cell = _wb2.active["A1"]
    if _cell.number_format != "General" or _cell._style.numFmtId != 31:
        corpus_failed += 1
        bad(f"시험의 전제가 낡았습니다 — number_format={_cell.number_format!r} "
            f"numFmtId={_cell._style.numFmtId} (General/31 이어야 재현입니다)")
    else:
        text, known = render_value(_cell)
        if known is False and text == "45730":
            ok(f"위장한 General(내장 31번) → 해석 못 함: {text!r}")
        else:
            corpus_failed += 1
            bad(f"위장한 General → {text!r} (해석됨={known}) — 원값 · known=False 여야 합니다")

if corpus_failed == 0:
    ok("실물 대조가 잡은 셋 전부 통과")

print("[31/45] 만들어진 파일은 LF 로 끝나고, 행 상한 기본값은 1,000 이다")
# 줄바꿈: CRLF 로 쓰면 SKILL.md 의 `head -n 1 | sed` 가 뽑는 --source 끝에 \r 이 붙어
#   insert_entry.py 의 중복 판정이 영영 안 맞고, 루프를 다시 돌리면 같은 시트가 통째로
#   다시 들어간다 (에러 없이 회차만 두 배).
# 상한: 실측이 정한 값은 1,000 이다. 400 이면 사업장 두 곳의 CP(91) 시트(541행)와
#   또 다른 사업장의 집계 시트(816·809행)가 함께 떨어진다.
import re as _re  # noqa: E402
import subprocess  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "행상한.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "요약"  # 어느 상한에서도 남는다 — 상한이 틀려도 시험이 파일 없음으로 죽지 않게
    for i in range(1, 11):
        ws.cell(row=i, column=1, value=i)
    ws1 = wb.create_sheet("집계")
    for i in range(1, 542):  # CP(91) 과 같은 541행 — 400 상한이면 떨어진다
        ws1.cell(row=i, column=1, value=i)
    ws2 = wb.create_sheet("계약자원장")
    for i in range(1, 1380):  # 사업장나 1-1.계약자list 와 같은 1,379행 — 1,000 상한에서 빠진다
        ws2.cell(row=i, column=1, value=i)
    wb.save(p)

    outdir = Path(td) / "blocks"
    r = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "xlsx_to_blocks.py"),
         "--file", str(p), "--date", "2026-08-22", "--source", "행상한.xlsx",
         "--out-dir", str(outdir)],   # --max-rows 를 일부러 안 준다 (기본값을 본다)
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0 or not (outdir / "sheet-01.md").exists():
        bad(f"변환이 실패했습니다: {(r.stderr or r.stdout).strip()[:200]}")
    else:
        raw = (outdir / "sheet-01.md").read_bytes()
        if b"\r\n" not in raw:
            ok("sheet-01.md 에 CRLF 가 없다")
        else:
            bad("sheet-01.md 이 CRLF 로 쓰였습니다 — insert_entry 의 중복 판정이 깨집니다")
        if b"\r\n" not in (outdir / "meta.json").read_bytes():
            ok("meta.json 에 CRLF 가 없다")
        else:
            bad("meta.json 이 CRLF 로 쓰였습니다")

        # SKILL.md 의 삽입 루프가 하는 그대로 — 헤더 첫 줄에서 --source 를 뽑는다.
        header = raw.decode("utf-8").split("\n")[0]
        src = _re.sub(r"^\*\*\d{4}-\d{2}-\d{2} · (.*)\*\*$", r"\1", header)
        if src == "행상한.xlsx — 시트 1/2: 요약":
            ok(f"헤더에서 뽑은 --source 에 \\r 이 안 붙는다: {src!r}")
        else:
            bad(f"--source 가 {src!r} 입니다 — 끝에 \\r 이 붙었거나 헤더 계약이 깨졌습니다")

        meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
        if meta["sheets"] == ["요약", "집계"]:
            ok("541행 시트는 기본 상한(1,000)에서 살아남는다")
        else:
            bad(f"meta['sheets'] = {meta['sheets']} — 기본 상한이 1,000 이 아닙니다(400?)")
        if [s["name"] for s in meta["skipped_sheets"]] == ["계약자원장"]:
            ok("1,379행 원장 시트만 상한을 넘어 빠진다")
        else:
            bad(f"meta['skipped_sheets'] = {meta['skipped_sheets']}")

# 화면 출력은 파이프로 나가도 UTF-8 이어야 한다.
#
# 윈도우 파이썬은 stdout 이 **파이프면** 로캘 인코딩(여기서는 cp949)으로 쓴다. 화면에
# 직접 찍을 때는 안 드러나고, 받아 적는 순간에만 깨진다 — 그래서 오래 안 보였다.
# `insert_entry.py:34-38` 은 같은 이유로 이미 stdout·stderr 를 UTF-8 로 맞춰 둔다.
#
# 안 맞추면 **정보가 가장 필요한 순간에 사라진다.** 바로 위 [30/32] 이 쓰는
# `subprocess.run(..., encoding="utf-8")` 은 cp949 바이트를 만나면 읽기 스레드가
# UnicodeDecodeError 로 죽고, 그때 `r.stdout` 이 **None** 이 된다. 그러면 917~918행의
# 실패 보고 `(r.stderr or r.stdout).strip()` 이 사유를 찍는 대신 AttributeError 로
# 터진다 — 변환이 진짜 실패한 날 사유 대신 엉뚱한 예외만 남는다는 뜻이다
# (2026-08-23 실측: `0xbd in position 0` · `r.stdout is None` · AttributeError).
print("[32/45] 화면 출력이 파이프로 나가도 UTF-8 이라 받아 적을 수 있다")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "출력인코딩.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "요약"
    ws.cell(row=1, column=1, value="값")
    wb.save(p)

    r = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "xlsx_to_blocks.py"),
         "--file", str(p), "--date", "2026-08-05", "--source", "출력인코딩.xlsx",
         "--out-dir", str(Path(td) / "blocks")],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.stdout is None:
        bad("r.stdout 이 None 입니다 — 읽기 스레드가 죽었습니다(파이프 출력이 UTF-8 이 아닙니다). "
            "이 상태면 변환 실패 시 917행의 사유 보고가 AttributeError 로 터집니다")
    elif "시트" in r.stdout:
        ok(f"파이프로 받은 출력이 그대로 읽힌다: {r.stdout.splitlines()[0][:40]!r}")
    else:
        bad(f"출력에 '시트' 가 없습니다: {r.stdout[:120]!r}")

    # 실패 보고 경로를 그대로 흉내낸다 — 위 검사가 통과해도 이 줄이 터지면 소용없다.
    try:
        (r.stderr or r.stdout).strip()
        ok("실패 보고 경로 `(r.stderr or r.stdout).strip()` 가 터지지 않는다")
    except AttributeError:
        bad("실패 보고 경로가 AttributeError 로 터집니다 — 변환 실패 사유를 영영 못 봅니다")

# 셀 메모는 표에 안 보이지만 숫자의 근거다 — 블록에 실어야 한다.
#
# 엑셀 셀 메모(셀 우측 상단 빨간 삼각형)는 `<table>` 어디에도 안 나온다. SKILL.md 가
# 워드 주석에 대해 적어 둔 것과 같은 함정이다 — 「본문은 멀쩡한데 그 파일을 올린
# 이유가 통째로 빠진다」. 2026-08-23 첫 범위 13건 실측: **실린 시트에만 메모 58개
# 3,004자**가 있었고 내용이 「회수가능액= 예상토지매각가 + PFV 잔존시재 전체」,
# 「EOD 2/3 초일산입, 말일불산입」, 「26.05.21. 기준 (26.05.22. 대체공휴일)」처럼
# 값이 왜 그 값인지였다. 팀이 봇에게 실제로 묻는 것이 이쪽이다.
#
# 거르는 기준은 **표와 같아야 한다** — 숨긴 행·숨긴 시트는 담지 않으므로 거기 달린
# 메모도 담지 않는다(그러지 않으면 안 실린 줄의 주석만 떠다닌다).
print("[33/45] 셀 메모가 블록에 실리고, 안 실린 자리의 메모는 빠진다")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "메모.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "산정"
    ws["A1"] = "항목"
    ws["B1"] = 100
    ws["B1"].comment = openpyxl.comments.Comment("심현준:\n26.05.21. 기준", "심현준")
    ws["A2"] = "숨긴 줄"
    ws["B2"] = 200
    ws["B2"].comment = openpyxl.comments.Comment("TY:\n안 실려야 한다", "TY")
    ws.row_dimensions[2].hidden = True

    ws2 = wb.create_sheet("메모없음")
    ws2["A1"] = "값만 있다"

    ws3 = wb.create_sheet("폐기안")
    ws3["A1"] = "숨긴 시트"
    ws3["A1"].comment = openpyxl.comments.Comment("TY:\n숨긴 시트 메모", "TY")
    ws3.sheet_state = "hidden"
    wb.save(p)

    blocks, meta = build_blocks(p, "2026-08-05", "메모.xlsx", max_rows=100)
    body = blocks[0]["body"]

    if "26.05.21. 기준" in body:
        ok("보이는 셀의 메모가 블록에 실린다")
    else:
        bad(f"메모가 블록에 없습니다 — 숫자의 근거가 통째로 빠집니다: {body[-200:]!r}")
    if "B1" in body:
        ok("어느 셀의 메모인지 셀 주소가 붙는다")
    else:
        bad("메모에 셀 주소가 없습니다 — 어느 값에 달린 것인지 알 수 없습니다")
    if body.count("심현준") == 1:
        ok("작성자가 한 번만 나온다 (원문의 '이름:' 접두어가 겹쳐 붙지 않는다)")
    else:
        bad(f"작성자가 {body.count('심현준')}번 나옵니다 — 원문 접두어를 안 떼면 두 번이 됩니다")
    if "안 실려야 한다" not in body:
        ok("숨긴 행의 메모는 빠진다 (표와 같은 기준)")
    else:
        bad("숨긴 행의 메모가 실렸습니다 — 표에 없는 줄의 주석만 떠다닙니다")
    if "숨긴 시트 메모" not in "".join(b["body"] for b in blocks):
        ok("숨긴 시트의 메모도 빠진다")
    else:
        bad("숨긴 시트의 메모가 실렸습니다")
    if "메모" not in blocks[1]["body"]:
        ok("메모가 없는 시트에는 메모 절이 안 붙는다")
    else:
        bad(f"메모 없는 시트에 절이 붙었습니다: {blocks[1]['body'][-160:]!r}")
    if meta.get("comments") == 1:
        ok("메타가 실제로 실은 메모 개수를 적는다 (1개)")
    else:
        bad(f"meta['comments'] = {meta.get('comments')!r} — 실은 것만 세어 1 이어야 합니다")

print("[34/45] 유효숫자 15자리 너머에서 답이 갈리는 값은 그리지 않고 센다")
# 엑셀은 유효숫자 15자리까지만 보여준다. 요청한 소수 자리가 그 너머에 있으면
# 「한 번에 반올림」과 「15자리로 줄인 뒤 반올림」의 답이 갈린다.
#
# **어느 쪽이 엑셀인지 우리는 모른다.** 2026-08-23 에 LibreOffice 로 218셀을 재 보니
# 15자리 쪽이 맞은 것 124 · 지금 쪽이 맞은 것 79 · **둘 다 틀린 것 5** 였고,
# 같은 자릿수·같은 소수자리 안에서도 값마다 갈렸다(정수 11자리·소수 1자리 = 6 대 4).
# 그래서 한쪽으로 정하지 않고 _render_general 과 같은 규칙을 쓴다 — 갈리면 안 그리고 센다.
# 실물 빈도는 26,426셀 중 2셀(0.008%)이라 계수기를 덮지 않는다.
for value, fmt, why in [
    (879168056724929.4, "#,##0.0", "15번째 자리 너머에 소수가 있다"),
    (2109901986.7499976, "0.0", "15자리로 줄이면 .7 이 .8 로 올라간다"),
    (9375060359.314995, "0.00", "반대 방향 — 줄이면 .31 이 .32 가 된다"),
    # 두 길이 우연히 같아도 못 믿는 자리. LibreOffice 가 이 값을 `0.0` 으로는 `…9`,
    # `0.00` 으로는 `…80` 으로 그린다 — 자기모순이라 어느 쪽이 엑셀인지 가릴 수 없다.
    (68601575893949.84, "0.0", "반올림을 결정하는 자리가 15번째다"),
    # 퍼센트는 100을 곱한 **뒤** 자리를 세야 한다. 곱하기 전으로 재면 이 값이 통과한다.
    (68772868931.2345, "0.0%", "100 곱한 뒤 결정 자리가 15번째 — LibreOffice 는 .4%"),
]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.xlsx"
        wb = openpyxl.Workbook()
        wb.active["A1"] = value
        wb.active["A1"].number_format = fmt
        wb.save(p)
        cell = openpyxl.load_workbook(p, data_only=True).active["A1"]
        text, known = render_value(cell)
        # **저장된 값과 비교한다.** xlsx 로 한 번 오가면 정밀도가 줄어
        # 2109901986.7499976 이 2109901986.749998 로 읽힌다 — 원래 리터럴과 대면 헛맞는다.
        if not known and text == str(cell.value):
            ok(f"{cell.value!r} + '{fmt}' → 원값 + 해석 못 함 ({why})")
        else:
            bad(
                f"{cell.value!r} + '{fmt}' → {text!r} (해석됨={known}) — "
                f"갈리는 값이라 {str(cell.value)!r} + known=False 여야 합니다 ({why})"
            )

# 갈리지 않는 값은 그대로 그려야 한다 — 위 규칙이 멀쩡한 셀까지 삼키면 안 된다.
for value, fmt, want in [
    (1520.4, "#,##0", "1,520"),
    (20336.5, "#,##0", "20,337"),
    (0.125, "0.0%", "12.5%"),
    (31682.4, "0.0", "31682.4"),
]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.xlsx"
        wb = openpyxl.Workbook()
        wb.active["A1"] = value
        wb.active["A1"].number_format = fmt
        wb.save(p)
        cell = openpyxl.load_workbook(p, data_only=True).active["A1"]
        text, known = render_value(cell)
        if (text, known) == (want, True):
            ok(f"{value!r} + '{fmt}' → {want!r} (안 갈리는 값은 그대로)")
        else:
            bad(f"{value!r} + '{fmt}' → {text!r} (해석됨={known}) — {want!r} 이어야 합니다")

print("[35/45] 반올림해서 0 이 된 음수에 마이너스를 붙이지 않는다")
# LibreOffice 대조가 잡은 것(2026-08-23, check_against_libreoffice.py 첫 실행).
# -0.125 를 `#,##0` 으로 그리면 `-0` 이 나왔다 — 화면은 `0` 이다. **아카이브에 이미 1셀
# 들어가 있었다.** General 경로는 `Context.plus` 가 `-0.0` 의 부호를 떼 주어 처음부터
# `0` 이었는데, 자리표시자 경로만 안 떼고 있어 우리 렌더러 안에서도 서로 어긋나 있었다.
for value, fmt, want in [
    (-0.125, "#,##0", "0"),
    (-0.4, "#,##0", "0"),
    (-0.004, "0.00", "0.00"),
    (-0.0004, "0.0%", "0.0%"),
    # 음수 구역이 따로 있는 서식은 **구역 선택이 먼저**다. 값이 음수이므로 2번 구역을
    # 쓰고, 그 구역이 그리는 글자는 그대로 나온다 — 부호 떼기가 구역을 덮으면 안 된다.
    (-0.125, "#,##0;(#,##0)", "(0)"),
    # 멀쩡한 음수는 그대로여야 한다
    (-1520.4, "#,##0", "-1,520"),
]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.xlsx"
        wb = openpyxl.Workbook()
        wb.active["A1"] = value
        wb.active["A1"].number_format = fmt
        wb.save(p)
        cell = openpyxl.load_workbook(p, data_only=True).active["A1"]
        text, known = render_value(cell)
        if (text, known) == (want, True):
            ok(f"{value!r} + '{fmt}' → {want!r}")
        else:
            bad(f"{value!r} + '{fmt}' → {text!r} (해석됨={known}) — {want!r} 이어야 합니다")

print("[36/45] 「해석 못 함」이 개수만이 아니라 **사유별로** 세어진다")
# 왜: 점검표가 「서식 해석 못 함 N셀」만 적어서, 한 파일이 14.69% 였을 때 읽는 사람이
# 무엇을 봐야 할지 알 수 없었다(전부 한 원인이었는데도). 사유는 판정하는 그 순간에만
# 있고 known=False 라는 bool 로 접히면서 사라진다 — 그래서 표시 쪽만 고쳐서는 복구가
# 안 되고, 세는 자리까지 사유를 들고 와야 한다.
#
# 같은 파일의 `숨긴 행 65(내용 있는 것 46)` · `오류 셀 12개 (#DIV/0! · #N/A)` 와 같은 꼴이다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "사유"
    # ① 반올림이 렌더러마다 갈리는 값 (2026-08-23 에 넣은 규칙)
    ws["A1"] = 9375060359.314995
    ws["A1"].number_format = "0.00"
    ws["A2"] = 68601575893949.84
    ws["A2"].number_format = "0.0"
    # ② General 인데 지수 표기 구간이라 화면 글자를 확정할 수 없다
    ws["B1"] = 1e17
    ws["B1"].number_format = "General"
    # ③ 구역이 넷을 넘는 서식 — 우리가 모르는 것이라 통째로 해석하지 않는다
    ws["C1"] = 5
    ws["C1"].number_format = "0;0;0;0;0"
    # 멀쩡한 셀도 섞어 둔다 — 사유 세기가 이런 셀까지 삼키면 안 된다
    ws["D1"] = 1520.4
    ws["D1"].number_format = "#,##0"
    wb.save(p)

    blocks, meta = build_blocks(p, "2026-08-23", "사유시험.xlsx", max_rows=100)
    reasons = meta.get("unformatted_reasons")

    if isinstance(reasons, dict) and reasons:
        ok(f"메타에 사유 내역이 있다: {reasons}")
    else:
        bad(f"meta['unformatted_reasons'] = {reasons!r} — {{사유: 개수}} 여야 합니다")

    if isinstance(reasons, dict):
        total = sum(reasons.values())
        if total == meta["unformatted_cells"]:
            ok(f"사유 합계가 총계와 맞는다 ({total}셀)")
        else:
            bad(f"사유 합계 {total} ≠ unformatted_cells {meta['unformatted_cells']} — "
                "한쪽만 세면 표가 거짓말을 한다")

        # 사유가 **갈려** 있어야 한다. 하나로 뭉뚱그리면 고치기 전과 똑같다.
        if len(reasons) >= 3:
            ok(f"사유가 {len(reasons)}갈래로 갈렸다")
        else:
            bad(f"사유가 {len(reasons)}갈래뿐입니다: {reasons} — "
                "반올림 갈림 · General 지수 구간 · 구역 초과가 서로 달라야 합니다")

        joined = " ".join(reasons)
        for word in ("반올림", "General", "구역"):
            if word in joined:
                ok(f"사유에 '{word}' 갈래가 있다")
            else:
                bad(f"사유에 '{word}' 갈래가 없습니다: {list(reasons)}")

    # 멀쩡한 셀은 안 세어져야 한다 — 표에 1,520 이 그대로 있어야 한다
    body = blocks[0]["body"]
    if "1,520" in body:
        ok("멀쩡한 셀은 그대로 그려진다")
    else:
        bad(f"멀쩡한 셀까지 삼켰습니다: {body[:200]}")

print("\n[머리글 판정]")
from xlsx_to_blocks import header_kind, find_header_row, MASK_TEXT

for text, want in [
    ("대표자명", "keep"),       # 이름은 안 가린다 (WHK 결정 2026-08-26).
                                # **None 이 아니라 keep 이다** — 머리글 찾기에는 계속 세어져야 한다
    ("핸드폰", "mask"),
    ("EMAIL", "mask"),          # 대문자 — 대소문자를 안 가려야 한다
    ("주민등록 주소1", "mask"),  # 부분 일치
    ("우편물주소2", "mask"),
    ("계좌번호", "mask"),
    ("담당", "keep"),
    ("관리담당", "keep"),
    ("팀", "keep"),
    ("고객명", None),           # 상호다 — 가리지도, 직원 열도 아니다
    ("업체명", None),
    ("공급금액", None),
    ("상담내용(운영팀)", "keep"),  # '팀' 이 걸린다. 머리글 찾기에만 쓰이므로 무해
    ("팀장 성명", "keep"),      # 이름 낱말이 mask 를 안 타므로 keep 으로 떨어진다
    # NOT_PERSONAL_HEADERS — 머리글 **전체가 정확히** 같을 때만 예외다 (WHK 결정 2026-08-25)
    ("물건지 주소", "keep"),
    (" 물건지 주소 ", "keep"),   # 앞뒤 공백은 무시한다
    ("물건지 주소1", "mask"),    # 한 글자만 달라도 예외가 아니다
    ("물건지 주소 및 지번", "mask"),
    ("계약자 물건지 주소", "mask"),  # 부분 일치가 아니므로 예외에 안 걸린다
    ("주민등록 주소1", "mask"),  # 예외를 넣어도 이건 그대로 가려져야 한다
    ("신탁계좌가압류", "keep"),      # 여부 표시 열 — 계좌번호가 아니다
    ("신탁계좌가압류1", "mask"),     # 한 글자만 달라도 예외가 아니다
    # ── 이름 낱말 5개는 전부 keep 이다 (2026-08-26 마스킹 규칙 2판) ──
    ("성명", "keep"),
    ("이름", "keep"),
    ("계약자명", "keep"),
    ("대상자", "keep"),
    ("계약자 성명", "keep"),
    # 이름 낱말이 빠져도 나머지는 그대로 가린다
    ("연락처", "mask"),
    ("생년월일", "mask"),
    ("", None),
    (None, None),
    # ── 낱말 사이에 다른 말이 끼어드는 주민번호 (2026-09-03) ──
    # 셋 다 `주민번호` 의 부분 문자열도 `주민등록번호` 의 부분 문자열도 아니다.
    # 낱말 목록으로는 어느 것도 안 걸려 열이 통째로 안 가려졌다.
    ("주민(사업자)번호", "mask"),
    ("주민/사업자번호", "mask"),
    ("주민(법인)번호", "mask"),
    ("주민등록·사업자번호", "mask"),
    ("주민 등록 번호", "mask"),     # 칸을 띄워 적은 것
    ("주민\n등록번호", "mask"),      # 머리글을 두 줄로 접어 적은 것
    ("주민등록번호(뒷자리)", "mask"),
    # ── 주소의 다른 이름 (2026-09-03) ──
    ("거주지", "mask"),
    ("실거주지", "mask"),           # `거주지` 가 부분 일치로 잡는다
    ("거주지 주소", "mask"),
    ("송달장소", "mask"),
    # ── 넓힌 것이 애먼 열을 삼키지 않는다 ──
    ("제2종일반주거지역", None),    # 용도지역이지 사람이 사는 곳이 아니다.
                                    # 그래서 `주거지` 는 낱말 목록에 안 넣었다
    ("주거용", None),
    ("비주거용건축착공면적추이", None),
    ("사업자번호", None),           # 법인 번호 열 — `주민` 이 없으므로 안 걸린다
    ("법인등록번호", None),
    ("건축물대장고유번호", None),
    ("부동산고유번호", None),
    ("일련번호", None),
    ("입찰번호", None),
    ("문서번호", None),
]:
    got = header_kind(text)
    if got == want:
        ok(f"머리글 {text!r} → {got}")
    else:
        bad(f"머리글 {text!r} → {got}, {want} 여야 합니다")

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "머리글3행"
ws["A3"], ws["B3"], ws["C3"] = "동호", "대표자명", "핸드폰"
ws["A4"], ws["B4"], ws["C4"] = "C-101", "홍길동", "010-7777-8888"
if find_header_row(ws) == 3:
    ok("빈 줄 뒤 3행 머리글을 찾는다")
else:
    bad(f"머리글 행 {find_header_row(ws)} — 3 이어야 합니다")

ws2 = wb.create_sheet("머리글2행")
ws2["B2"], ws2["I2"] = "대표자명", "담당"   # mask 1 + keep 1 = 2건
ws2["B3"] = "김철수"
if find_header_row(ws2) == 2:
    ok("mask·keep 을 함께 세어 2행 머리글을 찾는다")
else:
    bad(f"머리글 행 {find_header_row(ws2)} — 2 여야 합니다")

# 이름 낱말을 mask 에서 뺐다고 머리글 찾기가 깨지면 시트가 통째로 안 실린다.
ws6 = wb.create_sheet("이름과담당")
ws6["A1"], ws6["B1"] = "대표자명", "담당"   # keep 2건 — 문턱 2 를 넘어야 한다
ws6["A2"], ws6["B2"] = "홍길동", "박담당"
if find_header_row(ws6) == 1:
    ok("이름 낱말도 머리글 찾기에는 세어진다 (keep 2건 → 1행)")
else:
    bad(f"머리글 행 {find_header_row(ws6)} — 1 이어야 합니다. "
        "이름 낱말이 None 으로 떨어지면 이 시트는 통째로 안 실립니다")

ws3 = wb.create_sheet("머리글없음")
ws3["A1"], ws3["B1"] = "구분", "합계"
ws3["A2"], ws3["B2"] = "전일", 4182
if find_header_row(ws3) is None:
    ok("걸리는 낱말이 없으면 None 을 준다")
else:
    bad(f"머리글 행 {find_header_row(ws3)} — None 이어야 합니다")

ws4 = wb.create_sheet("한건뿐")
ws4["A1"] = "대표자명"          # 1건뿐 — 문턱 2 를 못 넘는다
if find_header_row(ws4) is None:
    ok("걸린 낱말이 1건이고 값 칸도 1개뿐이면 머리글로 안 본다")
else:
    bad(f"머리글 행 {find_header_row(ws4)} — None 이어야 합니다 "
        "(2차의 「값 3칸」 문턱을 못 넘는다)")

ws5 = wb.create_sheet("깊은곳")
ws5["A12"], ws5["B12"] = "대표자명", "핸드폰"   # 12행 — 훑는 범위 10 밖
if find_header_row(ws5) is None:
    ok("훑는 범위(10행) 밖은 안 찾는다")
else:
    bad(f"머리글 행 {find_header_row(ws5)} — None 이어야 합니다")

from xlsx_to_blocks import find_header_row_detail, masked_columns as _mc

# 2차 — 낱말 1개짜리라도 라벨이고 값 칸이 3개 이상이면 머리글로 본다.
ws7 = wb.create_sheet("2차채택")
ws7["A2"], ws7["B2"], ws7["C2"], ws7["D2"] = "구분", "계좌번호", "잔액", "비고"
ws7["A3"], ws7["B3"] = "갑은행", "123-456-789012"
if find_header_row_detail(ws7) == (2, "fallback"):
    ok("낱말 1개 + 라벨 + 값 4칸 → 2차로 2행을 머리글로 잡는다")
else:
    bad(f"2차 채택 {find_header_row_detail(ws7)} — (2, 'fallback') 이어야 합니다")

# **급여계좌 모양** — 제목 줄이 위에 있어도 진짜 머리글 줄을 잡아야 한다.
# 이 시험이 이번 변경의 핵심이다. 2행을 잡으면 계좌번호 6개가 그대로 나간다.
ws8 = wb.create_sheet("급여계좌모양")
ws8["A2"] = "□ 급여계좌"
ws8["A3"], ws8["B3"], ws8["C3"], ws8["D3"] = "구분", "계좌번호", "잔액", "비고"
ws8["A4"], ws8["B4"], ws8["C4"] = "갑은행", "123456-78-901234", 1000
row8, how8 = find_header_row_detail(ws8)
if (row8, how8) == (3, "fallback") and _mc(ws8, row8) == {2: "계좌번호"}:
    ok("제목 줄(값 1칸)을 건너뛰고 3행을 잡아 계좌번호 열을 가린다")
else:
    bad(f"급여계좌 모양 → 머리글 {row8}({how8}) · 가릴 열 {_mc(ws8, row8)} — "
        "(3, 'fallback') 과 {2: '계좌번호'} 여야 합니다. "
        "2행을 잡으면 은행명 열을 가리고 계좌번호가 그대로 나갑니다")

# 값 칸이 2개뿐이면 2차에서도 안 잡는다.
ws9 = wb.create_sheet("값두칸")
ws9["A1"], ws9["B1"] = "계좌번호", "잔액"
if find_header_row(ws9) is None:
    ok("값 칸이 2개뿐인 행은 2차에서도 머리글로 안 본다")
else:
    bad(f"값두칸 → {find_header_row(ws9)}, None 이어야 합니다 (값 3칸 문턱)")

# 낱말이 서술 문장 안에 있으면 2차 후보가 아니다.
ws10 = wb.create_sheet("문장속낱말")
ws10["A1"] = ("으로 ○○은행 ○○지점 대출 상담사 연락처 안내, 차주 중 대출자서예정"
              "이며 잔금 납부 일정은 추후 협의하기로 함")
ws10["B1"], ws10["C1"], ws10["D1"] = "확인", "완료", "비고"
if find_header_row(ws10) is None:
    ok("서술 문장 안의 낱말은 2차 머리글 후보가 아니다")
else:
    bad(f"문장속낱말 → {find_header_row(ws10)}, None 이어야 합니다")

# 1차가 되는 시트는 how 가 strict 이고 결과가 안 바뀐다.
if find_header_row_detail(ws) == (3, "strict"):
    ok("낱말 2개짜리는 1차로 잡고 strict 로 표시한다")
else:
    bad(f"1차 표시 {find_header_row_detail(ws)} — (3, 'strict') 여야 합니다")

# 2차 후보가 둘이면 **위엣것**을 쓴다. 아래 것을 쓰면 진짜 머리글 아래의
# 데이터 행이 머리글이 되어 그 위의 열이 통째로 안 가려진다.
ws11 = wb.create_sheet("후보둘")
ws11["A1"], ws11["B1"], ws11["C1"] = "계좌번호", "잔액", "비고"
ws11["A2"], ws11["B2"], ws11["C2"] = "갑은행", 1000, "-"
ws11["A3"], ws11["B3"], ws11["C3"] = "주소", "동호수", "면적"
if find_header_row_detail(ws11) == (1, "fallback"):
    ok("2차 후보가 둘이면 위엣것을 머리글로 잡는다")
else:
    bad(f"후보둘 → {find_header_row_detail(ws11)}, (1, 'fallback') 이어야 합니다")

# 공백만 있는 칸은 값으로 안 센다. 세면 제목 줄이 문턱을 넘어 머리글이 된다.
ws12 = wb.create_sheet("공백칸제목")
ws12["A1"], ws12["B1"], ws12["C1"] = "□ 급여계좌", " ", "  "
ws12["A2"], ws12["B2"], ws12["C2"], ws12["D2"] = "구분", "계좌번호", "잔액", "비고"
ws12["A3"], ws12["B3"] = "갑은행", "123456-78-901234"
if find_header_row_detail(ws12) == (2, "fallback"):
    ok("공백만 있는 칸은 값으로 안 세어 제목 줄이 머리글이 되지 않는다")
else:
    bad(f"공백칸제목 → {find_header_row_detail(ws12)}, (2, 'fallback') 이어야 합니다 — "
        "1행을 잡으면 계좌번호 열이 안 가려집니다")

print("\n[시트를 빼는 판정]")
from xlsx_to_blocks import sheet_drop_reason

# 라벨 칸에 계좌가 있고 머리글을 못 찾으면 계속 뺀다 (고정계좌 모양).
wsd1 = wb.create_sheet("라벨계좌")
wsd1["A1"] = "□ 고정계좌"
wsd1["A3"] = "합계"
if sheet_drop_reason(wsd1):
    ok("라벨 칸의 계좌는 여전히 시트를 빼는 이유다")
else:
    bad("라벨계좌 → 안 뺀다, 빼야 합니다")

# 서술 문장 안의 계좌·주소는 빼는 이유가 아니다 (사업장가 상담 메모).
wsd2 = wb.create_sheet("문장계좌")
wsd2["A1"] = "중도금이자계좌 안됨, 주소 확인 요청했으나 알려주지 않음 — 재통화 예정"
if sheet_drop_reason(wsd2) is None:
    ok("서술 문장 안의 계좌·주소는 시트를 빼는 이유가 아니다")
else:
    bad(f"문장계좌 → {sheet_drop_reason(wsd2)!r}, None 이어야 합니다")

# 전화·이메일은 인라인이 모양으로 잡으므로 빼는 이유가 아니다.
wsd3 = wb.create_sheet("전화라벨")
wsd3["A1"], wsd3["B1"] = "연락처", "이메일"
wsd3["A2"], wsd3["B2"] = "010-1234-5678", "hong@company.com"
if sheet_drop_reason(wsd3) is None:
    ok("전화·이메일 낱말은 인라인이 잡으므로 시트를 뺄 이유가 아니다")
else:
    bad(f"전화라벨 → {sheet_drop_reason(wsd3)!r}, None 이어야 합니다")

# 주민번호는 구분자가 없으면 인라인이 못 잡는다 — 그래서 빼는 이유로 남겼다.
wsd5 = wb.create_sheet("주민번호라벨")
wsd5["A1"] = "주민등록번호"
if sheet_drop_reason(wsd5):
    ok("주민번호 라벨은 시트를 빼는 이유다 (구분자 없으면 인라인이 못 잡는다)")
else:
    bad("주민번호라벨 → 안 뺀다, 빼야 합니다")

# 끼어드는 말이 있어도 주민번호는 주민번호다 (2026-09-03).
wsd6 = wb.create_sheet("주민사업자번호라벨")
wsd6["A1"] = "주민(사업자)번호"
if sheet_drop_reason(wsd6):
    ok("`주민(사업자)번호` 라벨도 시트를 빼는 이유다")
else:
    bad("주민사업자번호라벨 → 안 뺀다, 빼야 합니다")

# 주소의 다른 이름도 같은 이유로 빼는 이유가 된다 — 주소는 모양이 없어
# 인라인이 못 잡는다.
wsd7 = wb.create_sheet("거주지라벨")
wsd7["A1"] = "거주지"
if sheet_drop_reason(wsd7):
    ok("`거주지` 라벨도 시트를 빼는 이유다")
else:
    bad("거주지라벨 → 안 뺀다, 빼야 합니다")

# 다만 거주지·송달장소는 **10자 이하일 때만** 빼는 이유다 (WHK 결정 2026-09-03).
# 실물 아카이브의 거주지 칸은 전부 상담 메모·제출서류 목록(20·25·28자)이고
# 머리글 칸은 0종이다. 30자 문턱만으로는 저것들이 통과해 표 하나를 통째로 버린다.
for name, label, want_drop in (
    ("실거주지라벨", "실거주지", True),           # 4자 — 진짜 머리글
    ("송달장소라벨", "송달장소", True),           # 4자
    ("거주지주소라벨", "거주지 주소", True),      # 6자
    # 경계는 세지 말고 계산해서 만든다 — 손으로 세다 두 번 틀렸다.
    ("열자경계", "거주지" + "1" * 7, True),        # 정확히 10자 — 경계 안
    ("열한자경계", "거주지" + "1" * 8, False),     # 정확히 11자 — 경계 밖
    ("상담메모", "현 거주지 매매시\n잔금정산 가능성 中", False),   # 20자 · 실물
    ("제출서류", "15. 거주지 임대차 계약서(사본, 해당자만 제출)", False),  # 28자 · 실물
):
    ws_ = wb.create_sheet(name)
    ws_["A1"] = label
    got = sheet_drop_reason(ws_) is not None
    if got == want_drop:
        ok(f"{label[:18]!r}({len(label)}자) → {'뺀다' if want_drop else '안 뺀다'}")
    else:
        bad(f"{label[:18]!r}({len(label)}자) → {'뺀다' if got else '안 뺀다'}, "
            f"{'뺀다' if want_drop else '안 뺀다'} 여야 합니다")

# 대조군 — 길이 규칙은 **빼는 이유**에만 걸고 **열 마스킹**에는 안 건다.
# 긴 칸이라도 그것이 머리글로 뽑히면 그 열은 여전히 가려야 한다.
from xlsx_to_blocks import header_kind as _hk
_long = "거주지" + "1" * 8          # 11자 — 빼는 이유로는 경계 밖
if _hk(_long) == "mask":
    ok(f"{len(_long)}자 `거주지…` 도 열 마스킹 대상이다 (길이 규칙은 빼는 이유에만 건다)")
else:
    bad(f"{_long!r} → header_kind {_hk(_long)!r}, 'mask' 여야 합니다")

# NOT_PERSONAL_HEADERS 예외는 빼는 이유도 아니다.
wsd4 = wb.create_sheet("예외머리글")
wsd4["A1"] = "신탁계좌가압류"
if sheet_drop_reason(wsd4) is None:
    ok("NOT_PERSONAL_HEADERS 예외는 시트를 뺄 이유가 아니다")
else:
    bad(f"예외머리글 → {sheet_drop_reason(wsd4)!r}, None 이어야 합니다")

# 사유 문구는 사람이 읽는 자리다 — 「머리글」이 들어 있어야 무엇을 할지 안다.
if "머리글" in (sheet_drop_reason(wsd1) or ""):
    ok("빼는 사유에 무엇 때문인지가 적힌다")
else:
    bad(f"빼는 사유 {sheet_drop_reason(wsd1)!r} — '머리글' 이 들어가야 합니다")

print("\n[2차 머리글 표시]")
_wbf = openpyxl.Workbook()
_wsf = _wbf.active
_wsf.title = "급여계좌"
_wsf["A2"] = "□ 급여계좌"
_wsf["A3"], _wsf["B3"], _wsf["C3"], _wsf["D3"] = "구분", "계좌번호", "잔액", "비고"
_wsf["A4"], _wsf["B4"], _wsf["C4"] = "갑은행", "123456-78-901234", 1000
_wsf2 = _wbf.create_sheet("보통")
_wsf2["A1"], _wsf2["B1"] = "대표자명", "담당"
_wsf2["A2"], _wsf2["B2"] = "홍길동", "박담당"
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "fallback.xlsx"
    _wbf.save(_p)
    _blocks, _meta = build_blocks(str(_p), "2026-08-27", "fallback.xlsx", 1000)

if _meta.get("header_fallback_sheets") == ["급여계좌"]:
    ok("2차로 잡은 시트만 meta 에 남는다")
else:
    bad(f"header_fallback_sheets {_meta.get('header_fallback_sheets')!r} — "
        "['급여계좌'] 여야 합니다")

if _meta["unmaskable_sheets"] == []:
    ok("급여계좌는 이제 빠지지 않는다")
else:
    bad(f"unmaskable_sheets {_meta['unmaskable_sheets']!r} — 빈 리스트여야 합니다")

if "***" in _blocks[0]["body"] and "123456-78-901234" not in _blocks[0]["body"]:
    ok("계좌번호가 열 마스킹으로 가려진다")
else:
    bad("급여계좌의 계좌번호가 안 가려졌습니다 — 이번 변경의 핵심 줄입니다")

print("\n[끼어드는 말이 있는 머리글 — 통째 재현 (2026-09-03)]")
# 실무에서 흔한 표기 그대로다. **값은 전부 가짜다.**
#
# 이 표가 왜 위험했나: `연락처` 하나가 걸려 머리글 행은 **정상적으로 찾아지므로**
# `sheet_drop_reason` 안전망이 아예 안 돈다. 그래서 연락처만 `***` 가 되고
# 구분자 없는 주민번호와 주소는 그대로 실렸다 — 마스킹이 돌긴 돌아서
# 「가려졌다」로 보이는 상태였다.
_wbv = openpyxl.Workbook()
_wsv = _wbv.active
_wsv.title = "계약자명단"
_wsv["A1"], _wsv["B1"] = "성명", "주민(사업자)번호"
_wsv["C1"], _wsv["D1"] = "거주지", "연락처"
_wsv["A2"], _wsv["B2"] = "가나다", "9001011234567"
_wsv["C2"], _wsv["D2"] = "○○시 ○○구 ○○로 1길 2, 101동 303호", "010-1111-2222"
_wsv["A3"], _wsv["B3"] = "라마바", "8512312345678"
_wsv["C3"], _wsv["D3"] = "△△시 △△구 △△로 3로 45, 202호", "010-3333-4444"
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "변형머리글.xlsx"
    _wbv.save(_p)
    _blocksv, _metav = build_blocks(str(_p), "2026-09-03", "변형머리글.xlsx", 1000)

_bodyv = _blocksv[0]["body"]
if "9001011234567" not in _bodyv and "8512312345678" not in _bodyv:
    ok("`주민(사업자)번호` 열의 구분자 없는 주민번호가 가려진다")
else:
    bad("구분자 없는 주민번호가 그대로 실렸습니다 — 인라인은 구분자가 없으면 못 잡습니다")

if "○○로 1길" not in _bodyv and "△△로 3로" not in _bodyv:
    ok("`거주지` 열의 주소가 가려진다")
else:
    bad("거주지 열의 주소가 그대로 실렸습니다 — 주소는 모양이 없어 인라인이 못 잡습니다")

if set(_metav["masked"]["columns"]) == {"주민(사업자)번호", "거주지", "연락처"}:
    ok("가린 열 셋이 meta 에 그대로 남는다")
else:
    bad(f"masked.columns {_metav['masked']['columns']!r} — "
        "{'주민(사업자)번호', '거주지', '연락처'} 여야 합니다")

# 사람 이름은 여전히 안 가린다 (WHK 결정 2026-08-26). 이번 변경이 그 결정을
# 건드리지 않았다는 것을 이 줄이 지킨다.
if "가나다" in _bodyv and "라마바" in _bodyv:
    ok("성명 열은 그대로 열려 있다")
else:
    bad("성명 열이 가려졌습니다 — 이름은 안 가리기로 한 결정과 어긋납니다")

print("\n[라벨 판정]")
from xlsx_to_blocks import is_label_cell

for text, want in [
    ("계좌번호", True),
    ("□ 급여계좌", True),
    ("이자유보계좌(tr.b)", True),
    # 실제 머리글로 확인된 것 중 최장 25자 — 이것이 False 가 되면 (축약) 반응도 4행이 머리글을 잃는다
    ("내용증명 발송 이후 계약자 반응도(6월 이후)", True),
    # 실측 최단 문장 37자
    ("주) 출자전환 주주 중 투자설명서 교부 원하는 고객에게 주소로 발송", False),
    # 사업장가 상담 메모 계열 — 문장이지 머리글이 아니다
    ("으로 ○○은행 ○○지점 대출 상담사 연락처 안내, 차주 중 대출자서예정이며 "
     "잔금 납부 일정은 추후 협의하기로 함", False),
    ("  계좌번호  ", True),   # 앞뒤 공백은 뺀다
    ("", False),
    ("   ", False),
    (None, False),
    (1234, True),            # 숫자 셀도 글자로 보고 잰다
]:
    got = is_label_cell(text)
    if got == want:
        ok(f"라벨 판정 {str(text)[:20]!r} → {got}")
    else:
        bad(f"라벨 판정 {str(text)[:20]!r} → {got}, {want} 여야 합니다")

print("\n[열 마스킹]")
from xlsx_to_blocks import masked_columns

wbm = openpyxl.Workbook()
wsm = wbm.active
wsm.title = "호실별관리대장"
for c, v in enumerate(["동호", "고객명", "대표자명", "핸드폰", "EMAIL", "공급금액"], start=1):
    wsm.cell(row=3, column=c, value=v)
for c, v in enumerate(["C-101", "갑스튜디오", "홍길동", "010-7777-8888",
                       "gildong@naver.com", 1170110000], start=1):
    wsm.cell(row=4, column=c, value=v)

cols = masked_columns(wsm, 3)
if set(cols) == {4, 5}:
    ok(f"가릴 열을 2개 골랐다 ({sorted(cols)}) — 대표자명은 이제 안 가린다")
else:
    bad(f"가릴 열 {sorted(cols)} — 4·5(핸드폰·EMAIL) 여야 합니다")

body = sheet_to_table(wsm, mask_cols=set(cols))
for gone in ("010-7777-8888", "gildong@naver.com"):
    if gone not in body:
        ok(f"{gone!r} 가 본문에서 사라졌다")
    else:
        bad(f"{gone!r} 가 본문에 남아 있습니다")
for stay in ("C-101", "갑스튜디오", "홍길동", "1170110000"):
    if stay in body:
        ok(f"{stay!r} 는 그대로 남았다")
    else:
        bad(f"{stay!r} 까지 지웠습니다: {body[:300]}")
if body.count(MASK_TEXT) == 2:
    ok("가린 칸이 2개 — 이름 칸은 안 가린다")
else:
    bad(f"가린 칸 {body.count(MASK_TEXT)}개 — 2개여야 합니다")
if "대표자명" in body and "EMAIL" in body:
    ok("머리글 자체는 안 가린다 — 어떤 정보가 있었는지 남는다")
else:
    bad("머리글까지 가렸습니다 — 봇이 '가려져 있다'고 답할 근거가 사라집니다")

print("\n[메모 마스킹 · 가려진 행 수]")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "메모마스킹.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "호실별관리대장"
    # 3열은 이름이 아니라 생년월일로 둔다 — 이름 열은 이제 안 가리므로(WHK 결정
    # 2026-08-26), 여기서는 여전히 가려지는 열로 마스킹·메모 삭제를 시험한다.
    for c, v in enumerate(["동호", "고객명", "생년월일", "핸드폰", "EMAIL", "공급금액"], start=1):
        ws.cell(row=3, column=c, value=v)
    # 행 4 — 마스킹 열(생년월일·핸드폰·EMAIL)에 값이 있다 → 가려진 행으로 세어야 한다
    for c, v in enumerate(["C-101", "갑스튜디오", "1990-01-01", "010-1111-2222",
                           "a@a.com", 100], start=1):
        ws.cell(row=4, column=c, value=v)
    # 행 5 — 마스킹 열에 값이 있다 → 가려진 행
    for c, v in enumerate(["C-102", "다른업체", "1991-02-02", "010-2222-3333",
                           "b@b.com", 200], start=1):
        ws.cell(row=5, column=c, value=v)
    # 행 6 — 마스킹 열은 전부 비고 일반 열만 값이 있다 → 세지 않아야 한다
    ws.cell(row=6, column=1, value="C-103")
    ws.cell(row=6, column=2, value="세번째업체")
    ws.cell(row=6, column=6, value=300)

    # 메모 — 마스킹 열(3열 생년월일, C4)에 하나, 일반 열(2열 고객명, B4)에 하나
    ws["C4"].comment = openpyxl.comments.Comment("작성자:\n1990-01-01 개인정보", "작성자")
    ws["B4"].comment = openpyxl.comments.Comment("작성자:\n일반 메모", "작성자")
    wb.save(p)

    blocks, meta = build_blocks(p, "2026-08-23", "메모마스킹시험.xlsx", max_rows=100)
    body = blocks[0]["body"]

    if "일반 메모" in body:
        ok("가리지 않은 열의 메모는 남는다")
    else:
        bad(f"가리지 않은 열의 메모까지 사라졌습니다: {body}")

    if "개인정보" not in body and "1990-01-01" not in body:
        ok("가려진 열의 메모는 통째로 빠진다")
    else:
        bad(f"가려진 열의 메모가 남아 있습니다: {body}")

    dropped = meta["masked"]["comments_dropped"]
    if dropped == 1:
        ok(f"버려진 메모 수가 맞다 ({dropped}개)")
    else:
        bad(f"버려진 메모 수 {dropped} — 1 이어야 합니다")

    if meta["comments"] == 1:
        ok(f"meta['comments'] 는 실은 것만 센다 ({meta['comments']}개, 버려진 메모는 안 셈)")
    else:
        bad(f"meta['comments'] = {meta['comments']} — 1 이어야 합니다 (버려진 메모는 빼고 센다)")

    masked_row_count = meta["masked"]["rows"]
    if masked_row_count == 2:
        ok(f"가려진 행 수가 맞다 ({masked_row_count}행 — 마스킹 열에 값 있는 행만)")
    else:
        bad(f"가려진 행 수 {masked_row_count} — 2 여야 합니다 (마스킹 열에 값 없는 행은 안 셈)")

print("\n[셀 안 마스킹]")
from xlsx_to_blocks import mask_inline

for text, want_text, want_n in [
    ("4/9 부재, 010-7777-8888 로 재통화", f"4/9 부재, {MASK_TEXT} 로 재통화", 1),
    ("메일 gildong@naver.com 발송", f"메일 {MASK_TEXT} 발송", 1),
    ("주민 800101-1234567 확인", f"주민 {MASK_TEXT} 확인", 1),
    ("4/9 현재 외국 체류중", "4/9 현재 외국 체류중", 0),   # 개인 사정이지만 모양이 없다
    ("공급금액 1,170,110,000", "공급금액 1,170,110,000", 0),
    ("2026-07-10", "2026-07-10", 0),                     # 날짜를 전화로 오인하면 안 된다
    ("", "", 0),
]:
    got_text, got_n = mask_inline(text)
    if (got_text, got_n) == (want_text, want_n):
        ok(f"셀 안 마스킹 {text[:24]!r}")
    else:
        bad(f"셀 안 마스킹 {text!r} → {got_text!r}/{got_n}, {want_text!r}/{want_n} 여야 합니다")

# ── 신설 항목 (2026-08-26 마스킹 규칙 2판) ──────────────────────────────
# 항목마다 **가려지는 예와 안 가려지는 예를 짝으로** 둔다. 이 저장소가 겪은
# 마스킹 사고는 전부 "안 가려지는 예"가 없어서 났다.
from xlsx_to_blocks import INLINE_PATTERNS, _INLINE_MASK, mask_inline  # noqa: E402

MASK_INLINE_NEW = [
    # (설명, 넣는 글자, 기대 글자, 기대 건수)
    ("외국인등록번호", "등록 900101-5123456 확인", f"등록 {MASK_TEXT} 확인", 1),
    ("법인등록번호", "법인등록번호 : 110111-1111111", f"법인등록번호 : {MASK_TEXT}", 1),
    ("사업자등록번호", "등록 123-45-67890 발급", f"등록 {MASK_TEXT} 발급", 1),
    ("유선전화 서울", "대표 02-1234-5678 으로", f"대표 {MASK_TEXT} 으로", 1),
    ("유선전화 경기", "사무실 031-123-4567", f"사무실 {MASK_TEXT}", 1),
    ("안심번호", "연락 0507-1234-5678 로", f"연락 {MASK_TEXT} 로", 1),
    # 050 은 국번 넷이 통째로 개인번호 서비스다 — 0505·0507 만 적어 뒀다가
    # 사업장파 신탁계약서의 팩스 `0502-0000-1111` 를 놓쳤다 (2026-08-26).
    ("안심번호 0502", "팩스 0502-0000-1111", f"팩스 {MASK_TEXT}", 1),
    ("안심번호 0500", "연락 0500-123-4567", f"연락 {MASK_TEXT}", 1),
    ("안심번호 0509", "연락 0509-123-4567", f"연락 {MASK_TEXT}", 1),
    # 060·040 처럼 목록 밖 국번은 그대로 둔다 — 닫힌 목록인 것이 이 패턴의 안전장치다
    ("목록 밖 국번 040", "코드 0401-123-4567 참조", "코드 0401-123-4567 참조", 0),
    # ── 지역번호를 괄호로 닫는 표기 (2026-09-03) ────────────────────────────
    # 구분자 집합이 `[-–.\s]` 뿐이라 지역번호 뒤가 `)` 인 표기를 통째로 비켜 갔다.
    # 공문·감정평가서의 담당자 줄(`담당 : ○○○ 차장 ☎ (02)000-0000`)이 이 모양이라
    # 이름과 직통번호가 함께 아카이브에 실렸다. 아래 아홉은 실물 20가지 표기를
    # 모양별로 덮은 것이다 — **번호는 전부 지어낸 것**이고, 실물은 쓰지 않는다.
    ("괄호 지역번호 02 · 4-4", "담당 (02)1111-2222", f"담당 {MASK_TEXT}", 1),
    ("괄호 지역번호 02 · 3-4", "담당 (02)333-4444", f"담당 {MASK_TEXT}", 1),
    # 여는 괄호가 없는 반쪽 표기도 실물에 있다 (원문에서 앞 괄호가 떨어져 나온 것).
    ("닫는 괄호만 · 4-4", "담당 02)5555-6666", f"담당 {MASK_TEXT}", 1),
    ("닫는 괄호만 · 3-4", "담당 02)777-8888", f"담당 {MASK_TEXT}", 1),
    ("닫는 괄호 뒤 공백", "담당 02) 9999-0000", f"담당 {MASK_TEXT}", 1),
    ("괄호 지역번호 054", "문의 (054)111-2222", f"문의 {MASK_TEXT}", 1),
    ("괄호 지역번호 063", "문의 (063)333-4444", f"문의 {MASK_TEXT}", 1),
    # 지역번호와 `)` 사이가 벌어진 것 — pdf 의 칸 채움이 md 로 그대로 넘어온 것이다.
    # 실물(어느 사업장의 채권압류통지서)에서는 그 자리가 **빈 줄**이라 문단이 갈라져 있었고,
    # 같은 번호가 같은 문서 안에서 한 번은 가려지고 한 번은 안 가려질 뻔했다 (2026-09-03).
    ("괄호 안이 공백으로 벌어진 것", "전화:(02  )333-4444", f"전화:{MASK_TEXT}", 1),
    ("괄호 안이 빈 줄로 갈라진 것", "전화:(02\n\n)333-4444", f"전화:{MASK_TEXT}", 1),
    ("벌어져도 목록 밖 국번은 그대로", "코드 (040  )123-4567", "코드 (040  )123-4567", 0),
    # ── 050 은 세 자리로도 쓴다 (2026-09-03) ────────────────────────────────
    # `50\d` 가 050 을 네 자리(`0505`·`0507`)로만 봐서 `(050)`·`050-` 을 놓쳤다.
    ("050 세 자리 · 괄호", "연락 (050)1111-2222", f"연락 {MASK_TEXT}", 1),
    ("050 세 자리 · 하이픈", "연락 050-1111-2222", f"연락 {MASK_TEXT}", 1),
    # ── 괄호를 열어도 닫힌 목록과 「구분자 필수」는 그대로다 ──────────────────
    ("괄호라도 목록 밖 국번 040", "코드 (040)123-4567 참조", "코드 (040)123-4567 참조", 0),
    ("괄호라도 목록 밖 국번 060", "코드 (060)700-1234 참조", "코드 (060)700-1234 참조", 0),
    ("괄호 뒤에도 둘째 구분자는 필수", "코드 (02)12345678 참조", "코드 (02)12345678 참조", 0),
    ("구분자 없는 10자리는 그대로", "금액 0212345678 원", "금액 0212345678 원", 0),
    ("계좌 — 낱말 있음", "입금계좌 301-0123-4567", f"입금계좌 {MASK_TEXT}", 1),
    # ── 여기부터는 건드리면 안 되는 것 ──
    ("ISO 날짜", "납부일 2026-08-26 입니다", "납부일 2026-08-26 입니다", 0),
    ("계좌 낱말이 없는 숫자", "코드 301-0123-4567 참조", "코드 301-0123-4567 참조", 0),
    ("계좌 낱말 옆의 날짜", "계좌 개설일 2026-08-26", "계좌 개설일 2026-08-26", 0),
    ("금액", "공급금액 1,170,110,000 원", "공급금액 1,170,110,000 원", 0),
    ("긴 숫자열", "발행번호 1301802300000 조회", "발행번호 1301802300000 조회", 0),
    ("계약자 이름", "계약자 홍길동 님", "계약자 홍길동 님", 0),
    ("이미 가려진 것", f"연락처 {MASK_TEXT}", f"연락처 {MASK_TEXT}", 0),
    # ── 법인등록번호는 뒷자리 첫 글자가 아무 값이나 온다 (2026-08-26) ──────
    # 주민등록번호는 그 자리가 성별·국적 코드(1~4 내국인 · 5~8 외국인)라 닫힌
    # 목록이지만, **법인등록번호의 같은 자리는 일련번호**라 0·9 로도 시작한다.
    # 그걸 `[1-8]` 로 묶어 뒀다가 아카이브에서 4건을 놓쳤다 — 아래 두 값이 그것이다.
    # 실물 법인등록번호는 공개 등기 정보라 시험에 그대로 써도 된다.
    ("법인등록번호 뒷자리 0 — 병정보", "법인번호 110111-0111111",
     f"법인번호 {MASK_TEXT}", 1),
    ("법인등록번호 뒷자리 0 — 병공영개발", "소유자 171211-0222222 경상북도",
     f"소유자 {MASK_TEXT} 경상북도", 1),
    # 9 로 시작하는 실물은 이 아카이브에 아직 없다. 구조상 올 수 있으니 지어내 둔다.
    ("법인등록번호 뒷자리 9", "법인등록번호 110111-9012345",
     f"법인등록번호 {MASK_TEXT}", 1),
    # ── 넓힌 뒤에도 걸리면 안 되는 것 ── 자리 수가 13이어야 하고 구분자가 있어야 한다
    ("6-2-6 계좌는 이 패턴에 안 걸린다", "갑은행 067501-04-116062",
     "갑은행 067501-04-116062", 0),
    ("구분자 없는 13자리", "발행번호 1101110111111 조회", "발행번호 1101110111111 조회", 0),
    ("앞뒤에 숫자가 더 붙은 것", "9110111-01111112 참조", "9110111-01111112 참조", 0),
]
for label, text, want_text, want_n in MASK_INLINE_NEW:
    got_text, got_n = mask_inline(text)
    if got_text == want_text and got_n == want_n:
        ok(f"인라인 {label}")
    else:
        bad(f"인라인 {label}: {got_text!r}/{got_n} — {want_text!r}/{want_n} 여야 합니다")

# 사업자등록번호와 유선전화는 서로 안 겹친다 — 가운데 자릿수가 다르다.
_t, _n = mask_inline("사업자 031-81-12345 확인")
if _t == f"사업자 {MASK_TEXT} 확인" and _n == 1:
    ok("사업자등록번호가 유선전화로 이중 마스킹되지 않는다")
else:
    bad(f"031-81-12345 → {_t!r}/{_n} — 한 번만 가려져야 합니다")

# `ACCOUNT_NUM` 은 다섯 인라인 패턴과 달리 앞뒤 숫자 경계(`(?<!\d)`/`(?!\d)`)가 없었다
# (Task 1 리뷰에서 발견, 2026-08-26 수정). 그래서 긴 숫자열 한가운데서 걸려
# `mask_inline("계좌 1234567890-123-4567890123 확인")` 이 "계좌 1234***23 확인" 처럼
# 앞뒤 진짜 숫자를 남긴 채 가운데만 지웠다. 경계를 더한 지금은 이 값이 2~6·2~6·2~8
# 자리 어느 조합으로도 안 맞아 **통째로 안 걸려야** 한다 — 부분만 먹히면 안 된다.
_long_run_text = "계좌 1234567890-123-4567890123 확인"
_long_run_got, _long_run_n = mask_inline(_long_run_text)
if _long_run_got == _long_run_text and _long_run_n == 0:
    ok("긴 숫자열 한가운데를 부분만 계좌로 잡지 않는다 (전부 넘긴다)")
else:
    bad(f"긴 숫자열 계좌 경계: {_long_run_got!r}/{_long_run_n} — "
        f"부분만 먹히면 안 됩니다 (원문 그대로 0건이어야 함)")

# ── 계좌 토막이 넷인 것 (2026-09-03) ────────────────────────────────────────
# `ACCOUNT_NUM` 은 토막을 **셋으로 고정**하고 있었다. 뒤 경계 `(?!\d)` 는 다음
# 글자가 `-` 면 통과하므로, 농협·기업식 네 토막 계좌는 **앞 세 토막만 가려지고
# 마지막 토막이 그대로 남았다** — `mask_inline("입금계좌 301-1234-5678-90")` 이
# `"입금계좌 ***-90"` 을 냈다 (2026-09-03 재현).
# 「가려진 것처럼 보이는데 숫자가 남는」 모양이라 눈으로도 안 걸린다.
# 자료 저장소 실물에도 `301-12-34567-8` 이 `301-12-34567` 로 잘려 잡히고
# 있었다. 구분자는 **넓히지 않는다** — md 483개를 줄마다 훑어 재 보면 `[-–.\s]`
# 로 넓히는 순간 1건이 4건이 되는데 늘어난 3건이 전부 OCR 숫자표의 공백·소수점
# 이고, 토막까지 2~4 로 넓히면 113건에 소수·연월만 는다 (2026-09-03 전수 실측).
ACCOUNT_SEGMENTS = [
    # (이름, 원문, 기대 결과, 기대 건수)
    ("네 토막 — 농협식", "입금계좌 301-1234-5678-90",
     f"입금계좌 {MASK_TEXT}", 1),
    ("네 토막 — 마지막이 여러 자리", "가상계좌 351-1234-5678-9012",
     f"가상계좌 {MASK_TEXT}", 1),
    # ── 대조군: 세 토막은 전과 똑같이 가려져야 한다 ──
    ("세 토막 — 6자리 꼬리", "입금계좌 110-123-456789",
     f"입금계좌 {MASK_TEXT}", 1),
    ("세 토막 — 6-2-6", "예금주 홍길동 067501-04-116062",
     f"예금주 홍길동 {MASK_TEXT}", 1),
    # ── 반대 방향: 날짜는 토막이 늘어도 여전히 안 걸려야 한다 ──
    ("계좌 낱말 옆의 날짜", "계좌 개설일 2026-08-31", "계좌 개설일 2026-08-31", 0),
    ("계좌 낱말 옆의 두 자리 연도 날짜", "계좌 등록 26-08-31", "계좌 등록 26-08-31", 0),
    ("계좌 낱말이 없으면 네 토막도 안 걸린다", "코드 301-1234-5678-90 참조",
     "코드 301-1234-5678-90 참조", 0),
    ("연월은 토막이 모자라 안 걸린다", "계좌 개설 2026-08 기준", "계좌 개설 2026-08 기준", 0),
]
for label, text, want_text, want_n in ACCOUNT_SEGMENTS:
    got_text, got_n = mask_inline(text)
    if got_text == want_text and got_n == want_n:
        ok(f"계좌 토막 {label}")
    else:
        bad(f"계좌 토막 {label}: {got_text!r}/{got_n} — "
            f"{want_text!r}/{want_n} 여야 합니다")

# **남는 숫자가 한 자리도 없어야 한다.** 위 표는 글자열 전체를 대보지만, 이
# 검사는 「가린 뒤에도 계좌 숫자가 남았나」만 따로 본다 — 토막 수를 또 늘려야
# 하는 날이 오면 여기서 먼저 빨개진다.
for _txt in ("입금계좌 301-1234-5678-90", "가상계좌 351-1234-5678-9012"):
    _got, _ = mask_inline(_txt)
    if any(ch.isdigit() for ch in _got):
        bad(f"계좌를 가린 뒤에도 숫자가 남습니다: {_txt!r} → {_got!r}")
    else:
        ok(f"계좌를 가린 뒤 숫자가 남지 않는다 ({_txt})")

# 실물과 3-2-5-1 모양만 같은 가짜 `301-12-34567-8`(실물 계좌는 자료 저장소
# `documents/index.md` 에 있고 이 저장소에는 안 적는다)은
# **정규식 단위로 재야 한다.** `mask_inline` 은 인라인 다섯을 먼저 돌리는데
# 사업자등록번호 패턴 `(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)` 이 `301-12-34567` 을
# 먼저 먹고 `-8` 을 남긴다 — 그 패턴도 뒤 경계가 `-` 를 통과시키는 **같은 갈래의
# 별개 결함**이고, 그쪽은 계좌 낱말 게이트가 없어 넓히는 영향이 안 재어졌다.
# 여기서는 계좌 정규식이 네 토막을 통째로 잡는지만 본다.
from xlsx_to_blocks import ACCOUNT_NUM  # noqa: E402

_acct_whole = [m.group(0) for m in ACCOUNT_NUM.finditer("계좌 301-12-34567-8")]
if _acct_whole == ["301-12-34567-8"]:
    ok("ACCOUNT_NUM 이 실물 네 토막 계좌를 통째로 잡는다 (301-12-34567-8)")
else:
    bad(f"ACCOUNT_NUM 이 301-12-34567-8 을 통째로 안 잡습니다: {_acct_whole}")

# ── 사업자등록번호 뒤에 하이픈이 더 붙은 것 (2026-09-03) ──────────────────────
# `ACCOUNT_NUM` 과 **같은 갈래의 별개 결함**이었다. 뒤 경계 `(?!\d)` 는 다음
# 글자가 `-` 면 통과하므로 `\d{3}-\d{2}-\d{5}` 이 더 긴 번호의 앞부분만 먹고
# 꼬리를 남겼다 — `mask_inline("301-12-34567-8")` 이 `"***-8"` 을 냈다.
# **이 잔여는 자료 저장소에 실제로 들어가 있었다**: 어느 자금운영보고 md 의 표
# 한 칸이 `<td>***-8</td>` 이었다 (2026-09-03 전수 확인, 같은 날 `***` 로 닫았다).
# 그 칸에는 계좌 낱말이 없어 `ACCOUNT_NUM` 게이트가 안 열리므로 **계좌 쪽 수정으로는
# 안 막힌다.**
#
# 고친 모양은 계좌와 같다 — 꼬리 토막 하나를 **선택으로** 받는다. 이것은 마스킹을
# **넓히기만 한다**: 매치 시작 위치는 하나도 늘지 않고(자료 저장소 md 483개·줄
# 130,341 에서 새 시작 위치 0), 이미 가리던 구간을 전부 포함한다.
#
# **뒤 경계를 `(?![\d-–])` 로 막는 처방은 쓰면 안 된다.** 그러면 하이픈이 붙은
# 순간 통째로 **안 가려진다** — 아래 「꼬리가 붙어도 통째로 가린다」가 그 방향의
# 대조군이고, 그 처방에서는 원문이 그대로 남아 빨개진다.
BIZ_TAIL = [
    # (이름, 원문, 기대 결과, 기대 건수)
    ("실물 — 계좌 낱말 없는 칸", "301-12-34567-8", MASK_TEXT, 1),
    ("실물 — 계좌 낱말 있는 줄", "계좌 301-12-34567-8", f"계좌 {MASK_TEXT}", 1),
    ("꼬리가 붙어도 통째로 가린다", "등록 123-45-67890-1 참조",
     f"등록 {MASK_TEXT} 참조", 1),
    ("꼬리가 여러 자리여도", "등록 123-45-67890-1234 참조",
     f"등록 {MASK_TEXT} 참조", 1),
    # ── 대조군: 꼬리가 없는 진짜 사업자등록번호는 전과 똑같이 가려진다 ──
    ("대조군 — 꼬리 없는 사업자등록번호", "등록 123-45-67890 발급",
     f"등록 {MASK_TEXT} 발급", 1),
    ("대조군 — 라벨과 함께", "사업자등록번호 : 123-45-67890",
     f"사업자등록번호 : {MASK_TEXT}", 1),
    # ── 반대 방향: 넓혀도 걸리면 안 되는 것 ──
    ("꼬리가 숫자로 이어 붙은 것은 그대로", "발행번호 123-45-678901 조회",
     "발행번호 123-45-678901 조회", 0),
    ("앞에 숫자가 더 붙은 것은 그대로", "코드 9123-45-67890 참조",
     "코드 9123-45-67890 참조", 0),
    ("ISO 날짜는 그대로", "납부일 2026-08-26 입니다", "납부일 2026-08-26 입니다", 0),
    ("구분자 없는 10자리는 그대로", "발행번호 1234567890 조회",
     "발행번호 1234567890 조회", 0),
]
for label, text, want_text, want_n in BIZ_TAIL:
    got_text, got_n = mask_inline(text)
    if got_text == want_text and got_n == want_n:
        ok(f"사업자 꼬리 {label}")
    else:
        bad(f"사업자 꼬리 {label}: {got_text!r}/{got_n} — "
            f"{want_text!r}/{want_n} 여야 합니다")

# **가린 뒤에 숫자가 한 자리도 남으면 안 된다.** 위 표는 글자열 전체를 대보지만
# 이 검사는 「반쯤 가려진 채 숫자가 남았나」만 따로 본다 — 계좌 쪽 같은 검사와
# 짝이다. 꼬리 토막을 또 늘려야 하는 날이 오면 여기서 먼저 빨개진다.
for _txt in ("301-12-34567-8", "등록 123-45-67890-1 참조"):
    _got, _ = mask_inline(_txt)
    if any(ch.isdigit() for ch in _got):
        bad(f"사업자등록번호를 가린 뒤에도 숫자가 남습니다: {_txt!r} → {_got!r}")
    else:
        ok(f"사업자등록번호를 가린 뒤 숫자가 남지 않는다 ({_txt})")

# 라벨 목록과 정규식 목록의 길이가 갈리면 screen_personal 의 zip 이 조용히 자른다.
if len(INLINE_PATTERNS) == len(_INLINE_MASK):
    ok(f"INLINE_PATTERNS 와 _INLINE_MASK 의 길이가 같다 ({len(INLINE_PATTERNS)})")
else:
    bad(f"길이가 다릅니다: {len(INLINE_PATTERNS)} vs {len(_INLINE_MASK)}")

wbi = openpyxl.Workbook()
wsi = wbi.active
wsi["A2"], wsi["B2"] = "대표자명", "상담내용"
wsi["A3"], wsi["B3"] = "김철수", "4/14 전매, 010-1234-5678 로 연락"
bodyi = sheet_to_table(wsi, mask_cols={1})
if "010-1234-5678" not in bodyi and "4/14 전매" in bodyi:
    ok("자유 서술 열은 남기고 그 안의 전화만 가린다")
else:
    bad(f"자유 서술 처리가 틀렸습니다: {bodyi[:300]}")

print("\n[셀 안 마스킹 건수 — meta]")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "인라인건수.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "상담"
    ws["A1"], ws["B1"] = "동호", "상담내용"
    # 전화만 있는 칸 1개 + 이메일·전화가 함께 있는 칸 1개 → 건수(치환 횟수)는 1+2=3
    ws["A2"], ws["B2"] = "C-101", "4/9 부재, 010-7777-8888 로 재통화"
    ws["A3"], ws["B3"] = "C-102", "메일 gildong@naver.com / 010-1234-5678 로 회신"
    wb.save(p)

    blocks, meta = build_blocks(p, "2026-08-23", "인라인건수시험.xlsx", max_rows=100)
    inline_n = meta["masked"]["inline"]
    if inline_n == 3:
        ok(f"셀 안 마스킹 건수가 meta 에 맞게 쌓인다 ({inline_n}건)")
    else:
        bad(f"meta['masked']['inline'] = {inline_n} — 3 이어야 합니다 (전화1 + (이메일1+전화1))")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "인라인없음.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "메모"
    ws["A1"], ws["B1"] = "동호", "메모"
    ws["A2"], ws["B2"] = "C-101", "특이사항 없음"
    wb.save(p)

    blocks, meta = build_blocks(p, "2026-08-23", "인라인없음시험.xlsx", max_rows=100)
    if "inline" in meta["masked"] and meta["masked"]["inline"] == 0:
        ok("가릴 게 없어도 meta['masked']['inline'] 키가 0 으로 남는다")
    else:
        bad(f"meta['masked'] = {meta['masked']} — inline 키가 0 으로 있어야 합니다")

wsc = openpyxl.Workbook().active
wsc["A1"] = "010-1111-2222 로 연락 바랍니다"
body_c = sheet_to_table(wsc)
if "010-1111-2222" not in body_c and MASK_TEXT in body_c:
    ok("inline 인자를 안 줘도 셀 안 마스킹은 그대로 동작한다")
else:
    bad(f"inline 인자 없이 부른 결과가 달라졌습니다: {body_c[:200]}")

print("\n[머리글 못 찾은 시트]")
import tempfile as _tf
with _tf.TemporaryDirectory() as _d:
    _p = Path(_d) / "위험.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "정상"
    _ws["A1"], _ws["B1"] = "구분", "합계"
    _ws["A2"], _ws["B2"] = "전일", 4482
    # 머리글이 훑는 범위(10행) 밖이라 못 찾는데, 개인정보 낱말은 시트 안에 있다.
    # 핸드폰·이메일 등은 이제 인라인이 모양으로 잡아 시트를 빼는 이유가 아니다
    # (Task 3, DROP_TRIGGER_WORDS) — 그래서 라벨 칸에서만 잡히는 계좌로 시험한다.
    _ws2 = _wb.create_sheet("위험")
    _ws2["A12"] = "계좌"
    _ws2["A13"] = "1234567890"
    # **이번 변경의 핵심 주장을 끝까지 가서 확인한다.** 핸드폰은 이제 시트를
    # 빼는 이유가 아닌데(DROP_TRIGGER_WORDS 밖), 그래도 새지 않는 이유는
    # 인라인 마스킹이 머리글 없이도 모양으로 잡기 때문이다. 그 주장이 틀리면
    # 전화번호가 그대로 나간다 — 판정만 보는 시험으로는 그것을 못 잡는다.
    _ws3 = _wb.create_sheet("핸드폰만")
    _ws3["A12"] = "핸드폰"
    _ws3["A13"] = "010-7777-8888"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-08-23", "위험.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "위험" not in names:
        ok("머리글을 못 찾은 개인정보 시트를 뺐다")
    else:
        bad(f"마스킹 없이 실렸습니다: {names}")
    if "정상" in names:
        ok("개인정보 낱말이 없는 시트는 그대로 실린다")
    else:
        bad(f"멀쩡한 시트까지 뺐습니다: {names}")
    un = _meta.get("unmaskable_sheets")
    if un and un[0]["name"] == "위험" and un[0]["why"]:
        ok(f"뺀 사유가 메타에 적혔다: {un[0]['why']}")
    else:
        bad(f"unmaskable_sheets 가 비었습니다: {un} — 사유 없이 빼면 유실로 읽힌다")
    if "핸드폰만" in names:
        ok("핸드폰 낱말만 있는 시트는 이제 빠지지 않는다")
    else:
        bad(f"핸드폰만 시트가 빠졌습니다: {names} — 인라인이 잡으므로 실려야 합니다")
    _body = next((b["body"] for b in _blocks if b["name"] == "핸드폰만"), "")
    if "010-7777-8888" not in _body and MASK_TEXT in _body:
        ok("머리글이 없어도 인라인이 전화번호를 가린다")
    else:
        bad("핸드폰만 시트에 전화번호가 그대로 남았습니다 — "
            "전화를 드롭 트리거에서 뺀 근거가 무너집니다")
    if _blocks and _blocks[0]["total"] == len(_blocks):
        ok("시트 n/N 의 N 이 실제 실린 개수와 맞는다")
    else:
        bad("시트 총개수가 안 맞습니다 — verify_format.py:259 가 ✗ 를 냅니다")

print("\n[날짜 시트 접기]")
from xlsx_to_blocks import sheet_date_key

for title, want in [
    ("상담(0710)", ("상담", "0710")),
    ("상담(529)", ("상담", "0529")),      # 3자리 — 5/29. 실물에 있다
    ("방문(0504)", ("방문", "0504")),
    ("입주 일일업무보고", None),
    ("호실별관리대장", None),
    ("Sheet3", None),
    ("CP(91)", None),                    # 날짜가 아니다 — 산정내역 엑셀의 실제 시트 이름이다
]:
    got = sheet_date_key(title)
    if got == want:
        ok(f"시트 이름 {title!r} → {got}")
    else:
        bad(f"시트 이름 {title!r} → {got}, {want} 여야 합니다")

with _tf.TemporaryDirectory() as _d:
    _p = Path(_d) / "일보.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "입주 일일업무보고"
    _ws["A1"] = "누계"
    for day in ("0710", "0709", "0708"):
        w = _wb.create_sheet(f"상담({day})")
        w["A1"] = f"{day} 상담"
    _wb.create_sheet("호실별관리대장")["A1"] = "동호"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-08-23", "일보.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "상담(0710)" in names and "상담(0709)" not in names and "상담(0708)" not in names:
        ok("가장 최근 날짜 시트만 남았다")
    else:
        bad(f"날짜 시트 접기가 틀렸습니다: {names}")
    if "입주 일일업무보고" in names and "호실별관리대장" in names:
        ok("날짜가 없는 시트는 전부 남는다")
    else:
        bad(f"날짜 없는 시트까지 뺐습니다: {names}")
    folded = _meta.get("folded_date_sheets")
    if folded and {f["name"] for f in folded} == {"상담(0709)", "상담(0708)"}:
        ok("접은 시트가 메타에 적혔다")
    else:
        bad(f"folded_date_sheets 가 틀렸습니다: {folded}")
    if _blocks[0]["total"] == len(_blocks) == 3:
        ok("시트 n/N 의 N 이 3 으로 맞는다")
    else:
        bad(f"총개수 {_blocks[0]['total']} · 실린 것 {len(_blocks)} — 둘 다 3 이어야 합니다")

print("\n[날짜 시트 접기 — 해를 넘긴 경우]")

with _tf.TemporaryDirectory() as _d:
    # 파일 날짜 2026-01-07. 상담(1230) 은 2025-12-30, 상담(0105) 은 2026-01-05 —
    # 문자열만 비교하면 "1230" > "0105" 라 12월이 최신으로 잘못 뽑힌다.
    _p = Path(_d) / "일보_연말연시.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "누계"
    _ws["A1"] = "누계"
    for day in ("1230", "0105"):
        w = _wb.create_sheet(f"상담({day})")
        w["A1"] = f"{day} 상담"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-01-07", "일보_연말연시.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "상담(0105)" in names and "상담(1230)" not in names:
        ok("해를 넘겨도 파일 날짜 기준 최신(0105)만 남는다")
    else:
        bad(f"해를 넘긴 정렬이 틀렸습니다: {names}")
    folded = _meta.get("folded_date_sheets")
    if folded and any(f["name"] == "상담(1230)" and f["kept"] == "상담(0105)" for f in folded):
        ok("상담(1230)이 상담(0105)로 대체됐다고 메타에 적혔다")
    else:
        bad(f"folded_date_sheets 가 틀렸습니다: {folded}")

print("\n[날짜 시트 접기 — 기존 7월 사례 회귀 방지]")

with _tf.TemporaryDirectory() as _d:
    # 파일 날짜 2026-07-13, 시트는 전부 그 이전 — 연도 보정이 붙어도 결과가
    # 그대로여야 한다(리뷰가 지목한 회귀 방지 대상).
    _p = Path(_d) / "일보_7월.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "누계"
    _ws["A1"] = "누계"
    for day in ("0710", "0709", "0408"):
        w = _wb.create_sheet(f"상담({day})")
        w["A1"] = f"{day} 상담"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-07-13", "일보_7월.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "상담(0710)" in names and "상담(0709)" not in names and "상담(0408)" not in names:
        ok("기존 7월 사례는 연도 보정 뒤에도 그대로 상담(0710)이 남는다")
    else:
        bad(f"기존 7월 사례가 달라졌습니다: {names}")

print("\n[날짜 시트 접기 — 3자리·4자리 혼합]")

with _tf.TemporaryDirectory() as _d:
    # 상담(529)(5/29, 3자리)와 상담(0710)(7/10, 4자리)이 같은 앞말 그룹에 섞여 있다.
    _p = Path(_d) / "일보_자릿수혼합.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "누계"
    _ws["A1"] = "누계"
    _wb.create_sheet("상담(529)")["A1"] = "529 상담"
    _wb.create_sheet("상담(0710)")["A1"] = "0710 상담"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-08-23", "일보_자릿수혼합.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "상담(0710)" in names and "상담(529)" not in names:
        ok("3자리·4자리가 섞여도 더 최근(0710)만 남는다")
    else:
        bad(f"자릿수 혼합 접기가 틀렸습니다: {names}")

print("\n[날짜 시트 접기 — 한 장뿐이면 안 접는다]")

with _tf.TemporaryDirectory() as _d:
    _p = Path(_d) / "일보_한장.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "누계"
    _ws["A1"] = "누계"
    _wb.create_sheet("상담(0710)")["A1"] = "0710 상담"
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-08-23", "일보_한장.xlsx", 1000)
    names = [b["name"] for b in _blocks]
    if "상담(0710)" in names:
        ok("같은 앞말이 한 장뿐이면 접지 않는다")
    else:
        bad(f"한 장뿐인데 사라졌습니다: {names}")
    if not _meta.get("folded_date_sheets"):
        ok("한 장뿐이면 folded_date_sheets 도 비어 있다")
    else:
        bad(f"접지 않았어야 하는데 메타에 남았습니다: {_meta.get('folded_date_sheets')}")

print("\n[안 가리기로 한 열 — NOT_PERSONAL_HEADERS]")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "감정.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "물건정보"
    # 3열은 이름이 아니라 생년월일로 둔다 — 이름 열은 이제 안 가리므로(WHK 결정
    # 2026-08-26), 여기서는 NOT_PERSONAL_HEADERS 예외가 다른 마스킹 열까지
    # 번지지 않는지를 여전히 가려지는 낱말로 시험한다.
    _ws.append(["순번", "물건지 주소", "생년월일", "주민등록 주소1", "감정평가금액"])
    _ws.append([1, "서울 ○○구 ○○동 000-0", "1970-01-01", "서울 강남구 …", 279000000])
    _wb.save(_p)

    _blocks, _meta = build_blocks(_p, "2026-08-25", "감정.xlsx", 1000)
    _body = _blocks[0]["body"]
    if "서울 ○○구 ○○동 000-0" in _body:
        ok("물건지 주소는 그대로 실린다")
    else:
        bad("물건지 주소가 가려졌습니다 — 예외가 안 걸렸습니다")
    if "1970-01-01" not in _body:
        ok("같은 표의 생년월일은 그대로 가려진다")
    else:
        bad("생년월일이 샜습니다 — 예외가 너무 넓습니다")
    if "서울 강남구" not in _body:
        ok("주민등록 주소1 은 그대로 가려진다 (부분 일치라 예외 아님)")
    else:
        bad("주민등록 주소가 샜습니다 — 예외가 부분 일치로 걸렸습니다")
    if _meta["masked"]["not_personal"] == ["물건지 주소"]:
        ok("안 가린 열이 메타 masked.not_personal 에 남는다")
    else:
        bad(f"메타에 안 남았습니다: {_meta['masked'].get('not_personal')}")
    if _meta["masked"]["columns"] == ["생년월일", "주민등록 주소1"]:
        ok("가린 열 목록에는 물건지 주소가 없다")
    else:
        bad(f"가린 열 목록이 이상합니다: {_meta['masked']['columns']}")

print("[37/45] `주민등록번호` 는 개인정보 열로 본다 (`대상자` 는 이제 이름 열로 열려 있다)")
# 2026-08-25 실측 — `사업장라/어느 현황표` 의 머리글(5행)이 이 모양인데
# **적중이 1개뿐이라**(`소유부동산 주소`) 문턱 2를 못 넘어 시트가 통째로 빠졌다.
# 낱말 목록이 부분 문자열로 보는데 글자가 어긋나서다:
#   "주민번호" in "주민등록번호" → False    (목록에 `주민번호` 만 있었다)
#   "이름"    in "대상자"        → False    (이름 열 낱말에 `대상자` 가 없었다)
# **막힌 것은 운이었다.** 낱말이 하나만 더 걸려 머리글이 잡혔다면 시트는 실렸을
# 것이다. `대상자` 는 그 뒤 `NAME_COLUMN_WORDS` 로 옮겨져 지금은 **의도적으로**
# 안 가린다(WHK 결정 2026-08-26) — 여전히 가려야 하는 것은 `주민등록번호` 다.
with tempfile.TemporaryDirectory() as td:
    _p = Path(td) / "가압류.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "가압류신청"
    _ws.append(["NO", "대상자", "동호수", "주민등록번호\n/법인번호", "소유부동산 \n주소"])
    # **이름·생년·주소는 전부 지어낸 값이다.** 실물을 시험에 적으면 사본이 하나 더
    # 생기고 그것도 이력에 남는다 (아래 [43/45] 의 주민번호와 같은 이유). 이 시험이
    # 재는 것은 이름이 아니라 **모양**이라, 지어낸 값으로 같은 것을 잰다.
    _ws.append([1, "홍길동", "0103-0801", "900101", "○○도 △△군 □□읍 ◇◇리"])
    _wb.save(_p)

    _row = find_header_row(_ws)
    if _row == 1:
        ok("머리글 행을 찾는다 (적중 2개 이상)")
    else:
        bad(f"머리글 행을 못 찾았습니다: {_row} — 시트가 통째로 빠집니다")

    _blocks, _meta = build_blocks(_p, "2026-07-01", "가압류.xlsx", 1000)
    if not _blocks:
        bad("시트가 통째로 빠졌습니다 — 머리글을 못 찾은 것입니다")
    else:
        _body = _blocks[0]["body"]
        if "홍길동" in _body:
            ok("`대상자` 열의 이름은 이제 안 가린다 (WHK 결정 2026-08-26)")
        else:
            bad("`대상자` 열의 이름이 사라졌습니다 — 이제는 열어야 합니다")
        if "900101" not in _body:
            ok("`주민등록번호` 열이 가려진다")
        else:
            bad("`주민등록번호` 열이 샜습니다")
        if "○○도 △△군" not in _body:
            ok("`소유부동산 주소` 열도 그대로 가려진다")
        else:
            bad("주소가 샜습니다")

print("[38/45] 유령 셀이 openpyxl 의 max_row 를 끌어올린다 (전제 확인)")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ghost.xlsx"
    make_ghost_book(p, ghost_row=500)
    _ws = openpyxl.load_workbook(p, data_only=True)["현황"]
    if _ws.max_row == 500:
        ok("max_row 가 값이 있는 3 이 아니라 유령 셀의 500 이다")
    else:
        bad(f"전제가 깨졌습니다 — max_row 가 {_ws.max_row} 입니다")

print("[39/45] data_bounds 는 값이 있는 마지막 행·열을 낸다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ghost.xlsx"
    make_ghost_book(p, ghost_row=500)
    _ws = openpyxl.load_workbook(p, data_only=True)["현황"]
    if data_bounds(_ws) == (3, 2):
        ok("(3, 2) — 유령 셀을 안 센다")
    else:
        bad(f"data_bounds 가 {data_bounds(_ws)} 를 냈습니다 (3, 2) 여야 합니다")

print("[40/45] 오른쪽 유령 셀도 안 센다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ghost.xlsx"
    make_ghost_book(p, ghost_row=3, ghost_col="Z")
    _ws = openpyxl.load_workbook(p, data_only=True)["현황"]
    if data_bounds(_ws) == (3, 2):
        ok("(3, 2) — Z열 유령 셀을 안 센다")
    else:
        bad(f"data_bounds 가 {data_bounds(_ws)} 를 냈습니다")

print("[41/45] 값이 하나도 없는 시트는 (0, 0)")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "empty.xlsx"
    _wb = openpyxl.Workbook()
    _wb.active.title = "빈시트"
    _wb.save(p)
    _ws = openpyxl.load_workbook(p, data_only=True)["빈시트"]
    if data_bounds(_ws) == (0, 0):
        ok("(0, 0)")
    else:
        bad(f"data_bounds 가 {data_bounds(_ws)} 를 냈습니다")

print("[42/45] 유령 셀 때문에 행 상한을 넘던 시트가 이제 실린다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ghost.xlsx"
    make_ghost_book(p, ghost_row=500)
    _blocks, _meta = build_blocks(p, "2026-08-26", "현황.xlsx", 100)
    if len(_blocks) == 1:
        ok("시트가 빠지지 않았다")
    else:
        bad(f"시트 {len(_blocks)}개 — 1개여야 합니다. 빠진 것: {_meta.get('skipped_sheets')}")
    if not _meta.get("skipped_sheets"):
        ok("상한 초과로 뺀 시트가 없다")
    else:
        bad(f"상한 초과로 뺐습니다: {_meta['skipped_sheets']}")
    if _blocks and "82538.6" in _blocks[0]["body"]:
        ok("머리 표의 대출잔액이 실렸다")
    else:
        bad("표 내용이 안 실렸습니다")

print("[43/45] 꼬리 빈 열이 표에 안 붙는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ghost.xlsx"
    make_ghost_book(p, ghost_row=3, ghost_col="Z")
    _ws = openpyxl.load_workbook(p, data_only=True)["현황"]
    _t = sheet_to_table(_ws)
    # 머리글 행의 <th> 가 정확히 2개여야 한다 (구 분 · 금액)
    if _t.count("<th>") == 2:
        ok("<th> 가 2개 — C열부터 Z열까지의 빈 칸이 안 붙었다")
    else:
        bad(f"<th> 가 {_t.count('<th>')}개입니다 — 2개여야 합니다")

print("[44/45] 값이 없는 셀에 달린 주석은 경계 밖이어도 그대로 실린다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "memo.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "현황"
    _ws["A1"] = "구 분"
    _ws["B1"] = "금액"
    _ws["A2"] = "대출잔액"
    _ws["B2"] = 100
    # 값이 없는 셀에 메모만 단다 — data_bounds 의 경계(2행) 밖이다
    _ws["A5"].comment = openpyxl.comments.Comment("EOD 2/3 초일산입", "심현준")
    _wb.save(p)
    _blocks, _meta = build_blocks(p, "2026-08-26", "메모.xlsx", 1000)
    if _meta.get("comments") == 1:
        ok("경계 밖 주석 1건이 세어졌다")
    else:
        bad(f"주석이 {_meta.get('comments')}건입니다 — 1건이어야 합니다")
    if _blocks and "초일산입" in _blocks[0]["body"]:
        ok("본문 「시트 메모」 절에 실렸다")
    else:
        bad("주석 본문이 안 실렸습니다")

print("[45/45] 유령 셀이 있어도 개인정보 열 마스킹이 그대로 걸린다 (이름은 이제 안 가린다)")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "mask.xlsx"
    _wb = openpyxl.Workbook()
    _ws = _wb.active
    _ws.title = "명단"
    _ws["A1"] = "대상자"
    _ws["B1"] = "주민등록번호"
    _ws["C1"] = "금액"
    _ws["A2"] = "김철수"
    # 구분자 없는 모양이어야 한다 — `900101-1234567` 은 열 마스킹이 죽어도
    # `mask_inline` 이 잡아 이 판정이 초록으로 남는다 (2026-09-05 되돌리기 대조).
    _ws["B2"] = "9001011234567"
    _ws["C2"] = 500
    _wb.save(p)
    import re as _re
    import zipfile as _zip
    _g = Path(td) / "mask_ghost.xlsx"
    with _zip.ZipFile(p) as _zi, _zip.ZipFile(_g, "w", _zip.ZIP_DEFLATED) as _zo:
        for _it in _zi.infolist():
            _d = _zi.read(_it.filename)
            if _it.filename == "xl/worksheets/sheet1.xml":
                _s = _d.decode("utf-8").replace(
                    "</sheetData>", '<row r="400"><c r="A400"/></row></sheetData>')
                _d = _s.encode("utf-8")
            _zo.writestr(_it, _d)
    _blocks, _meta = build_blocks(_g, "2026-08-26", "명단.xlsx", 100)
    _body = _blocks[0]["body"] if _blocks else ""
    if "김철수" in _body and "900101" not in _body:
        ok("이름은 남고 주민등록번호는 가려졌다")
    else:
        bad("유령 셀이 있는 시트에서 마스킹이 어긋났습니다")
    if "주민등록번호" in sorted(_meta["masked"]["columns"]):
        ok("메타에 가린 열이 적혔다")
    else:
        bad(f"masked.columns 가 {_meta['masked']['columns']} 입니다")

print("[46] 값 바닥·구조물 바닥·마지막 행을 잰다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "g.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    with zipfile.ZipFile(p) as z:
        last_val, last_anchor, last_row, unnumbered = _sheet_bottoms(z, "xl/worksheets/sheet1.xml")
    if last_val == 3:
        ok("값 바닥 3 — 유령 행을 안 센다")
    else:
        bad(f"값 바닥이 {last_val} 입니다 (3 이어야 합니다)")
    if last_row == 349:
        ok("마지막 <row> 는 349")
    else:
        bad(f"마지막 <row> 가 {last_row} 입니다 (349 여야 합니다)")
    if last_anchor <= 3:
        ok(f"구조물 바닥 {last_anchor} — 병합셀도 메모도 없다")
    else:
        bad(f"구조물 바닥이 {last_anchor} 입니다 (3 이하여야 합니다)")
    if unnumbered is False:
        ok("모든 <row> 에 r 이 있으니 네 번째 값은 False")
    else:
        bad(f"`r` 없는 행 표시가 {unnumbered!r} 입니다 (False 여야 합니다)")

print("[47] 병합셀과 셀 메모가 구조물 바닥을 끌어올린다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "m.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, merge_to=120)
    with zipfile.ZipFile(p) as z:
        _v, anchor_merge, _r, _u = _sheet_bottoms(z, "xl/worksheets/sheet1.xml")
    if anchor_merge == 120:
        ok("병합셀 D1:D120 → 구조물 바닥 120")
    else:
        bad(f"병합셀이 있는데 구조물 바닥이 {anchor_merge} 입니다 (120 이어야 합니다)")

    p2 = Path(td) / "c.xlsx"
    make_ghost_rows_book(p2, n_ghost=300, first_ghost=50, comment_at=200)
    with zipfile.ZipFile(p2) as z:
        _v, anchor_cmt, _r, _u = _sheet_bottoms(z, "xl/worksheets/sheet1.xml")
    if anchor_cmt == 200:
        ok("A200 셀 메모 → 구조물 바닥 200")
    else:
        bad(f"메모가 있는데 구조물 바닥이 {anchor_cmt} 입니다 (200 이어야 합니다)")

print("[47.5] 없는 부속을 가리키는 rels 는 죽지 않고 stderr 에 경고를 남긴다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "c.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, comment_at=200)
    p2 = Path(td) / "broken.xlsx"
    # comments rels 의 Target 을 zip 안에 없는 이름으로 바꿔치기한다.
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(p2, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/_rels/sheet1.xml.rels":
                data = data.decode("utf-8").replace(
                    "/xl/comments/comment1.xml", "/xl/comments/없음.xml"
                ).encode("utf-8")
            zout.writestr(item, data)

    import contextlib
    import io

    stderr_buf = io.StringIO()
    try:
        with zipfile.ZipFile(p2) as z:
            with contextlib.redirect_stderr(stderr_buf):
                _sheet_bottoms(z, "xl/worksheets/sheet1.xml")
        ok("없는 부속을 가리켜도 예외 없이 돌아왔다")
    except Exception as e:
        bad(f"없는 부속을 만나자 죽었습니다: {e!r}")
    warned = stderr_buf.getvalue()
    if "없음.xml" in warned:
        ok("못 찾은 Target 이름이 경고에 찍혔다")
    else:
        bad(f"stderr 에 경고가 없거나 대상 이름이 안 보입니다: {warned!r}")

print("[47.6] 깨진 시트 rels 도 죽지 않고 stderr 에 경고를 남긴다")
# 같은 결함 갈래의 셋째 자리다 — 여기서 조용히 []를 돌려주면 그 시트의 메모·표
# 참조가 **하나도** 안 세어져 구조물 바닥이 낮게 나오고, 아직 쓰이는 행이 잘린다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "c.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, comment_at=200)
    p2 = Path(td) / "brokenrels.xlsx"
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(p2, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/_rels/sheet1.xml.rels":
                data = b"<Relationships><Relationship Target=</Relationships"
            zout.writestr(item, data)

    import contextlib as _ctx2
    import io as _io2

    stderr_buf = _io2.StringIO()
    parts = None
    try:
        with zipfile.ZipFile(p2) as z:
            with _ctx2.redirect_stderr(stderr_buf):
                parts = _sheet_related_parts(z, "xl/worksheets/sheet1.xml")
        ok("깨진 rels 를 만나도 예외 없이 돌아왔다")
    except Exception as e:
        bad(f"깨진 rels 를 만나자 죽었습니다: {e!r}")
    if parts == []:
        ok("빈 목록을 돌려줬다")
    else:
        bad(f"{parts} 를 돌려줬습니다 ([] 여야 합니다)")
    warned = stderr_buf.getvalue()
    if "경고" in warned and "sheet1.xml.rels" in warned:
        ok("못 읽은 rels 이름이 경고에 찍혔다")
    else:
        bad(f"stderr 에 경고가 없거나 rels 이름이 안 보입니다: {warned!r}")

print("[47.7] 소문자로 적힌 참조(a1)도 구조물 바닥으로 센다")
# 틀리는 방향이 안전한 쪽이 아니다 — 못 알아보면 바닥이 **낮게** 나와 아직 쓰이는
# 행이 잘린다.
if _row_refs("a1:b120") == [1, 120] and _row_refs("A1:B120") == [1, 120]:
    ok("대문자든 소문자든 같은 행 번호를 낸다")
else:
    bad(f"소문자 {_row_refs('a1:b120')} · 대문자 {_row_refs('A1:B120')}")

print("[48] 값 없는 꼬리 행만 잘라낸다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "g.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    with zipfile.ZipFile(p) as z:
        raw = z.read("xl/worksheets/sheet1.xml")
        before = _count_value_cells(z, "xl/worksheets/sheet1.xml")
    cut, n_dropped, n_hidden_dropped = _strip_rows(raw, 3)
    n_rows = cut.count(b"<row ")
    if n_rows == 3:
        ok("<row> 가 3개만 남았다")
    else:
        bad(f"<row> 가 {n_rows}개 남았습니다 (3 이어야 합니다)")
    if b'ref="A1:B3"' in cut:
        ok("dimension 끝 행이 3 으로 고쳐졌다")
    else:
        bad("dimension 이 안 고쳐졌습니다")
    if n_dropped == 300 and n_hidden_dropped == 0:
        ok("버린 행 수는 <row> 번호 차가 아니라 실제로 지운 개수(300) — 숨긴 것 0")
    else:
        bad(f"버린 행 {n_dropped}개 · 그중 숨긴 것 {n_hidden_dropped}개 "
            f"(300 · 0 이어야 합니다)")

print("[49] 행이 번호순이 아니어도 값이 안 없어진다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "r.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, out_of_order=True)
    with zipfile.ZipFile(p) as z:
        raw = z.read("xl/worksheets/sheet1.xml")
    cut, _n_dropped, _n_hidden_dropped = _strip_rows(raw, 3)
    if cut.count(b"<row ") == 3 and b"82538.6" in cut and b"9747.4" in cut:
        ok("역순으로 쓰인 유령 행 300줄이 값 3행을 안 건드리고 빠졌다")
    else:
        bad(f"역순 파일에서 <row> {cut.count(b'<row ')}개 · 값 보존 "
            f"{b'82538.6' in cut and b'9747.4' in cut}")

print("[50] 시트 이름을 zip 항목 이름에서 되찾는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_ghost_rows_book(p, n_ghost=10, first_ghost=50)
    with zipfile.ZipFile(p) as z:
        titles = _sheet_titles(z)
    if titles.get("xl/worksheets/sheet1.xml") == "현황":
        ok("sheet1.xml → '현황'")
    else:
        bad(f"시트 이름이 {titles} 입니다")

print("[50.5] workbook.xml 이 깨지면 죽지 않고 stderr 에 경고를 남긴다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "t.xlsx"
    make_ghost_rows_book(p, n_ghost=10, first_ghost=50)
    p2 = Path(td) / "broken.xlsx"
    # xl/workbook.xml 을 깨진 XML 로 바꿔치기한다.
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(p2, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/workbook.xml":
                data = b"<not><valid<xml"
            zout.writestr(item, data)

    import contextlib
    import io

    stderr_buf = io.StringIO()
    try:
        with zipfile.ZipFile(p2) as z:
            with contextlib.redirect_stderr(stderr_buf):
                titles = _sheet_titles(z)
        ok("깨진 workbook.xml 을 만나도 예외 없이 돌아왔다")
    except Exception as e:
        bad(f"깨진 workbook.xml 을 만나자 죽었습니다: {e!r}")
        titles = None
    if titles == {}:
        ok("빈 매핑을 돌려줬다")
    else:
        bad(f"{titles} 를 돌려줬습니다 ({{}} 여야 합니다)")
    warned = stderr_buf.getvalue()
    if "workbook.xml" in warned and "경고" in warned:
        ok("workbook.xml 을 못 읽었다는 경고가 stderr 에 찍혔다")
    else:
        bad(f"stderr 에 경고가 없거나 내용이 부족합니다: {warned!r}")

print("[51] 유령 행 300줄이 붙은 파일이 정상 변환되고 meta 에 적힌다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "g.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    blocks, meta = build_blocks(p, "2026-08-27", "g.xlsx", 1000)
    body = blocks[0]["body"] if blocks else ""
    if "82538.6" in body or "82,539" in body:
        ok("값이 그대로 실렸다")
    else:
        bad(f"값이 안 실렸습니다: {body[:200]!r}")
    tr = meta.get("trimmed_sheets") or []
    if len(tr) == 1 and tr[0]["name"] == "현황" and tr[0]["kept"] == 3 and tr[0]["dropped"] == 300:
        ok("trimmed_sheets 에 현황(3행까지 남기고 300행) — kept 는 행 번호다")
    else:
        bad(f"trimmed_sheets 가 {tr} 입니다")
    if "untrimmed_sheets" not in meta:
        ok("못 자른 시트가 없어 untrimmed_sheets 키가 없다")
    else:
        bad(f"untrimmed_sheets 가 있습니다: {meta['untrimmed_sheets']}")

print("[52] 값 있는 셀 개수가 자르기 전후 같다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "g.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    with zipfile.ZipFile(p) as z:
        before = _count_value_cells(z, "xl/worksheets/sheet1.xml")
        raw = z.read("xl/worksheets/sheet1.xml")
    cut, _n_dropped, _n_hidden_dropped = _strip_rows(raw, 3)
    after = _count_value_cells_bytes(cut)
    if before == after == 6:
        ok(f"값 있는 셀 {before} = {after}")
    else:
        bad(f"값 있는 셀이 {before} → {after} 로 달라졌습니다 (둘 다 6 이어야 합니다)")

print("[53] 병합셀이 값 바닥 아래에 있으면 거기까지 남기고 병합 자체가 잘린 사본에도 남는다")
# **`rowspan="120"` 이 렌더된 표에 뜨는지는 시험하지 않는다.** `sheet_to_table` 은
# `data_bounds` 가 낸 값 있는 범위만 그리므로(이 시트는 (3,2)) 값이 하나도 없는
# D 열은 자르기와 무관하게 애초에 그려지지 않는다 — 이 병합은 D1:D120 에 값을 안
# 준 시험 설정이라 렌더 여부로는 「병합이 살아남았나」를 확인할 수 없다. 실제로
# 지켜야 할 불변은 「자른 사본을 열어도 병합 범위 자체가 그대로 있나」다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "m.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, merge_to=120)
    blocks, meta = build_blocks(p, "2026-08-27", "m.xlsx", 1000)
    tr = meta.get("trimmed_sheets") or []
    if len(tr) == 1 and tr[0]["kept"] == 120:
        ok("병합셀 바닥 120 까지 남겼다")
    else:
        bad(f"trimmed_sheets 가 {tr} 입니다 (kept 가 120 이어야 합니다)")
    with tempfile.TemporaryDirectory() as td2:
        load_path, _tr, _un, _hd = trim_ghost_rows(p, td2)
        wb2 = openpyxl.load_workbook(load_path, data_only=True)
        ranges = [str(r) for r in wb2["현황"].merged_cells.ranges]
    if "D1:D120" in ranges:
        ok("잘린 사본을 열어도 병합 범위 D1:D120 이 그대로 있다")
    else:
        bad(f"병합 범위가 사라졌습니다: {ranges}")

print("[54] 셀 메모가 값 바닥 아래에 있어도 살아남는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "c.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, comment_at=200)
    blocks, meta = build_blocks(p, "2026-08-27", "c.xlsx", 1000)
    body = blocks[0]["body"] if blocks else ""
    if "아래쪽 메모" in body:
        ok("A200 의 메모가 「시트 메모」 절에 실렸다")
    else:
        bad("메모가 사라졌습니다")

print("[55] 구조물이 꼬리 끝까지면 안 자르고 사유와 함께 남는다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "f.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, merge_to=349)
    blocks, meta = build_blocks(p, "2026-08-27", "f.xlsx", 1000)
    un = meta.get("untrimmed_sheets") or []
    if len(un) == 1 and un[0]["name"] == "현황" and "349" in un[0]["why"]:
        ok(f"untrimmed_sheets 에 사유가 남았다: {un[0]['why']}")
    else:
        bad(f"untrimmed_sheets 가 {un} 입니다")
    if "trimmed_sheets" not in meta:
        ok("자른 것이 없어 trimmed_sheets 키가 없다")
    else:
        bad(f"trimmed_sheets 가 있습니다: {meta['trimmed_sheets']}")

print("[55.5] `r` 이 없는 <row> 가 있으면 그 시트는 아예 안 자른다")
# **개수 대조로는 못 막는 유일한 갈래다.** `r` 이 없는 행은 앞에 몇 행이 있었나로
# 번호가 정해지므로, 앞쪽 행을 지우면 값은 그대로인 채 **다른 행 번호로 옮겨간다** —
# 값 있는 셀 개수는 전후가 똑같아 안전망이 안 울린다. 그래서 자르기를 아예 거절한다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "u.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50, unnumbered_text="위치행")
    blocks, meta = build_blocks(p, "2026-08-27", "u.xlsx", 1000)
    un = meta.get("untrimmed_sheets") or []
    hit = [s for s in un if s["name"] == "현황"]
    if hit and "`r`" in hit[0]["why"] and "밀린다" in hit[0]["why"]:
        ok(f"untrimmed_sheets 에 거절 사유가 남았다: {hit[0]['why']}")
    else:
        bad(f"untrimmed_sheets 가 {un} 입니다 (`r` 없는 행이라는 사유가 있어야 합니다)")
    if hit and hit[0]["ghost_rows"] == 300:
        ok("ghost_rows 는 값 바닥(3행) 아래의 <row> 300개 — 다른 갈래와 같은 뜻이다")
    else:
        bad(f"ghost_rows 가 {hit[0]['ghost_rows'] if hit else None} 입니다 (300 이어야 합니다)")
    if "trimmed_sheets" not in meta:
        ok("거절한 시트는 trimmed_sheets 에 안 든다")
    else:
        bad(f"trimmed_sheets 가 있습니다: {meta['trimmed_sheets']}")

    with tempfile.TemporaryDirectory() as td2:
        load_path, _tr, _un, _hd = trim_ghost_rows(p, td2)
        same = Path(load_path) == Path(p)
        wb_u = openpyxl.load_workbook(load_path, data_only=True)
        vals = {c.value for row in wb_u["현황"].iter_rows() for c in row
                if c.value not in (None, "")}
    if same:
        ok("자를 것이 없으니 사본을 안 만들고 원본을 그대로 연다")
    else:
        bad(f"사본을 만들었습니다: {load_path}")
    need = {"구 분", "금액", "대출잔액", 82538.6, "보증", 9747.4, "위치행"}
    if need <= vals:
        ok("원래 값이 하나도 안 없어졌다 (`r` 없는 행의 '위치행' 포함)")
    else:
        bad(f"없어진 값이 있습니다: {sorted(map(str, need - vals))}")

print("[56] 유령 행이 정말로 하나도 없는 파일은 결과가 그대로고 두 키가 다 없다")
# **`make_book()` 은 이 시험에 안 맞는다** — 그 안의 「이자계산」 시트는 4행이
# `<row r="4" hidden="1"></row>` 로, 값도 서식 참조도 없는 진짜 유령 꼬리 행이다
# ([56.5]가 그 사실로 만드는 시험). 그래서 여기는 유령 행이 전혀 없는 새 파일로
# 다시 만든다 — 시험 이름과 실제로 확인하는 것이 어긋나면 안 된다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "n.xlsx"
    wb0 = openpyxl.Workbook()
    ws0 = wb0.active
    ws0.title = "정상"
    ws0["A1"] = "구 분"
    ws0["B1"] = "금액"
    ws0["A2"] = "하나"
    ws0["B2"] = 1
    wb0.save(p)
    blocks, meta = build_blocks(p, "2026-08-27", "n.xlsx", 1000)
    if "trimmed_sheets" not in meta and "untrimmed_sheets" not in meta:
        ok("진짜 유령 행이 없는 엑셀에는 두 키가 다 안 생긴다")
    else:
        bad(f"키가 생겼습니다: trimmed={meta.get('trimmed_sheets')} "
            f"untrimmed={meta.get('untrimmed_sheets')}")
    # **「결과가 그대로」까지 확인한다** — 키가 없다는 것만 봐서는 시험 이름의 절반이
    # 안 지켜진다. 시트 하나에 값 네 개가 그대로 실렸나를 본문에서 본다.
    body = blocks[0]["body"] if blocks else ""
    if (len(blocks) == 1 and meta["sheets"] == ["정상"]
            and all(t in body for t in ("구 분", "금액", "하나", ">1<"))):
        ok("시트 1개 · 값 네 개가 표에 그대로 실렸다")
    else:
        bad(f"블록 {len(blocks)}개 · sheets={meta['sheets']} · 본문 {body[:200]!r}")

print("[56.5] 숨긴 빈 꼬리 행이 잘려도 hidden_rows 는 자르기 전 숫자를 그대로 보고한다")
# WHK 판정 2026-08-27: 값 없는 숨긴 꼬리 행은 그대로 잘라내되(트리밍은 계속한다),
# 그로 인해 `meta["hidden_rows"]` 가 조용히 줄어들면 안 된다. `make_book()` 의
# 「이자계산」 시트(3행 내용 있는 숨긴 행 + 4행 빈 숨긴 행)로 그 약속을 확인한다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "h.xlsx"
    make_book(p)
    blocks, meta = build_blocks(p, "2026-08-27", "h.xlsx", 1000)
    tr = meta.get("trimmed_sheets") or []
    if len(tr) == 1 and tr[0]["name"] == "이자계산" and tr[0]["kept"] == 3 and tr[0]["dropped"] == 1:
        ok("이자계산의 숨긴 빈 꼬리 행(4행)이 잘렸다고 trimmed_sheets 에 남았다")
    else:
        bad(f"trimmed_sheets 가 {tr} 입니다 (이자계산 · kept 3 · dropped 1 이어야 합니다)")
    if (meta["hidden_rows"], meta["hidden_rows_with_content"]) == (2, 1):
        ok("잘렸어도 숨긴 행 2 · 내용 있는 것 1 — 자르기 전 숫자를 지킨다")
    else:
        bad(f"숨긴 행 {meta['hidden_rows']} · 내용 있는 것 {meta['hidden_rows_with_content']} "
            f"— (2, 1) 이어야 합니다 (자르기가 사람이 보는 숫자를 바꿨다)")

print("[56.6] 시트 이름을 못 되찾아도 잘려 나간 숨긴 행이 hidden_rows 에서 안 샌다")
# `_sheet_titles` 가 {} 를 내면(workbook.xml 이 깨졌거나 r:id 가 rels 에 없으면)
# `hidden_dropped` 의 키가 zip 항목 이름이라 `ws.title` 과 영영 안 맞는다. 그때
# 조용히 빠지면 [56.5] 가 지키는 숫자가 경고 한 줄 뒤에서 깨진다.
real_sheet_titles = xlsx_to_blocks._sheet_titles
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "h2.xlsx"
    make_book(p)
    xlsx_to_blocks._sheet_titles = lambda z: {}
    try:
        blocks, meta = build_blocks(p, "2026-08-27", "h2.xlsx", 1000)
    finally:
        xlsx_to_blocks._sheet_titles = real_sheet_titles
    tr = meta.get("trimmed_sheets") or []
    if len(tr) == 1 and tr[0]["name"].startswith("xl/worksheets/"):
        ok(f"이름을 못 되찾아 zip 항목 이름으로 적혔다: {tr[0]['name']}")
    else:
        bad(f"trimmed_sheets 가 {tr} 입니다 (zip 항목 이름이어야 합니다)")
    if (meta["hidden_rows"], meta["hidden_rows_with_content"]) == (2, 1):
        ok("이름이 안 맞아도 숨긴 행 2 · 내용 있는 것 1 — 개수는 지킨다")
    else:
        bad(f"숨긴 행 {meta['hidden_rows']} · 내용 있는 것 {meta['hidden_rows_with_content']} "
            f"— (2, 1) 이어야 합니다 (이름 못 찾은 시트 몫이 샜다)")

print("[57] 잘린 시트에서도 마스킹과 상한 판정이 그대로 걸린다")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "k.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    _b, meta = build_blocks(p, "2026-08-27", "k.xlsx", 2)
    sk = meta.get("skipped_sheets") or []
    if sk and sk[0]["rows"] == 3:
        ok("상한 판정이 자른 뒤의 3행을 본다 (유령 349 가 아니다)")
    else:
        bad(f"skipped_sheets 가 {sk} 입니다 (rows 3 이어야 합니다)")

print("[58] 자르기가 값을 지우면 (안전망) 그 시트를 원본으로 되돌리고 hidden_rows 도 안 흔든다")
# `trim_ghost_rows`의 `before != after` 대조가 `_strip_rows` 결함과 「값이 조용히
# 사라진다」 사이에 선 유일한 안전망인데, 지금까지 이 경로를 실제로 통과하는
# 시험이 없었다([55]는 자르기 전 판정에서 걸러지는 다른 가지다). `_strip_rows`
# 를 몽키패치해 「진짜로 자른 뒤 값 있는 행(3행, 보증 9747.4)까지 한 번 더
# 지우는」 결함 있는 버전으로 바꿔치기해 그 가지를 직접 밟는다.
real_strip_rows = xlsx_to_blocks._strip_rows


def _lossy_strip_rows(raw, keep_upto):
    cut, dropped, hidden = real_strip_rows(raw, keep_upto)
    # 정상적으로 자른 결과를 한 번 더 잘라 값이 있는 마지막 행을 지운다 —
    # "자르는 코드가 값 있는 행까지 버렸다"는 결함을 그대로 흉내 낸다.
    lossy_cut, _extra_dropped, _extra_hidden = real_strip_rows(cut, keep_upto - 1)
    return lossy_cut, dropped, hidden


with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "lossy.xlsx"
    make_ghost_rows_book(p, n_ghost=300, first_ghost=50)
    xlsx_to_blocks._strip_rows = _lossy_strip_rows
    try:
        blocks, meta = build_blocks(p, "2026-08-27", "lossy.xlsx", 1000)
    finally:
        xlsx_to_blocks._strip_rows = real_strip_rows  # 뒤에 오는 시험을 안 건드리게 복원

    tr = meta.get("trimmed_sheets") or []
    un = meta.get("untrimmed_sheets") or []
    if not any(s["name"] == "현황" for s in tr):
        ok("훼손된 자르기 결과는 trimmed_sheets 에 안 남는다")
    else:
        bad(f"trimmed_sheets 에 현황이 남아 있습니다: {tr}")
    hit = [s for s in un if s["name"] == "현황"]
    # **낱자로 찾지 않는다** — `"6" in why` 는 「64」 에도 걸려 엉뚱한 값이 들어가도
    # 통과한다. 사유 문장이 실제로 만드는 그 구절을 통째로 본다.
    if hit and "(원본 6 · 자른 뒤 4)" in hit[0]["why"]:
        ok(f"untrimmed_sheets 에 되돌린 사유가 원본·자른 뒤 두 값과 함께 남았다: {hit[0]['why']}")
    else:
        bad(f"untrimmed_sheets 가 {un} 입니다 (원본 6 · 자른 뒤 4 가 사유에 있어야 합니다)")
    body = blocks[0]["body"] if blocks else ""
    if "82538.6" in body and "9747.4" in body:
        ok("되돌린 시트는 원본 그대로 로드돼 두 값이 모두 살아 있다 — 기록만 남긴 게 아니다")
    else:
        bad(f"되돌렸다면서 값이 사라졌습니다: {body[:200]!r}")
    if (meta["hidden_rows"], meta["hidden_rows_with_content"]) == (0, 0):
        ok("되돌리기는 hidden_rows 를 안 건드린다 (이 시트엔 숨긴 행이 없다)")
    else:
        bad(f"hidden_rows={meta['hidden_rows']} "
            f"hidden_rows_with_content={meta['hidden_rows_with_content']} (0, 0 이어야 합니다)")

print("[59] hidden=\"true\" 로 적힌 숨긴 빈 꼬리 행도 잘리기 전 hidden_rows 를 그대로 지킨다")
# ECMA-376 은 불리언을 `1`/`0` 뿐 아니라 `true`/`false` 로도 적을 수 있다.
# openpyxl 은 항상 `1`을 쓰므로, `true` 로 저장한 실물 파일을 흉내 내려면 zip 을
# 손으로 바꿔치기해야 한다.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "truehidden.xlsx"
    wb_th = openpyxl.Workbook()
    ws_th = wb_th.active
    ws_th.title = "시트"
    ws_th["A1"] = "항목"
    ws_th["A2"] = "보이는 행"
    wb_th.save(p)

    with zipfile.ZipFile(p) as zin:
        names = zin.namelist()
        data = {n: zin.read(n) for n in names}
    s = data["xl/worksheets/sheet1.xml"].decode("utf-8")
    s = s.replace("</sheetData>", '<row r="3" hidden="true"></row></sheetData>')
    s = _re.sub(r'<dimension ref="[^"]*"/>', '<dimension ref="A1:A2"/>', s)
    data["xl/worksheets/sheet1.xml"] = s.encode("utf-8")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, data[n])

    blocks, meta = build_blocks(p, "2026-08-27", "truehidden.xlsx", 1000)
    tr = meta.get("trimmed_sheets") or []
    if len(tr) == 1 and tr[0]["name"] == "시트" and tr[0]["kept"] == 2 and tr[0]["dropped"] == 1:
        ok('hidden="true" 인 빈 꼬리 행(3행)이 잘렸다')
    else:
        bad(f"trimmed_sheets 가 {tr} 입니다 (시트 · kept 2 · dropped 1 이어야 합니다)")
    if meta["hidden_rows"] == 1:
        ok('hidden="true" 로 적힌 숨긴 행도 잘리기 전 개수(1)로 세어졌다')
    else:
        bad(f"hidden_rows 가 {meta['hidden_rows']} 입니다 (1 이어야 합니다 — "
            f'"true" 철자를 놓치면 0 으로 조용히 줄어든다)')

sys.exit(1 if FAILED else 0)
