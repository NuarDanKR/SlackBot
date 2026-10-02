#!/usr/bin/env python3
"""
변환된 엑셀 md 에서 마스킹을 뚫고 남은 개인정보 후보를 문맥과 함께 보인다.

**판정하지 않는다.** 좁혀서 보이기만 하고, 넣을지 말지는 사람이 정한다.
설계: docs/superpowers/specs/2026-08-25-hermes-excel-screen-personal-design.md
"""
import argparse
import collections
import html
import re
import sys
import unicodedata
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))

# **복사하지 않고 가져온다.** `review_batch.py` 도 같은 `INLINE_PATTERNS` 를
# `xlsx_to_blocks` 에서 직접 가져온다 (2026-08-26 마스킹 규칙 2판) — 라벨·정규식을
# 두 벌 두지 않으니 변환기가 패턴을 늘려도 여기서 손으로 맞출 일이 없다.
from xlsx_to_blocks import (
    ACCOUNT_NUM, ACCOUNT_WORD, INLINE_PATTERNS, MASK_TEXT, looks_like_date,
)

# 한글 음절이 끊기지 않고 이어진 최대 구간. 공백·숫자·영문·부호에서 끊긴다.
_HANGUL_RUN = re.compile(r"[가-힣]+")

# 실제로 쓰이는 한국 성씨. **닫힌 목록이라는 것이 이 그물의 전부다** —
# `지분율`·`전용률` 처럼 성씨가 아닌 글자로 시작하는 업무 용어가 여기서 떨어진다.
# 두 글자 성(`남궁`·`황보`·`제갈`)은 따로 두지 않는다 — 첫 글자(`남`·`황`·`제`)가
# 이미 이 목록에 있어 어차피 잡힌다. `독고` 때문에 `독` 을 넣었다.
SURNAMES = frozenset(
    "강경계고공곽구국권금기길김나남노도독동류마맹명모문민박반방배백범변복봉부"
    "빈사서석선설성소손송신심안양어엄여연염오옥온왕용우원위유육윤은음이인임"
    "장전정제조주지진차채천초최추탁태편표피하한함허현형호홍화황"
)


def hangul_runs(text):
    """한글 음절이 끊기지 않고 이어진 최대 구간들. 빈 값·None 이면 빈 목록."""
    return _HANGUL_RUN.findall(text or "")


def is_name_candidate(run):
    """2~4자이고 첫 글자가 성씨면 사람 이름 후보다.

    **길이를 4로 막는 이유**: 조사·호칭이 붙어 길어진 덩어리(`홍길동님이`)는
    여기서 빠지고 호칭 그물이 그 칸을 통째로 보인다. 두 그물이 서로를 받친다."""
    if not isinstance(run, str) or not (2 <= len(run) <= 4):  # 문자열이 아니면 이름일 수 없다 — 죽는 대신 그냥 후보에서 뺀다
        return False
    return run[0] in SURNAMES


# 사람이 손으로 쌓는 「성씨로 시작하지만 사람이 아닌 말」. **화면만 줄이고
# 아카이브에 들어가는 글자는 한 자도 안 바꾼다** — CLAUDE.md 의 「'안 가린다'
# 목록은 제외에 쓰지 않는다」는 봇에게 나가는 것을 정하는 `KEEP_COLUMN_WORDS`
# 얘기고, 이것은 사람이 보는 화면 얘기다.
STOPLIST_PATH = Path(__file__).parent / "screen_personal_stoplist.txt"


