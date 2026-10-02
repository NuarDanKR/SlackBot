#!/usr/bin/env python3
"""
detect_merged_numbers.py 시험 — **뭉친 실물을 픽스처로 박아 둔다.**

  python .claude/skills/doc-archive/scripts/test_detect_merged_numbers.py

**왜 픽스처가 필요한가.** kordoc 이 `<td>` HTML 표를 안 내는 쪽으로 바뀌면 이
정규식은 **에러 없이 0건**을 낸다 — 검사가 죽었는데 화면은 「깨끗합니다」와 똑같다.
아카이브에서 실제로 뭉친 칸을 몇 개 박아 두면 그 침묵이 시험 실패로 바뀐다.
아래 픽스처는 전부 자료 저장소에 실재하는 문자열이다 (2026-09-21 확인).

**정규식을 일부러 깨뜨리면 빨개지는가**도 함께 본다 — 이 집은 「관문은 자기를
지키는 시험을 갖는다」가 관례다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import detect_merged_numbers as D  # noqa: E402

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def eq(got, want, m):
    ok(m) if got == want else bad(f"{m} — 기대 {want!r} · 실제 {got!r}")


# ── 실물 픽스처 ──────────────────────────────────────────────────────────
# 자료 저장소에서 그대로 따왔다. 왼쪽이 md 의 한 줄, 오른쪽이 걸려야 하는 토큰.

MERGED = [
    # 본부장간담회 — 이웃 칸 셋이 한 칸으로 (계획서 「근거」 절의 실물)
    ('<td rowspan="7">13,4731,076<br>7,499</td>', ["13,4731,076"]),
    # 대물변제계약서 — 스캔 OCR 표
    ("<td>85019,803132,238124,900</td>", ["85019,803132,238124,900"]),
    ("<td>1,05,994</td>", ["1,05,994"]),
    # 2026년 8월 경영실적 보고 — 2026-09-17 에 4.14.0 으로 변환돼 검사 없이 들어갔다
    ("<td>2868,373</td>", ["2868,373"]),
    ("<td>7901,076</td>", ["7901,076"]),
]

# **갈래 ① 로만 걸리는 실물.** 실사고가 이 꼴이었다 — 사람이 `1,000900` 을
# 1,000억으로 읽어 머리말 `주요 항목` 에 옮겨 적었고 봇이 그 값을 인용했다
# (아이엠뱅크 몫 900억이 통째로 빠졌다. 2026-09-20 에 1,900억으로 정정).
# 계획서는 이 건이 갈래 ② 축에 걸렸을 것으로 적었으나 **안 걸린다** — 그래서
# `review_batch.py` 의 여덟째 축은 `with_ledger=True` 로 부른다 (2026-09-21 시험이 잡음).
LEDGER_ONLY = [
    ("<td>1,000900</td>", ["1,000900"]),          # 어느 사업장의 시장성분석 보고
    ("<td>153,600,000000</td>", ["153,600,000000"]),
]

CLEAN = [
    "<td>13,473</td>",                 # 정상형
    "<td>1,234,567</td>",              # 정상형 (세 자리 묶음)
    "<td>99.7%</td>",                  # 숫자지만 쉼표 없음
    "<td>1,2</td>",                    # 열거 쉼표 — 6자리 문턱 아래
    "<td>3,4,15</td>",                 # 쪽·첨부 번호 나열
    "<td>2007,2008</td>",              # 헛걸림 ① 연도쌍·전매예정 호수
    "<td>2025,06</td>",                # 헛걸림 ② 연월
    "13,4731,076",                     # `<td>` 밖 — 표 칸이 아니면 안 본다
]


def t_merged():
    print("뭉친 실물이 걸리나")
    for line, want in MERGED:
        hits = D.scan_text(line)
        got = [t for *_, toks in hits for t in toks]
        eq(got, want, f"{line[:42]}…")


def t_clean():
    print("멀쩡한 것이 안 걸리나")
    for line in CLEAN:
        hits = D.scan_text(line)
        eq([t for *_, toks in hits for t in toks], [], line[:42])


def t_ledger():
    """갈래 ①(원장 쉼표형)은 기본에서 빠지고 `--with-ledger` 로만 나온다.

    기본이 ② 전용인 이유는 **계보의 숫자를 재현하기 위해서**다(옛 판정표가 ② 만
    셌다). 사람이 보는 7.5 축은 반대로 **둘 다** 봐야 한다 — 실사고가 ① 이었다.
    """
    print("갈래 ① — 기본에서 빠지고 켜면 나오나")
    line = "<td>1,234567</td>"
    eq([t for *_, k in D.scan_text(line) for t in k], [], "기본에서는 안 걸린다")
    eq([t for *_, k in D.scan_text(line, with_ledger=True) for t in k],
       ["1,234567"], "--with-ledger 면 걸린다")
    for src, want in LEDGER_ONLY:
        eq([t for *_, k in D.scan_text(src) for t in k], [], f"기본에서 안 걸린다 — {src[:30]}")
        eq([t for *_, k in D.scan_text(src, with_ledger=True) for t in k], want,
           f"켜면 걸린다 — {src[:30]}")


def t_fp_switch():
    print("헛걸림 거르기를 끄면 다시 걸리나")
    line = "<td>2007,2008</td>"
    eq([t for *_, k in D.scan_text(line, fp_filter=False) for t in k],
       ["2007,2008"], "--no-fp-filter 면 걸린다 (옛 판정표 재현용)")


def t_threshold():
    """**6자리 문턱이 이 잣대의 핵심이다** — 잃어버려서 337칸을 냈던 그 조건."""
    print("6자리 문턱")
    eq([t for *_, k in D.scan_text("<td>12,345</td>", mindigits=6) for t in k], [],
       "5자리는 안 걸린다")
    eq([t for *_, k in D.scan_text("<td>1,05,994</td>", mindigits=6) for t in k],
       ["1,05,994"], "6자리는 걸린다")
    eq(D.MIN_DIGITS, 6, "기본 문턱이 6 이다")


def t_axis():
    print("그래프 눈금 사다리 표시")
    eq(D.looks_like_chart_axis("600,000500,000400,000300,000200,000100,0000"), True,
       "내림 사다리를 눈금으로 본다")
    eq(D.looks_like_chart_axis("85019,803132,238124,900"), False,
       "진짜 뭉친 금액은 눈금이 아니다")
    eq(D.looks_like_chart_axis("13,4731,076"), False, "두 값이 붙은 것은 눈금이 아니다")
    # 눈금이어도 **걸리기는 한다** — 빼지 않고 문구만 가른다
    hits = D.scan_text("<td>10,0005,0000</td>")
    eq([t for *_, k in hits for t in k], ["10,0005,0000"], "눈금도 표에는 실린다")


def t_multiline():
    """`review_batch.py` 가 넘기는 **여러 줄**을 그대로 먹고 줄 번호를 낸다."""
    print("여러 줄 입력")
    text = "머리글\n<td>13,473</td>\n<td>2868,373</td>\n"
    hits = D.scan_text(text)
    eq(len(hits), 1, "뭉친 칸 하나만 걸린다")
    eq(hits[0][0], 3, "줄 번호가 3 이다 (1부터)")


def t_sabotage():
    """**정규식을 일부러 깨뜨리면 빨개지나.** 검사가 죽은 것을 시험이 알아채야 한다."""
    print("사보타주 — 조건을 망가뜨리면 걸러지나")
    saved_cell, saved_min = D.CELL, D.MIN_DIGITS
    try:
        import re
        D.CELL = re.compile(r"<(xx)\b[^>]*>(.*?)</\1>", re.S)  # 표 칸을 못 찾게
        got = [t for *_, k in D.scan_text("<td>2868,373</td>") for t in k]
        eq(got, [], "(깨뜨린 상태에서는 0건 — 이 침묵이 아래 판정의 대상이다)")
        if got:
            bad("사보타주가 안 먹혔다")
        else:
            ok("픽스처 시험이 이 상태를 실패로 잡는다 (t_merged 가 0건을 받는다)")
    finally:
        D.CELL, D.MIN_DIGITS = saved_cell, saved_min
    # 되돌린 뒤 정상 동작을 다시 확인 — 시험이 자기 뒷정리를 했나
    eq([t for *_, k in D.scan_text("<td>2868,373</td>") for t in k], ["2868,373"],
       "되돌린 뒤 다시 걸린다")


def main():
    print("\n[detect_merged_numbers.py 시험]\n")
    for fn in (t_merged, t_clean, t_ledger, t_fp_switch, t_threshold,
               t_axis, t_multiline, t_sabotage):
        fn()
    print(f"\n{'실패 ' + str(FAILED) + '건' if FAILED else '전부 통과'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
