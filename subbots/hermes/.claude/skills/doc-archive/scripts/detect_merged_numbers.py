#!/usr/bin/env python3
"""
표 칸에 숫자 둘 이상이 **붙어 버린 것**(뭉침)을 찾는다.

**왜 있나.** kordoc 이 표를 md 로 옮길 때 이웃한 칸의 값을 한 칸에 몰아넣는 일이
있다 — `13,473` 과 `1,076` 이 `13,4731,076` 이 되는 식이다. 에러는 안 난다.
md 는 멀쩡해 보이고 검색도 되며, 봇은 본문을 **무가공으로** 받는다
(`src/llm/tools.js` 의 `read_document` 가 `readDocument()` 의 `r.text` 를 그대로
도구 결과로 돌려준다 — 경로 위의 가공은 비공개 마스킹·글자수 자르기·절 분할뿐이고
표 파싱·숫자 정규화는 `src/documents/*.js` 어디에도 없다). 그래서 봇은 `13,4731,076`
을 하나의 금액으로 읽고 그 값을 근거로 답한다.

**옛 문서 청소용이 아니다 — 지금도 난다.** 같은 원본 25문서를 옛 판과 kordoc 4.14.0
으로 돌려 짝지어 비교하니 93칸 → 145칸으로 **늘었다** (2026-09-21 실측).
재변환으로는 안 닫힌다는 것이 그때 확정됐고, 그래서 **재변환 도구가 아니라 잣대**를
프로세스 안에 남긴다.

**왜 스크립트로 남기나 — 서술로 두었다가 실제로 잃어버렸다.** 이 조건이 설계서에
산문으로만 적혀 있었고, 그것을 보고 재구성하니 같은 아카이브에서 337칸이 나왔다.
이 계보가 내내 숫자가 안 맞던(220↔337 · 12·7·193↔24·29·227) 원인이 그것이다.
잃어버렸던 조건은 **6자리 문턱 하나**였다 (`MIN_DIGITS`). 다시 재려면 서술을 읽고
재구성하지 말고 이 파일을 쓴다.

찾는 조건 (갈래 ②) — 표 칸(`<td>`·`<th>`) 안의 토큰 중
  ① 쉼표가 들어 있고
  ② 정상형(`1,234,567`)이 아니고
  ③ 갈래 ①(원장 쉼표형 `1,234567`)이 아니고
  ④ 쉼표를 뺀 숫자가 **6자리 이상**이고
  ⑤ 헛걸림 꼴(연도쌍·연월)이 아닌 것

**갈래 ①(원장 쉼표형)도 이 파일에 함께 산다.** 원장이 세던 406칸의 61칸이 그 꼴이다.
지금 검사가 보는 것은 ② 뿐이라 기본값에서는 `LEDGER_COMMA` 를 **일부러 뺀다** —
`--with-ledger` 로 켠다. 두 조건이 한 파일에 있어야 다음에 갈래를 바꿔 잴 때
같은 잣대에서 출발한다.

사용:
  python detect_merged_numbers.py <폴더|파일> [--percell] [--with-ledger] [--td-only]
                                  [--no-fp-filter] [--mindigits N]

**2026-09-21 자료 저장소 전수 값** (`documents/projects`). 다음에 재면 이 셋과 대본다 —
숫자가 갈리면 아카이브가 바뀐 것인지 잣대가 바뀐 것인지부터 가른다.

| 어떻게 부르나 | 칸 | 토큰 | 문서 | 무엇인가 |
|---|---|---|---|---|
| `--td-only --no-fp-filter` | 254 | 269 | 42 | **옛 판정표 재현값** (계보 비교용) |
| (기본) | 269 | 283 | 43 | `<th>` 를 더 보고 헛걸림을 거른 값 |
| `--with-ledger` | 330 | 344 | 54 | **7.5 점검표가 쓰는 범위** (갈래 ①+②) |

모듈로도 쓴다 — `review_batch.py` 가 `scan_text` 를 임포트한다.
**정규식을 그쪽에 복사하지 않는다**: 마스킹 패턴이 두 벌이 되어 한쪽만 고쳐도
에러가 안 난 채 조용히 갈린 전례가 이 스킬에 있다 (2026-08-26).

종료코드: 0 (판정이 아니라 셈이다 — 커밋 여부는 사람이 정한다)
"""