def load_stoplist(path):
    """한 줄에 하나. `#` 뒤(줄 앞이든 줄 가운데든)는 주석, 빈 줄은 건너뛴다.
    파일이 없거나 폴더면 빈 집합.

    **읽은 줄은 NFC 로 맞춘다** (2026-09-03). 한글은 같은 글자를 완성형 한 글자
    (NFC — `조정액` 3자)로도, 자모로 풀어(NFD — 8자)로도 적을 수 있고 **화면에는
    똑같이 보인다.** macOS 에서 만든 파일·붙여넣기·일부 편집기가 NFD 를 낸다.
    맞추기 전에는 NFD 로 적힌 줄이 `apply_stoplist` 의 전체 일치에서 조용히 빗나갔다
    — 파일에는 분명히 그 낱말이 적혀 있는데 보고에서 안 빠지고, **왜 안 빠지는지
    사람이 알 방법이 없다.** BOM 을 벗기는 것과 같은 자리다.

    방향을 NFC 로 잡는 이유: 대보는 쪽(`hangul_runs` 의 `[가-힣]+`)이 애초에
    **완성형만** 잡는다. NFD 로 통일하면 한 건도 안 걸린다.

    **폴더 경로를 주면 `PermissionError` 로 죽는다** (2026-09-03 에 고쳤다).
    윈도우는 폴더를 텍스트로 열려고 하면 `PermissionError` 를 던진다 —
    `--stoplist` 오타로 폴더를 가리키면 이 도구 전체가 트레이스백으로 죽어서,
    개인정보를 보이는 화면 자체가 안 뜬다. `path.exists()` 는 폴더에도 참이라
    이 죽음을 못 막는다 — `is_file()` 로 따로 가른다. 못 읽는 것을 「빈
    스톱리스트」로 조용히 삼키지 않고 화면에 한 줄 남긴다 — 안 그러면
    「스톱리스트가 텅 비어 있다」와 「경로를 잘못 줬다」가 사람 눈에 같은
    모양이 된다.

    **줄 가운데 `#` 도 주석이다** (2026-09-03 에 고쳤다). 이 목록의 항목은
    `is_name_candidate` 가 잡는 것과 같은 것 — 순수 한글 2~4자(`hangul_runs` 의
    `[가-힣]+`)뿐이라 `#` 가 항목 글자로 들어올 자리가 원래 없다. 그래서 부분
    일치 걱정 없이 첫 `#` 앞까지만 쓴다. 전에는 줄 **앞**의 `#` 만 주석으로
    보아서, `조정액 # 인명 아님` 처럼 사람이 뒤에 설명을 달면 그 설명 글자까지
    통째로 스톱리스트 항목이 되어 `apply_stoplist` 의 전체 일치에서 **아무것도
    안 빠졌다** — 조용히 안 걸리는 것은 NFD 와 같은 갈래의 사고다.
    """
    path = Path(path)
    if not path.exists():
        return set()
    if not path.is_file():
        print(f"! 스톱리스트 경로가 파일이 아닙니다 — 빈 목록으로 봅니다: {path}",
              file=sys.stderr)
        return set()
    out = set()
    # utf-8-sig: 윈도우 편집기가 붙이는 BOM 이 있으면 첫 줄이 조용히 안 걸린다 —
    # 손으로 고치는 파일이라 실제로 난다.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = unicodedata.normalize("NFC", line.split("#", 1)[0]).strip()
        if s:
            out.add(s)
    return out


def apply_stoplist(runs, stop):
    """(남은 것, 뺀 것). **글자 전체가 정확히 같을 때만** 뺀다.

    부분 일치를 쓰면 `이자` 한 줄이 `이자현` 이라는 사람을 삼킨다 —
    `NOT_PERSONAL_HEADERS` 가 전체 일치인 것과 같은 이유다.
    뺀 것을 따로 돌려주는 이유: 부르는 쪽이 개수와 목록을 반드시 함께 찍는다.
    조용히 사라지면 그때부터 이 목록이 위험해진다."""
    kept, dropped = [], []
    for r in runs:
        (dropped if r in stop else kept).append(r)
    return kept, dropped


# 변환기의 HEADER_FMT (`**{date} · {source} — 시트 {i}/{n}: {name}**`) 에서 이름만.
_SHEET_NAME_RE = re.compile(r"^\*\*.*—\s*시트\s*\d+/\d+:\s*(.+?)\s*\*\*\s*$", re.M)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<(th|td)([^>]*)>(.*?)</\1>", re.S)
_COLSPAN_RE = re.compile(r'colspan="(\d+)"')
# `- **A1** [심현준] 본문` — 셀주소와 대괄호 작성자를 떼고 본문만.
_MEMO_RE = re.compile(r"^-\s+\*\*[^*]+\*\*\s*(?:\[[^\]]*\]\s*)?(.*)$", re.M)
_MEMO_HEAD = "### 시트 메모"


