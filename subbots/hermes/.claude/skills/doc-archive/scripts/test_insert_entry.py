#!/usr/bin/env python3
"""
insert_entry.py 시험. **아무 파일도 안 건드린다** — 임시 폴더만 쓴다.

  python .claude/skills/doc-archive/scripts/test_insert_entry.py

종료코드: 0 전부 통과 / 1 실패 있음

## 왜 이 파일이 지금까지 없었나 (Task 3)

`insert_entry.py` 는 시험이 한 개도 없었다. 특히 **엑셀 문서의 「2회차」** —
같은 문서 md 에 이미 1회차 시트들이 들어간 뒤, 다른 날짜에 같은(또는 개정된)
엑셀이 다시 올라와 `insert_entry.py --source "…— 시트 n/N: …"` 조합으로 새
시트들을 붙이는 경로 — 는 자료 저장소를 실측해도 **한 번도 실행된 적이
없었다**(53개 엑셀 문서 전부 회차가 날짜 1개뿐이었다, 2026-09-03 측정 —
`python -c "..."` 로 각 문서의 `ENTRY_RE` 날짜 집합을 세어 확인했다). 처음
도는 날 조용히 깨지면 그 문서의 검색 단위가 갈라지므로, 실물처럼 두 번
불러서 재현한다.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import insert_entry as ie  # noqa: E402

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def check(label, cond, detail=None):
    if cond:
        ok(label)
    else:
        bad(f"{label}" + (f"  {detail!r}" if detail is not None else ""))


# `기간`·`수록 회차` 줄을 미리 넣어 둔다 — `refresh_meta` 는 **있는 값을 고칠 뿐**
# 없는 줄을 새로 만들지는 않는다(실물 문서도 이 두 줄을 처음부터 갖고 있다).
_META = (
    "# [현황표] 테스트 문서\n\n"
    "> **사업장**: 테스트 · **종류**: 현황표\n"
    "> **출처**: Slack #테스트 · 업로드 2026-08-05\n"
    "> **기간**: 2026-08-05 · **수록 회차**: 0건\n"
    "> **열람**: 공개\n"
    "> **원본**: Slack 파일\n"
    "\n---\n"
)


def _sheet_block(date, i, n, name, body):
    return f"**{date} · 파일.xlsx — 시트 {i}/{n}: {name}**\n\n{body}\n"


print("엑셀 문서의 2회차 — 이미 1회차 시트가 들어간 md 에 다른 날짜 시트를 더 붙인다")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "doc.md"
    _p.write_text(_META, encoding="utf-8")

    # 1회차: 2026-08-05, 시트 A·B
    for i, name in enumerate(["A", "B"], start=1):
        block = _sheet_block("2026-08-05", i, 2, name, f"1회차 시트{name} 본문")
        where = ie.insert_entry(_p, "2026-08-05", f"파일.xlsx — 시트 {i}/2: {name}", block, None)
        check(f"1회차 시트{i} 삽입", where is not None, where)

    # 2회차: 2026-08-20, 시트 X·Y·Z (개정본 파일명)
    for i, name in enumerate(["X", "Y", "Z"], start=1):
        block = _sheet_block("2026-08-20", i, 3, name, f"2회차 시트{name} 본문")
        where = ie.insert_entry(
            _p, "2026-08-20", f"파일_v2.xlsx — 시트 {i}/3: {name}", block, None,
        )
        check(f"2회차 시트{i} 삽입", where is not None, where)

    text = _p.read_text(encoding="utf-8")
    all_entries = ie.all_entries(text.split("\n"))
    check("회차가 5건이다 (1회차 2 + 2회차 3)", len(all_entries) == 5, all_entries)

    dates_in_order = [ie.ENTRY_RE.match(l).group(1) for l in text.split("\n") if ie.ENTRY_RE.match(l)]
    check(
        "최신 날짜(2회차)가 위, 오래된 날짜(1회차)가 아래",
        dates_in_order == ["2026-08-20"] * 3 + ["2026-08-05"] * 2,
        dates_in_order,
    )

    names_in_order = [
        l.split("시트")[-1].split(":")[-1].strip().rstrip("*")
        for l in text.split("\n") if ie.ENTRY_RE.match(l)
    ]
    check(
        "같은 회차 안에서는 넣은 순서(시트 번호 순)를 지킨다",
        names_in_order == ["X", "Y", "Z", "A", "B"],
        names_in_order,
    )

    check(
        "새 시트가 옛 시트 본문을 지우거나 덮지 않는다 — 다섯 본문이 다 남아 있다",
        all(f"{r}회차 시트{n} 본문" in text for r, n in
            [("1", "A"), ("1", "B"), ("2", "X"), ("2", "Y"), ("2", "Z")]),
    )

    note = ie.refresh_meta(_p)
    check("refresh_meta 가 회차 5건을 본다", "회차 5건" in note, note)
    check(
        "refresh_meta 가 기간을 1회차~2회차로 넓힌다",
        "2026-08-05 ~ 2026-08-20" in note, note,
    )
    text2 = _p.read_text(encoding="utf-8")
    check(
        "메타의 `기간` 줄이 실제로 넓어진다",
        "**기간**: 2026-08-05 ~ 2026-08-20" in text2, text2,
    )
    check(
        "메타의 `수록 회차` 줄이 5건으로 바뀐다",
        "**수록 회차**: 5건" in text2, text2,
    )


print("\n엑셀 문서의 2회차 — 「## 회차 요약」 표가 있는 시리즈 md 에도 같은 순서로 붙는다")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "series.md"
    _p.write_text(
        "# [현황표] 테스트 시리즈\n\n"
        "> **사업장**: 테스트 · **종류**: 현황표\n"
        "> **열람**: 공개\n"
        "\n---\n\n"
        "## 회차 요약\n\n"
        "| 날짜 | 원본 파일 | 요약 |\n"
        "|---|---|---|\n"
        "\n---\n",
        encoding="utf-8",
    )
    for i, (name, summ) in enumerate([("A", "수금 88%"), ("B", "수금 90%")], start=1):
        block = _sheet_block("2026-08-05", i, 2, name, f"본문{name}")
        ie.insert_entry(_p, "2026-08-05", f"파일.xlsx — 시트 {i}/2: {name}", block, summ)
    for i, (name, summ) in enumerate([("X", "수금 95%")], start=1):
        block = _sheet_block("2026-08-20", i, 1, name, f"본문{name}")
        ie.insert_entry(_p, "2026-08-20", f"파일_v2.xlsx — 시트 {i}/1: {name}", block, summ)

    text = _p.read_text(encoding="utf-8")
    summary_rows = [l for l in text.split("\n") if l.startswith("| 2026-")]
    check("회차 요약 표에 세 행이 모두 들어간다", len(summary_rows) == 3, summary_rows)
    check(
        "회차 요약 표도 최신(2회차)이 위다",
        summary_rows[0].startswith("| 2026-08-20"), summary_rows,
    )
    check(
        "2회차 요약 줄에 개정본 파일명과 요약이 함께 들어간다",
        "파일_v2.xlsx" in summary_rows[0] and "수금 95%" in summary_rows[0],
        summary_rows,
    )


print("\n2회차의 시트가 1회차와 같은 날짜·같은 원본명이면 중복으로 건너뛴다 (already_has)")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "doc.md"
    _p.write_text(_META, encoding="utf-8")
    block = _sheet_block("2026-08-05", 1, 1, "A", "본문A")
    where1 = ie.insert_entry(_p, "2026-08-05", "파일.xlsx — 시트 1/1: A", block, None)
    check("첫 삽입은 된다", where1 is not None, where1)
    where2 = ie.insert_entry(_p, "2026-08-05", "파일.xlsx — 시트 1/1: A", block, None)
    check("같은 날짜·같은 원본명을 다시 넣으면 None(SKIP)", where2 is None, where2)


print("\nCLI — 실물 회차가 재현하는 시나리오를 명령줄로도 돌려 본다")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "doc.md"
    _p.write_text(_META, encoding="utf-8")
    _block = Path(_d) / "block1.md"
    _block.write_text(_sheet_block("2026-08-05", 1, 1, "A", "1회차 본문"), encoding="utf-8")
    r1 = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-08-05",
         "--source", "파일.xlsx — 시트 1/1: A", "--content-file", str(_block)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("CLI 로 1회차 삽입 성공 (종료코드 0)", r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr))

    _block2 = Path(_d) / "block2.md"
    _block2.write_text(_sheet_block("2026-08-20", 1, 1, "X", "2회차 본문"), encoding="utf-8")
    r2 = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-08-20",
         "--source", "파일_v2.xlsx — 시트 1/1: X", "--content-file", str(_block2)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("CLI 로 2회차(개정본) 삽입도 성공 (종료코드 0)", r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr))

    text = _p.read_text(encoding="utf-8")
    check(
        "CLI 경로로도 1·2회차 본문이 둘 다 파일에 남는다",
        "1회차 본문" in text and "2회차 본문" in text, text,
    )

print("\n헤더 검사 — 회차 헤더가 없는 블록은 막힌다 (2026-09-21)")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "doc.md"
    _p.write_text(_META, encoding="utf-8")
    _before = _p.read_text(encoding="utf-8")

    _bad = Path(_d) / "noheader.md"
    _bad.write_text("| 구분 | 금액 |\n|---|---|\n| 계 | 100 |\n", encoding="utf-8")
    rb = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-08-05",
         "--source", "보고.pdf", "--content-file", str(_bad)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("헤더 없는 블록은 종료코드 1", rb.returncode == 1, (rb.returncode, rb.stdout, rb.stderr))
    check("`OK` 를 찍지 않는다", "OK" not in rb.stdout, rb.stdout)
    check("사유에 만들어야 할 헤더 모양이 있다", "2026-08-05 · 보고.pdf" in rb.stderr, rb.stderr)
    check("막힌 회차가 md 에 안 들어갔다", _p.read_text(encoding="utf-8") == _before, None)

    _good = Path(_d) / "withheader.md"
    _good.write_text("**2026-08-05 · 보고.pdf**\n\n본문입니다.\n", encoding="utf-8")
    rg = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-08-05",
         "--source", "보고.pdf", "--content-file", str(_good)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("헤더가 있으면 지금과 똑같이 통과", rg.returncode == 0, (rg.returncode, rg.stdout, rg.stderr))
    check("본문이 실제로 들어갔다", "본문입니다." in _p.read_text(encoding="utf-8"), None)

    # 헤더 모양만 맞고 **값이 어긋나는** 것도 막는다 (2026-09-21 회의적 검증이 잡음)
    _wrongdate = Path(_d) / "wrongdate.md"
    _wrongdate.write_text("**2026-01-15 · 보고.pdf**\n\n딴 날짜 본문.\n", encoding="utf-8")
    rw = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-09-23",
         "--source", "보고.pdf", "--content-file", str(_wrongdate)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("헤더 날짜가 --date 와 다르면 막힌다", rw.returncode == 1, (rw.returncode, rw.stdout))
    check("딴 날짜 본문이 안 들어갔다", "딴 날짜 본문" not in _p.read_text(encoding="utf-8"))

    # **원본명이 다른 것은 막지 않는다** — 개정본 회차가 일부러 그렇게 부른다
    # (위 「엑셀 문서의 2회차」 시나리오). 강화하려다 그 시험이 빨개져서 되돌렸다.
    _othersrc = Path(_d) / "othersrc.md"
    _othersrc.write_text("**2026-09-23 · 엉뚱.pdf**\n\n개정본 본문.\n", encoding="utf-8")
    rs = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-09-23",
         "--source", "보고2.pdf", "--content-file", str(_othersrc)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("헤더 원본명이 --source 와 달라도 통과한다 (개정본 경로)",
          rs.returncode == 0, (rs.returncode, rs.stdout, rs.stderr))

    _bom = Path(_d) / "bom.md"
    _bom.write_bytes("**2026-09-24 · 보고.pdf**\n\nBOM 본문.\n".encode("utf-8-sig"))
    rbom = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-09-24",
         "--source", "보고.pdf", "--content-file", str(_bom)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("BOM 붙은 헤더도 막힌다", rbom.returncode == 1, (rbom.returncode, rbom.stdout))
    check("사유가 BOM 이라고 말해 준다 (눈으로는 구별이 안 된다)",
          "BOM" in rbom.stderr, rbom.stderr)

    # 빈 블록은 **검사 전에** SKIP 된다 — 지금 동작 그대로여야 한다
    _empty = Path(_d) / "empty.md"
    _empty.write_text("\n  \n", encoding="utf-8")
    re_ = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "insert_entry.py"),
         "--file", str(_p), "--date", "2026-08-09",
         "--source", "빈것.pdf", "--content-file", str(_empty)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("빈 블록은 전처럼 SKIP(0)", re_.returncode == 0 and "SKIP" in re_.stdout,
          (re_.returncode, re_.stdout))

print()
print("전부 통과" if FAILED == 0 else "실패 있음")
sys.exit(1 if FAILED else 0)