import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# **`<th>` 도 본다** (2026-09-21). 옛 잣대는 `<td>` 만 봤고, 그래서 머리글 칸에 뭉친
# 것을 통째로 못 보고 있었다 — **7.5 축과 같은 조건(갈래 ①+②)에서 22칸**이다
# (308 → 330. 기본 조건에서는 19칸). 옛 판정표의 숫자를 재현할 때만 `--td-only` 로
# 좁힌다 — 계보 비교용 값이지 사람이 볼 범위가 아니다.
CELL = re.compile(r"<(td|th)\b[^>]*>(.*?)</\1>", re.S | re.I)
TD_ONLY = re.compile(r"<(td)\b[^>]*>(.*?)</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
BR = re.compile(r"<br\s*/?>", re.I)
TOKEN = re.compile(r"[\d,]+")
NONDIGIT = re.compile(r"\D")

# 제대로 된 숫자. `1,234` · `1,234,567`
NORMAL = re.compile(r"^\d{1,3}(,\d{3})*$")
# 갈래 ① — 원장이 세던 쉼표형. `1,234567` 처럼 **뒤쪽만** 자릿수가 어긋난 꼴.
LEDGER_COMMA = re.compile(r"^\d{1,3}(,\d{3})+\d+$")

# **6자리 문턱.** 이것이 없으면 열거 쉼표(`1,2` · `3,4,15` · 쪽번호·첨부번호)가
# 전부 걸린다. 그것은 뭉친 금액이 아니다. 두 번의 독립 프로브가 이 문턱을 넣으면
# 예전 판정표가 정확히 재현된다는 것을 확인했다 (2026-09-21).
MIN_DIGITS = 6

# 헛걸림 꼴 — 무작위 40건 표본에서 실제로 나온 둘뿐이다 (2026-09-21, 헛걸림률 5%).
# 전수 하한은 5/269 = 1.9% 이고, 검사가 실제로 도는 **최근 30일 변환분**에서는 1%대다.
FALSE_POSITIVES = [
    re.compile(r"^(19|20)\d{2},(19|20)\d{2}$"),   # 연도쌍 · 전매예정 호수 (`2007,2008`)
    re.compile(r"^20\d{2},(0[1-9]|1[0-2])$"),      # 연월 (`2025,06`)
]

# 그래프 눈금이 한 칸에 몰린 것(`600,000500,000…0`). **뭉친 것 자체는 사실이라
# 검출 오류가 아니다** — 지우지 않고 문구에서만 갈라 적는다.
#
# **이 판정으로 ⚠ 를 없애면 안 된다** (2026-09-21 회의적 검증). 어느 매매계약서의
# `262,500,000268,0000` 은 262,500,000 과 손상된 268,000,000 이 뭉친 **진짜 금액**인데
# 이 휴리스틱에 「둥근 값 셋이 단조」로 걸린다. 계약서에 그래프 눈금이 있을 리 없다.
# 그러므로 이것은 **사람에게 덧붙이는 말**이지 거르는 조건이 아니다.
_PART = re.compile(r"\d{1,3}(?:,\d{3})*")
AXIS_MIN_PARTS = 3


def _greedy_parts(tok: str):
    """토큰을 왼쪽부터 정상형 숫자로 욕심껏 잘라 [(문자열, 값)] 로."""
    out, i = [], 0
    while i < len(tok):
        m = _PART.match(tok, i)
        if not m or not m.group(0):
            i += 1
            continue
        out.append((m.group(0), int(m.group(0).replace(",", ""))))
        i = m.end()
    return out


def looks_like_chart_axis(tok: str) -> bool:
    """그래프 눈금 사다리로 보이나 — 둥근 값이 단조로 3개 이상 이어진 것.

    **단정이 아니라 덧붙이는 말이다.** 걸린 것을 빼지도, ⚠ 를 내리지도 않는다 —
    실측으로 **진짜 계약 금액이 이 꼴에 걸리는 것**이 확인됐기 때문이다(위 주석).
    사람이 표에서 「이건 그래프였네」 하고 빨리 넘길 수 있게 돕는 것까지가 이 함수의
    몫이고, 무엇을 볼지 말지는 사람이 정한다.
    """
    parts = _greedy_parts(tok)
    if len(parts) < AXIS_MIN_PARTS:
        return False
    vals = [v for _, v in parts]
    if not all(v == 0 or v % 100 == 0 for v in vals):
        return False
    return all(a > b for a, b in zip(vals, vals[1:])) or all(a < b for a, b in zip(vals, vals[1:]))


def abnormal_tokens(celltext: str, mindigits: int = MIN_DIGITS,
                    with_ledger: bool = False, fp_filter: bool = True):
    """칸 글자에서 뭉친 것으로 보이는 토큰들."""
    out = []
    for m in TOKEN.finditer(celltext):
        tok = m.group(0).strip(",")
        if "," not in tok:
            continue
        if NORMAL.match(tok):
            continue
        if LEDGER_COMMA.match(tok) and not with_ledger:
            continue
        if len(NONDIGIT.sub("", tok)) < mindigits:
            continue
        if fp_filter and any(p.match(tok) for p in FALSE_POSITIVES):
            continue
        out.append(tok)
    return out


def cell_texts(text: str, td_only: bool = False):
    """[(시작 오프셋, 끝 오프셋, 태그를 걷어낸 칸 글자)] — 칸 하나가 한 항목."""
    out = []
    for m in (TD_ONLY if td_only else CELL).finditer(text):
        inner = BR.sub(" ", m.group(2))
        out.append((m.start(), m.end(), TAG.sub(" ", inner)))
    return out


def scan_text(text: str, mindigits: int = MIN_DIGITS, with_ledger: bool = False,
              fp_filter: bool = True, td_only: bool = False):
    """[(시작 줄, 끝 줄, [토큰…])] — 뭉친 것이 든 칸마다 한 항목. 줄 번호는 1부터.

    **칸이 걸친 줄 범위를 함께 낸다.** 한 칸이 여러 줄에 걸치는 표가 실제로 있고
    (자료 저장소에 71파일·1,114칸 · 한 칸이 134줄인 것도 있다), 부르는 쪽이
    「이번에 들어온 줄」로 범위를 좁힐 때 **줄 하나만 대면 그 칸을 통째로 놓친다.**
    그래서 글자는 **온전한 회차 본문**을 먹이고, 좁히는 일은 이 줄 범위로 한다
    (2026-09-21 회의적 검증이 이 구멍을 실물로 재현했다 — 칸 가운데 줄 하나를
    고친 회차가 표에서 `✓`, 즉 **「검사했고 깨끗하다」는 거짓 단언**으로 나왔다).
    """
    hits = []
    for start, end, txt in cell_texts(text, td_only):
        toks = abnormal_tokens(txt, mindigits, with_ledger, fp_filter)
        if toks:
            hits.append((text.count("\n", 0, start) + 1,
                         text.count("\n", 0, end) + 1, toks))
    return hits


def scan_file(path: Path, mindigits: int = MIN_DIGITS, with_ledger: bool = False,
              fp_filter: bool = True, td_only: bool = False):
    return scan_text(path.read_text(encoding="utf-8", errors="replace"),
                     mindigits, with_ledger, fp_filter, td_only)


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print("사용: python detect_merged_numbers.py <폴더|파일> [--percell] "
              "[--with-ledger] [--td-only] [--no-fp-filter] [--mindigits N]", file=sys.stderr)
        return 1
    root = Path(args[0])
    percell = "--percell" in args
    with_ledger = "--with-ledger" in args
    fp_filter = "--no-fp-filter" not in args
    td_only = "--td-only" in args
    mindigits = int(args[args.index("--mindigits") + 1]) if "--mindigits" in args else MIN_DIGITS

    files = [root] if root.is_file() else sorted(root.rglob("*.md"))
    total_cells = total_toks = docs = axis = 0
    rows = []
    for f in files:
        hits = scan_file(f, mindigits, with_ledger, fp_filter, td_only)
        if hits:
            docs += 1
            total_cells += len(hits)
            for _, _, toks in hits:
                total_toks += len(toks)
                axis += sum(1 for t in toks if looks_like_chart_axis(t))
            rows.append((str(f), len(hits), hits))
    for path, n, hits in sorted(rows, key=lambda x: -x[1]):
        print("%5d  %s" % (n, path))
        if percell:
            for l0, l1, toks in hits:
                where = "L%d" % l0 if l0 == l1 else "L%d-%d" % (l0, l1)
                mark = " (그래프 눈금 꼴)" if all(looks_like_chart_axis(t) for t in toks) else ""
                print("        %s: %s%s" % (where, toks[:6], mark))
    print("합계 칸=%d 토큰=%d 문서=%d (그중 그래프 눈금 꼴 토큰 %d) "
          "— 6자리 문턱=%d · 갈래①=%s · 헛걸림 거르기=%s · 칸=%s"
          % (total_cells, total_toks, docs, axis, mindigits,
             "포함" if with_ledger else "뺌", "켬" if fp_filter else "끔",
             "td 만(옛 잣대 재현)" if td_only else "td+th"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