def _cells(row_html):
    """한 `<tr>` 의 칸들 → 글자 목록. colspan 만큼 같은 글자를 반복한다.

    **rowspan 은 무시한다** — 위 행에서 내려온 칸이 있으면 그 아래 행의 열 자리가
    한 칸씩 밀린다. 그래서 머리글 짝짓기는 최선 추정이고, 부르는 쪽은 머리글이
    틀려도 판단할 수 있도록 셀 원문을 항상 함께 낸다."""
    out = []
    for _tag, attrs, inner in _CELL_RE.findall(row_html):
        text = html.unescape(inner)
        m = _COLSPAN_RE.search(attrs)
        out.extend([text] * (int(m.group(1)) if m else 1))
    return out


def parse_sheet(md_text):
    """변환기가 쓴 sheet-NN.md 하나 → {"name", "headers", "rows", "memos"}."""
    md_text = md_text or ""
    m = _SHEET_NAME_RE.search(md_text)
    name = m.group(1) if m else ""

    rows_html = _ROW_RE.findall(md_text)
    headers = _cells(rows_html[0]) if rows_html else []
    rows = [_cells(r) for r in rows_html[1:]]

    memos = []
    if _MEMO_HEAD in md_text:
        tail = md_text.split(_MEMO_HEAD, 1)[1]
        memos = [html.unescape(t).strip() for t in _MEMO_RE.findall(tail) if t.strip()]

    return {"name": name, "headers": headers, "rows": rows, "memos": memos}


# 성씨 목록에 없는 이름(외국인의 한글 표기 등)을 다른 신호로 받친다.
# 3차를 막은 `TA-302호 ○○○님이 매수자…` 가 이 그물에 걸린다.
TITLE_WORDS = ("님", "씨", "대표", "계약자", "매수자", "매도자", "임차인", "모친", "부친")

# 라벨은 변환기에서 그대로 가져온다 — 손으로 또 적으면 변환기가 패턴을 더할 때
# 조용히 어긋난다 (2026-08-26. 전에는 목록을 두 벌 두고 길이 assert 로 받쳤다).
_INLINE_KINDS = tuple(label for label, _ in INLINE_PATTERNS)
_INLINE_MASK = tuple(pat for _, pat in INLINE_PATTERNS)

# 계좌 라벨. `INLINE_PATTERNS` 에 없다 — 계좌는 모양만으로는 날짜와 안 갈려서
# **낱말이 같은 글자열에 있을 때만** 잡는 별도 갈래다 (`xlsx_to_blocks.mask_inline`).
ACCOUNT_KIND = "계좌"

# **화면에 적는 축 이름을 실제로 재는 것에서 만든다** (2026-09-03). 전에는 보고 줄이
# 「전화·이메일·주민번호 잔여 0건」이라고 손으로 적혀 있었는데, 그때 이 도구는
# 계좌를 **아예 안 보고 있었다** — 화면이 축 하나를 통째로 안 적으니 「안 쟀다」가
# 「없다」로 읽혔다. 이제 목록에서 문구를 만드니 축을 더할 때 문구가 같이 따라온다.
SCREENED_KINDS = _INLINE_KINDS + (ACCOUNT_KIND,)
_SCREENED_TEXT = " · ".join(SCREENED_KINDS)


def account_hits(text):
    """한 칸 안의 마스킹 안 된 계좌번호들. **변환기와 같은 조건이다.**

    `xlsx_to_blocks.mask_inline` 은 셀 하나씩 돌면서 **그 셀 안에** 계좌 낱말이
    있을 때만 가리고, 날짜로 읽히는 것은 건너뛴다. 여기서 조건을 넓히면 ① 의
    뜻(「0이 아니면 변환기 결함」)이 무너진다 — 변환기가 애초에 안 가리는 것을
    변환기 결함으로 찍게 된다.

    **그래서 이 축은 「계좌 낱말이 머리글 칸에 있고 값은 다른 칸」인 표를 못 본다.**
    그건 변환기도 못 가리는 자리고, 7.5 점검표(`review_batch.py`)가 회차 블록
    전체를 먹여서 보는 몫이다.
    """
    if not text or not ACCOUNT_WORD.search(text):
        return []
    return [m.group(0) for m in ACCOUNT_NUM.finditer(text)
            if MASK_TEXT[0] not in m.group(0) and not looks_like_date(m.group(0))]


