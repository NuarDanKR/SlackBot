#!/usr/bin/env python3
"""
문서 md 파일에 회차 블록을 날짜 순서에 맞게 삽입한다.

slack-sync 의 insert_messages.py 와 하는 일이 비슷하지만 **일부러 별도 파일**이다.
공유 스크립트에 인자를 늘리면 두 스킬이 서로를 깨뜨린다.
차이도 있다 — slack-sync 는 시간순으로 도니까 늘 맨 위에 붙이면 되지만,
문서 변환은 파일을 뒤죽박죽 처리하므로 **날짜를 보고 자리를 찾아** 넣어야 한다.

문서 md 구조 (Hermes archive.js 가 파싱하는 계약):
    # 제목
    > **사업장**: … (메타 블록)
    ---
    ## 회차 요약          ← 시리즈만. 선택
    | 날짜 | 원본 파일 | 한 줄 |
    ---
    ## 2026-07            ← 최신 월이 위
    **2026-07-31 · 원본파일명.pdf**   ← 회차 헤더. 최신이 위
    …본문…

사용:
  python insert_entry.py --file <md> --date 2026-07-31 --source "260731_보고.pdf" \\
      --content-file <블록md> [--summary "수금률 88.2%"]
  python insert_entry.py --file <md> --refresh     # 메타의 기간·수록 회차 재계산

종료코드: 0 성공 / 2 이미 있음(건너뜀) / 1 실패
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "_shared"))
import argparse
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# `archive.js` 의 `splitMessages` 와 같은 정규식. 이 줄이 두 아카이브의 공통 계약이다.
ENTRY_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$")
MONTH_RE = re.compile(r"^##\s+(\d{4}-\d{2})\s*$")
SUMMARY_HEAD_RE = re.compile(r"^##\s+회차 요약\s*$")
HEADING_RE = re.compile(r"^##\s+")


def header_problem(block: str, date: str, source: str):
    """삽입할 블록의 첫 줄이 회차 헤더인가. 문제 없으면 None, 있으면 사유 문장.

    **새 관문이 아니라 거짓말의 수리다** (2026-09-21). 헤더 없는 블록을 넣어도
    이 스크립트는 **`OK 삽입`** 을 찍었다. 시리즈 md 면 뒤따르는 `refresh_meta` 가
    기존 회차 수를 세어 「회차 12건」처럼 멀쩡한 숫자까지 붙여서, 화면에는
    잘못됐다는 기미가 하나도 없다. 그런데 `archive.js` 의 `splitMessages` 는
    `ENTRY_RE` 로 회차를 가르므로 **헤더 없는 본문은 앞 회차에 통째로 붙어**
    봇이 남의 회차 날짜·원본명으로 그 내용을 인용한다. 같은 사고가 두 번 났다
    (2026-08-04 실사고 · 2026-09-20 재변환 파일럿에서 재발).

    **모양만 보지 않고 날짜를 인자와 대본다** (2026-09-21 회의적 검증). 헤더가 있기만
    하면 통과시키니, 날짜가 `--date` 와 다른 블록이 `OK 삽입` 되어 `## 2026-09` 절
    안에 1월 회차가 앉고 메타 기간이 틀어졌다.

    **헤더를 만들어 주지는 않는다.** 날짜·원본명을 이 스크립트가 지어내면 그
    값이 곧 봇의 인용 출처가 된다 — 그것은 사람이 정하는 자리다.

    `verify_format.py` 8번과 두 겹이 아니다 — 그쪽은 **결과 파일**을 보고 이쪽은
    **입력 블록**을 본다. 그리고 그쪽은 돌려야 잡는 그물인데, 그 사이 이 스크립트가
    「OK」라는 거짓 안심 문구를 찍는다.
    """
    first = next((l for l in block.split("\n") if l.strip()), "")
    want = f"**{date} · {source or '원본파일명'}**"
    m = ENTRY_RE.match(first)
    # **원본명은 대보지 않는다.** 개정본 회차는 헤더에 그 블록의 원본명이 오고
    # `--source` 에는 개정본 이름이 오도록 **일부러** 갈라 부른다(엑셀 2회차 경로 —
    # `test_insert_entry.py` 의 그 시나리오가 실물이다). 대보게 했더니 그 시험이
    # 바로 빨개졌다 (2026-09-21). 날짜는 다르다 — 헤더 날짜가 곧 봇이 인용하는
    # 회차 날짜이고 `--date` 가 자리를 정하므로, 둘이 갈리면 언제나 결함이다.
    if m and m.group(1) == date:
        return None

    got = first.strip()[:60] or "(빈 줄뿐)"
    if first.startswith("﻿"):
        # **화면으로는 구별이 안 되는 실패다.** BOM 이 붙으면 눈에는 요구 형식과
        # 똑같이 보이는데 정규식도 `splitMessages` 도 그 줄을 회차 헤더로 안 본다.
        why = "첫 줄 맨 앞에 보이지 않는 BOM 이 붙어 있습니다 — 블록 파일을 BOM 없는 UTF-8 로 저장하세요"
    elif m and m.group(1) != date:
        why = (f"헤더의 날짜({m.group(1)})가 `--date {date}` 와 다릅니다 — 이대로 넣으면 "
               f"`## {date[:7]}` 절 안에 {m.group(1)} 회차가 앉고 메타 기간이 틀어집니다")
    else:
        why = ("헤더가 없으면 봇이 이 본문을 **앞 회차의 일부로** 읽습니다 "
               "(archive.js 의 splitMessages 가 그 줄로 회차를 가릅니다)")
    return (f"블록의 첫 줄이 회차 헤더와 안 맞습니다 — 있어야 할 것: `{want}`\n"
            f"  지금 첫 줄: {got}\n"
            f"  {why}.\n"
            "  헤더는 이 스크립트가 만들어 주지 않습니다 — 블록 파일 맨 위에 직접 넣으세요.")


def read_lines(path: Path):
    """(줄 목록, 그 파일의 줄바꿈) — 원래 줄바꿈을 그대로 돌려주려고 함께 읽는다.

    Windows 에서 write_text 를 그냥 쓰면 LF 파일이 통째로 CRLF 로 바뀌어
    한 줄 넣었을 뿐인데 전체가 diff 로 잡힌다.
    """
    raw = path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n").split("\n"), nl


def write_lines(path: Path, lines, nl: str):
    """tmp 에 쓰고 바꿔 끼운다 — 중간에 죽어도 반쪽짜리 md 가 남지 않게.

    문서 md 는 Hermes 가 파싱하는 **계약**이다(회차 헤더 `**YYYY-MM-DD · 원본파일명**`,
    월 헤딩 `## YYYY-MM`, 상단 `>` 메타 블록). 쓰다 잘리면 그 파일은 **에러를 내지 않고**
    검색에서만 빠진다 — 그러면 봇이 "그런 자료가 없다"고 단정한다. 조용히 틀리는 종류라
    나중에 발견하기가 가장 어렵다.

    `slack-sync/scripts/insert_messages.py` 의 write_lines 와 같은 방식이다. tmp 는 대상과
    **같은 폴더**에 둔다 — 다른 드라이브로 옮기면 rename 이 원자적이지 않아진다.
    `documents/**` 화이트리스트가 `*.md` 만 되살리므로 `*.md.tmp` 는 이미
    커밋에서 빠진다(조각이 남아도 작업 트리를 더럽히지 않는다).
    """
    text = collapse_blanks(lines)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(nl.join(text).encode("utf-8"))
    tmp.replace(path)


def collapse_blanks(lines):
    """빈 줄이 셋 이상 이어지면 하나로 줄인다 (삽입하면서 생기는 여백 정리)."""
    out = []
    blank = 0
    for line in lines:
        if line.strip() == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return out


def month_sections(lines):
    """[(줄번호, 'YYYY-MM')] — 파일에 있는 월 헤딩들"""
    return [(i, m.group(1)) for i, line in enumerate(lines) if (m := MONTH_RE.match(line))]


def section_end(lines, start):
    """start 다음의 첫 '## ' 헤딩 줄번호. 없으면 파일 끝."""
    for i in range(start + 1, len(lines)):
        if HEADING_RE.match(lines[i]):
            return i
    return len(lines)


def entries_in(lines, start, end):
    """[(줄번호, 'YYYY-MM-DD')] — 구간 안의 회차 헤더들"""
    out = []
    for i in range(start, end):
        m = ENTRY_RE.match(lines[i])
        if m:
            out.append((i, m.group(1)))
    return out


def all_entries(lines):
    return entries_in(lines, 0, len(lines))


def already_has(lines, date, source):
    """같은 날짜 + 같은 원본 파일명 회차가 이미 있는지"""
    for i, d in all_entries(lines):
        if d == date and (not source or source in lines[i]):
            return True
    return False


def insert_entry(path: Path, date: str, source: str, block: str, summary: str | None):
    lines, nl = read_lines(path)

    if already_has(lines, date, source):
        return None  # 호출자가 SKIP 으로 처리

    month = date[:7]
    block_lines = block.rstrip("\n").split("\n")
    months = month_sections(lines)

    target = next((i for i, m in months if m == month), None)

    if target is not None:
        end = section_end(lines, target)
        # 같은 달 안에서 우리보다 오래된 첫 회차 앞에 넣는다 (최신이 위)
        insert_at = end
        for i, d in entries_in(lines, target + 1, end):
            if d < date:
                insert_at = i
                break
        # 앞쪽 빈 줄은 남기고 붙인다
        while insert_at > target + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + [""] + block_lines + [""] + lines[insert_at:]
        where = f"기존 '## {month}' 안, 날짜순 자리"
    else:
        # 새 월 헤딩. 우리보다 오래된 첫 월 앞에 (월도 최신이 위)
        insert_at = None
        for i, m in months:
            if m < month:
                insert_at = i
                break
        if insert_at is None:
            insert_at = len(lines)
            while insert_at > 0 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
        new_lines = (
            lines[:insert_at]
            + ["", f"## {month}", ""]
            + block_lines
            + [""]
            + lines[insert_at:]
        )
        where = f"새 '## {month}' 생성"

    # **`--summary` 가 없어도 부른다** (2026-08-27). 전에는 여기서 걸러서, 요약 칸이
    # 없는 2칸짜리 표(`문서일 | 원본 파일`)는 **아무도 행을 넣을 수 없었다** — 회차는
    # 늘고 표는 그대로라 조용히 밀렸다(실측 3곳, 비공개사은 회차 46건에 표 25행).
    # 에러가 안 나고 md 도 멀쩡해 보여서 드러나지 않는다.
    new_lines = insert_summary_row(new_lines, date, source, summary)

    write_lines(path, new_lines, nl)
    return where


def insert_summary_row(lines, date, source, summary):
    """'## 회차 요약' 표에 한 행을 날짜순으로 넣는다. 표가 없으면 아무것도 안 한다.

    **칸 수는 그 표의 머리글에서 읽는다** (2026-08-27). 전에는 `| 날짜 | 원본 | 요약 |`
    세 칸으로 고정이라, 2칸짜리 표(`문서일 | 원본 파일`)에 넣으면 표가 깨지고 그래서
    아예 안 넣게 돼 있었다. `--summary` 는 남는 칸에 `|` 로 나눠 들어간다.
    """
    head = next((i for i, l in enumerate(lines) if SUMMARY_HEAD_RE.match(l)), None)
    if head is None:
        return lines
    end = section_end(lines, head)

    # 표의 모든 줄. 구분선(|---|---|)도 '|' 로 시작하므로 함께 잡아야 인덱스가 안 밀린다.
    rows = [i for i in range(head, end) if lines[i].lstrip().startswith("|")]
    if len(rows) < 2:
        return lines  # 표가 아직 없다
    body = rows[2:]  # 0=헤더, 1=구분선

    ncols = len(lines[rows[0]].strip().strip("|").split("|"))
    parts = [p.strip() for p in summary.split("|")] if summary else []
    need = max(0, ncols - 2)
    if len(parts) != need:
        # 조용히 자르거나 채우면 값이 엉뚱한 칸에 들어간다. 맞춰 넣되 그 사실을 알린다.
        print(f"   ! 요약 칸이 {need}개인데 {len(parts)}개를 받았습니다 — 표를 확인하세요")
    parts = (parts + [""] * need)[:need]
    row = "| " + " | ".join([date, source, *parts]) + " |"

    insert_at = None
    for i in body:
        m = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})", lines[i])
        if m and m.group(1) < date:
            insert_at = i
            break
    if insert_at is None:
        insert_at = (body[-1] + 1) if body else rows[1] + 1
    return lines[:insert_at] + [row] + lines[insert_at:]


def refresh_meta(path: Path):
    """메타의 '기간'·'수록 회차' 를 실제 회차에서 다시 계산한다."""
    lines, nl = read_lines(path)
    dates = sorted(d for _, d in all_entries(lines))
    if not dates:
        return "회차 없음"

    period = dates[0] if dates[0] == dates[-1] else f"{dates[0]} ~ {dates[-1]}"
    changed = []
    text = "\n".join(lines)

    def _period(m):
        changed.append("기간")
        return f"**기간**: {period}"

    # 값 끝의 공백은 매치에서 빼둔다. 같이 삼키면 뒤따르는 '·' 와 붙어버린다.
    text, _ = re.subn(r"\*\*기간\*\*:[ \t]*[^\n·]*[^\s·]", _period, text, count=1)

    def _count(m):
        changed.append("수록 회차")
        return f"**수록 회차**: {len(dates)}건"

    text, _ = re.subn(r"\*\*수록 회차\*\*:\s*\d+건", _count, text, count=1)

    write_lines(path, text.split("\n"), nl)
    return f"{', '.join(changed) or '변경 없음'} (회차 {len(dates)}건, {period})"


def main():
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다 —
    # 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 밖으로 나간다.
    from mode import exit_if_blocked
    exit_if_blocked("아카이브 삽입(insert_entry)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="문서 md 경로")
    ap.add_argument("--date", help="회차 날짜 (YYYY-MM-DD)")
    ap.add_argument("--source", default="", help="원본 파일명 (중복 판정에 씀)")
    ap.add_argument("--content-file", help="삽입할 블록이 든 파일")
    ap.add_argument("--summary", help="회차 요약 표에 넣을 한 줄")
    ap.add_argument("--refresh", action="store_true", help="메타의 기간·수록 회차만 재계산")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: 파일 없음 — {path}", file=sys.stderr)
        return 1

    try:
        if args.refresh:
            print(f"OK 메타 갱신 — {path.name}: {refresh_meta(path)}")
            return 0

        if not args.date or not args.content_file:
            print("ERROR: --date 와 --content-file 이 필요합니다", file=sys.stderr)
            return 1

        block = Path(args.content_file).read_text(encoding="utf-8")
        if not block.strip():
            print(f"SKIP 빈 본문 — {path.name}")
            return 0

        problem = header_problem(block, args.date, args.source)
        if problem:
            print(f"ERROR: {problem}", file=sys.stderr)
            return 1

        where = insert_entry(path, args.date, args.source, block, args.summary)
        if where is None:
            print(f"SKIP 이미 있음 ({args.date} · {args.source}) — {path.name}")
            return 2

        print(f"OK 삽입 ({where}) — {path.name}")
        print(f"   {refresh_meta(path)}")
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