def _texts(sheet):
    """시트 하나의 (칸 글자, 열 머리글, 그 행의 다른 칸들). 메모는 머리글이 없다.

    **첫 번째로 살아남은 행도 함께 훑는다** (WHK 결정 2026-08-25). 변환기는 그 행이
    실제 머리글인지 데이터인지 모른 채 무조건 `<th>` 로 적는다 — 첫 행이 데이터인
    시트는 그 행의 이름·전화가 지금까지 이 그물 어디에도 안 걸렸다. 대가는 `대표자명`
    같은 열 이름 자체가 후보로 잡히는 잡음인데, 그건 스톱리스트가 받을 몫이다."""
    if sheet["headers"]:
        yield from ((cell, "(머리글 줄)", sheet["headers"]) for cell in sheet["headers"])
    for row in sheet["rows"]:
        for i, cell in enumerate(row):
            head = sheet["headers"][i] if i < len(sheet["headers"]) else ""
            yield cell, head, row
    for memo in sheet["memos"]:
        yield memo, "(시트 메모)", [memo]


def screen_dir(d, stop):
    """폴더 하나를 훑어 보고를 만든다. **아무것도 판정하지 않는다.**"""
    d = Path(d)
    res = {
        "dir": str(d), "sheets": [], "inline": [],
        "names": [], "masked_row": [], "titled": [], "stopped": [],
    }
    found = collections.defaultdict(
        lambda: {"count": 0, "sheets": [], "headers": [], "samples": []}
    )
    stopped = collections.Counter()

    for f in sorted(d.glob("sheet-*.md")):
        sheet = parse_sheet(f.read_text(encoding="utf-8"))
        res["sheets"].append(sheet["name"])

        for cell, head, row in _texts(sheet):
            if not cell or cell == MASK_TEXT:
                continue

            # ① 인라인 잔여 — 정상이면 0건. 0이 아니면 변환기 쪽 결함이다.
            for kind, pat in zip(_INLINE_KINDS, _INLINE_MASK):
                for hit in pat.findall(cell):
                    res["inline"].append(
                        {"kind": kind, "text": hit, "sheet": sheet["name"], "cell": cell}
                    )
            # 계좌도 같은 갈래다 (5단계 규칙 4). 조건은 변환기와 같다 — 낱말이
            # 같은 칸에 있고 날짜가 아닐 때만.
            for hit in account_hits(cell):
                res["inline"].append(
                    {"kind": ACCOUNT_KIND, "text": hit, "sheet": sheet["name"], "cell": cell}
                )

            # ② 이름 후보
            runs = [r for r in hangul_runs(cell) if is_name_candidate(r)]
            kept, dropped = apply_stoplist(runs, stop)
            for r in dropped:
                stopped[r] += 1
            for r in kept:
                e = found[r]
                e["count"] += 1
                if sheet["name"] not in e["sheets"]:
                    e["sheets"].append(sheet["name"])
                if head and head not in e["headers"]:
                    e["headers"].append(head)
                if len(e["samples"]) < 3 and cell not in e["samples"]:
                    e["samples"].append(cell)

            # ③ 가려진 칸과 같은 행에 남은 한글
            if MASK_TEXT in row and hangul_runs(cell):
                res["masked_row"].append(
                    {"sheet": sheet["name"], "header": head, "text": cell}
                )

            # ④ 호칭·관계 낱말이 든 칸
            if any(w in cell for w in TITLE_WORDS):
                res["titled"].append(
                    {"sheet": sheet["name"], "header": head, "text": cell}
                )

    res["names"] = [
        dict(run=r, **v) for r, v in sorted(found.items(), key=lambda kv: -kv[1]["count"])
    ]
    res["stopped"] = [{"run": r, "count": c} for r, c in stopped.most_common()]
    return res


def _trunc(s, n):
    """길이를 n 으로 자르되, **실제로 잘랐으면 `…` 을 남긴다** — 안 그러면 n 자를
    넘는 이름이 아무 흔적 없이 사라지고 무해한 앞부분과 숫자만 남는다.

    # WORKAROUND: <원인 미상> 2026-09-03
    이 함수를 되돌려 재는 측정(`test_screen_personal.py` 의 "report() — 잘려 나간
    글자에는 … 표시가 남는다" 절)이 2026-08-26 에 **한 번 틀린 값을 냈다.** 다섯
    자리를 맨 슬라이싱(`s[:n]`)으로 되돌려 재는데, 첫 회차만 「2곳이 잡힌다」가
    나왔고 그 뒤 캐시를 켜고 3회·끄고 5회를 더 재도 전부 「1곳」이었다.
    처음엔 `.pyc` 캐시로 단정했다(다섯 자리를 되돌리면 원본 크기가 다섯 다
    똑같이 −6바이트가 되어 파이썬이 크기·mtime 만으로는 최신 여부를 못 가르는
    조건은 실제로 있다) — 하지만 **그 가설로 재현을 시도했는데 재현되지
    않았다.** 무엇을 관측했는지는 위 그대로이고, 왜 첫 회차만 달랐는지는
    끝내 못 찾았다. 완화책(두 번 이상 재고 `PYTHONDONTWRITEBYTECODE=1` 을 켠다)은
    `SKILL.md` 에 절차로 남아 있지만, **"원인 미상"이라는 사실 자체는 절차
    문장만으로는 안 드러난다** — 이 주석이 그 사실을 대신 남긴다.
    """
    return s if len(s) <= n else s[:n] + "…"


def report(res):
    """사람이 읽는 글자. **후보를 개수로 자르지 않는다** — 상한을 두면
    잘린 것이 「없다」로 읽힌다."""
    L = [f"■ {res['dir']}", f"  시트 {len(res['sheets'])}개: {' · '.join(res['sheets'])}"]

    # **훑을 것이 0개면 여기서 끝낸다** (2026-09-03). `screen_dir` 은 `sheet-*.md` 만
    # 모으므로 시트로 안 나뉜 문서(pdf·hwp·docx)를 가리키면 훑을 것이 하나도 없는데,
    # 그때도 아래로 내려가면 `✓ … 잔여 0건` 과 「0종·0건」 줄이 줄줄이 찍혀
    # **깨끗한 폴더와 글자 하나 다르지 않다.** SKILL.md 가 이 훑기를 삽입 전 관문으로
    # 쓰므로 그 ✓ 는 곧 통과로 읽힌다. 「안 봤다」와 「보고 깨끗했다」는 화면에서
    # 갈려야 한다 — 이 도구가 없는 폴더를 ✗ 로 찍는 것과 같은 이유다.
    if not res["sheets"]:
        L.append("  ✗ 훑을 시트가 없습니다 — 이 폴더에 `sheet-*.md` 가 한 개도 없습니다. "
                 "아무것도 안 훑었으니 「잔여 0건」이 아닙니다")
        return "\n".join(L)

    if res["inline"]:
        L.append(f"  ✗ 잔여 {len(res['inline'])}건 — 변환기 결함입니다 ({_SCREENED_TEXT})")
        for it in res["inline"]:
            L.append(f"      [{it['kind']}] {it['text']}  ({it['sheet']}) {_trunc(it['cell'], 60)}")
    else:
        L.append(f"  ✓ 잔여 0건 ({_SCREENED_TEXT})")
        L.append("      계좌는 **낱말이 같은 칸에 있을 때만** 센다 — 표에서 「계좌」가 "
                 "머리글 칸에 있고 값이 다른 칸에 있으면 못 본다 (7.5 점검표가 볼 몫)")

    L.append(f"  이름 후보 {len(res['names'])}종 (참고 — 이름은 가리지 않는다, "
             "2026-08-26 규칙 2판)")
    for n in res["names"]:
        heads = _trunc("/".join(n["headers"]), 24) or "-"
        L.append(f"      {n['run']}  {n['count']}회  [{heads}]  {' · '.join(n['sheets'])}")
        for s in n["samples"]:
            L.append(f"          {_trunc(s, 80)}")

    L.append(f"  *** 와 같은 행에 남은 칸 {len(res['masked_row'])}건")
    for m in res["masked_row"]:
        L.append(f"      ({m['sheet']}) [{m['header']}] {_trunc(m['text'], 80)}")

    L.append(f"  호칭·관계 낱말이 든 칸 {len(res['titled'])}건")
    for t in res["titled"]:
        L.append(f"      ({t['sheet']}) [{t['header']}] {_trunc(t['text'], 80)}")

    if res["stopped"]:
        L.append(
            "  스톱리스트로 뺀 것 "
            + f"{len(res['stopped'])}종: "
            + " · ".join(f"{s['run']}({s['count']})" for s in res["stopped"])
        )
    return "\n".join(L)


def _has_finding(res):
    # `names` 는 이제 「참고」다 — 그래도 --quiet 에서 세는 것은 그대로 둔다.
    # 어느 이름이 몇 번 나오는지는 여전히 사람이 봐야 하는 값이다.
    # `stopped` 도 찾은 것이다 — 스톱리스트로만 걸린 폴더를 --quiet 가 「찾은 것
    # 없음」으로 보이면, 뺀 목록이 개수와 함께 반드시 찍혀야 한다는 원칙이 --quiet
    # 뒤에서 조용히 깨진다.
    # **훑을 시트가 0개인 것도 사람이 봐야 하는 것이다** (2026-09-03). --quiet 는
    # 「찾은 것 없음」 한 줄로 접는데, 아무것도 안 훑은 폴더가 그렇게 접히면
    # 그 사실이 화면에서 아예 사라진다.
    if not res["sheets"]:
        return True
    return bool(
        res["inline"] or res["names"] or res["masked_row"] or res["titled"] or res["stopped"]
    )


def main():
    ap = argparse.ArgumentParser(
        description="변환된 엑셀 md 에서 마스킹을 뚫고 남은 개인정보 후보를 보인다. 판정은 안 한다."
    )
    ap.add_argument("dirs", nargs="+", help="xlsx_to_blocks.py 가 뱉은 폴더들")
    ap.add_argument("--stoplist", default=str(STOPLIST_PATH))
    ap.add_argument("--quiet", action="store_true", help="찾은 것이 있는 폴더만 낸다")
    a = ap.parse_args()

    stop = load_stoplist(Path(a.stoplist))
    print(f"스톱리스트 {len(stop)}개 — {a.stoplist}\n")
    screened = 0
    no_sheets = 0
    unreadable = []
    for d in a.dirs:
        p = Path(d)
        # 없거나 못 읽는 폴더를 조용히 건너뛰면 「깨끗한 보고」와 「볼 것이 없어서
        # 깨끗한 보고」가 같은 모양이 된다 — 그게 이 도구가 막으려는 「조용한 0」이다.
        if not p.is_dir():
            print(f"✗ {d} — 폴더를 찾을 수 없습니다 (건너뜁니다)")
            unreadable.append(d)
            continue
        res = screen_dir(p, stop)
        # 훑은 폴더로 안 센다 — 「2개 훑음」이 아무것도 안 훑은 폴더까지 세면
        # 요약 줄 자체가 「조용한 0」이 된다.
        if res["sheets"]:
            screened += 1
        else:
            no_sheets += 1
        if a.quiet and not _has_finding(res):
            print(f"■ {d} — 찾은 것 없음")
            continue
        print(report(res))
        print()
    print(f"■ 요약: {screened}개 훑음 · {no_sheets}개는 훑을 시트가 없음 · "
          f"{len(unreadable)}개 못 읽음")
    # **종료코드는 언제나 0.** 이 도구는 아무것도 막지 않는다 — 못 읽은 폴더가
    # 있어도 마찬가지다. 위 ✗ 줄과 요약이 보이는 것으로 충분하다.
    return 0


if __name__ == "__main__":
    sys.exit(main())
