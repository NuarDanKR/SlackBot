#!/usr/bin/env python3
"""
screen_personal.py 시험. **아무 파일도 안 건드린다** — 임시 폴더와 메모리만 쓴다.

  python .claude/skills/doc-archive/scripts/test_screen_personal.py

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
from screen_personal import (
    hangul_runs, is_name_candidate, load_stoplist, apply_stoplist, parse_sheet,
    screen_dir, report, TITLE_WORDS, main, _has_finding, _INLINE_KINDS, _INLINE_MASK,
    _SCREENED_TEXT,
)

# 「잔여 0건」줄. **모듈에서 만든다** — 손으로 적어 두면 축을 더할 때 문구가
# 갈리고, 갈려도 에러가 안 난다 (계좌 축이 정확히 그렇게 빠져 있었다).
_CLEAN_LINE = f"✓ 잔여 0건 ({_SCREENED_TEXT})"
import contextlib
import io
import tempfile

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


print("한글 덩어리 가르기")
if hangul_runs("최초계약자 홍길동 모친") == ["최초계약자", "홍길동", "모친"]:
    ok("공백에서 갈린다")
else:
    bad(f"공백에서 안 갈립니다: {hangul_runs('최초계약자 홍길동 모친')}")

if hangul_runs("TA-302호 홍길동님이 매수자") == ["호", "홍길동님이", "매수자"]:
    ok("영문·숫자·부호에서 갈린다")
else:
    bad(f"영문·숫자에서 안 갈립니다: {hangul_runs('TA-302호 홍길동님이 매수자')}")

if hangul_runs("") == [] and hangul_runs(None) == []:
    ok("빈 값·None 에서 안 터진다")
else:
    bad("빈 값에서 터집니다")

if hangul_runs("가나다ㄱㄴㄷ라마바") == ["가나다", "라마바"]:
    ok("낱자(자모)에서 갈린다")
else:
    bad(f"낱자에서 안 갈립니다: {hangul_runs('가나다ㄱㄴㄷ라마바')}")

print("성씨 판정")
for run in ("홍길동", "김철", "남궁민수", "독고영재"):
    if is_name_candidate(run):
        ok(f"{run} — 후보로 잡힌다")
    else:
        bad(f"{run} 이 후보에서 빠졌습니다")

for run, why in (("합계액", "합은 성씨가 아니다"),
                 ("분양가", "분은 성씨가 아니다"),
                 ("홍", "1자는 이름이 아니다"),
                 ("최초계약자", "5자는 길다 — 호칭 그물이 받는다"),
                 ("홍길동님이", "조사가 붙어 5자 — 호칭 그물이 받는다")):
    if not is_name_candidate(run):
        ok(f"{run} — 안 잡힌다 ({why})")
    else:
        bad(f"{run} 이 후보로 잡혔습니다 ({why})")

for run, why in ((None, "None 은 이름일 수 없다"),
                 ("", "빈 문자열은 이름일 수 없다"),
                 (5, "문자열이 아니면 이름일 수 없다 — 죽지 않고 아니라고만 한다")):
    if not is_name_candidate(run):
        ok(f"{run!r} — 안 잡힌다 ({why})")
    else:
        bad(f"{run!r} 이 후보로 잡혔습니다 ({why})")

print("성씨라서 잡히는 잡음 — 설계가 예상한 것이지 결함이 아니다")
# `구`·`지`·`모`·`조` 는 전부 실제 한국 성씨다. 그래서 이 말들은 성씨 그물을 통과한다.
# **성씨 목록에서 빼서 해결하지 않는다** — 빼면 `지○○` 라는 실제 사람을 놓친다.
# 덜 가리는 쪽이 사고다. 이건 스톱리스트가 받을 몫이다 (Task 2).
for run in ("구분", "지분율", "모친", "조정액"):
    if is_name_candidate(run):
        ok(f"{run} — 잡힌다 (스톱리스트로 뺀다)")
    else:
        bad(f"{run} 이 안 잡혔습니다 — 성씨 목록이 좁아졌는지 보세요")

print("스톱리스트")
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "stop.txt"
    _p.write_text("# 주석 줄은 무시한다\n조정액\n\n이자\n", encoding="utf-8")
    _stop = load_stoplist(_p)
    if _stop == {"조정액", "이자"}:
        ok("주석 줄과 빈 줄을 건너뛴다")
    else:
        bad(f"읽은 값이 이상합니다: {_stop}")

    _kept, _dropped = apply_stoplist(["조정액", "홍길동", "이자현"], _stop)
    if _kept == ["홍길동", "이자현"]:
        ok("스톱리스트에 정확히 있는 것만 빠진다")
    else:
        bad(f"남은 것이 이상합니다: {_kept}")
    if _dropped == ["조정액"]:
        ok("뺀 것이 따로 돌아온다 — 조용히 사라지지 않는다")
    else:
        bad(f"뺀 목록이 이상합니다: {_dropped}")
    if "이자현" in _kept:
        ok("`이자` 한 줄이 `이자현` 을 안 삼킨다 (전체 일치)")
    else:
        bad("부분 일치로 걸렸습니다 — 사람 이름이 스톱리스트에 먹혔습니다")

if load_stoplist(Path("없는파일_1234.txt")) == set():
    ok("파일이 없으면 빈 집합 — 안 터진다")
else:
    bad("없는 파일에서 터지거나 이상한 값을 냅니다")

# **폴더 경로를 주면 `PermissionError` 로 죽는다** (2026-09-03 에 고쳤다). `path.exists()`
# 는 폴더에도 참이라 이 죽음을 못 막았다 — 개인정보를 보이는 화면 전체가 트레이스백으로
# 죽는 것이라 조용한 실패가 아니라 시끄러운 실패지만, 시끄러운 것도 실제로 터지면 안 된다.
with tempfile.TemporaryDirectory() as _d:
    try:
        _stop_dir = load_stoplist(Path(_d))
        if _stop_dir == set():
            ok("폴더 경로를 줘도 안 죽고 빈 집합을 돌려준다")
        else:
            bad(f"폴더 경로에서 이상한 값을 냅니다: {_stop_dir}")
    except PermissionError:
        bad("폴더 경로를 주면 PermissionError 로 죽습니다")

# **줄 가운데 `#` 도 주석이다** (2026-09-03 에 고쳤다). 항목은 순수 한글 2~4자뿐이라
# `#` 가 항목 글자로 들어올 자리가 없다 — 그래서 `조정액 # 인명 아님` 처럼 사람이 뒤에
# 설명을 달아도 `조정액` 만 항목이 되어야 한다. 전에는 줄 **앞**의 `#` 만 봐서 이 줄
# 전체(`조정액 # 인명 아님`)가 통째로 항목이 됐고, `apply_stoplist` 의 전체 일치에서
# `조정액` 하나만 있는 이름 후보는 걸리지 않았다.
with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "stop_inline_comment.txt"
    _p.write_text("조정액 # 인명 아님 주의\n이자 #\n", encoding="utf-8")
    _stop_inline = load_stoplist(_p)
    if _stop_inline == {"조정액", "이자"}:
        ok("줄 가운데 `#` 뒤도 주석으로 걷어낸다")
    else:
        bad(f"줄 가운데 `#` 가 안 걷힙니다: {_stop_inline}")
    _kept_inline, _dropped_inline = apply_stoplist(["조정액"], _stop_inline)
    if _dropped_inline == ["조정액"]:
        ok("주석을 걷어낸 항목이 실제로 걸린다 (전체 일치)")
    else:
        bad(f"주석이 안 걷혀 전체 일치가 실패합니다: 남은 것 {_kept_inline}, 뺀 것 {_dropped_inline}")

with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "stop_bom.txt"
    _p.write_bytes("구분\n지분율\n".encode("utf-8-sig"))
    _stop = load_stoplist(_p)
    _kept, _dropped = apply_stoplist(["구분"], _stop)
    if _dropped == ["구분"]:
        ok("BOM 붙은 파일도 첫 줄이 정상적으로 걸린다")
    else:
        bad(f"BOM 이 첫 줄을 삼켰습니다 — 남은 것: {_kept}, 뺀 것: {_dropped}")

# **NFD 로 적힌 한 줄이 조용히 안 걸린다** (2026-09-03 에 고쳤다).
# 한글은 같은 글자를 두 가지로 적을 수 있다 — 완성형 한 글자(NFC, `조정액` 3자)와
# 자모로 풀어 쓴 것(NFD, 8자). **화면에는 똑같이 보인다.** macOS 에서 만든 파일·
# 붙여넣기·일부 편집기가 NFD 를 낸다. 스톱리스트는 「보고에서 빼는 목록」이라
# 안 걸리면 화면이 시끄러워지는 쪽으로만 틀리지만, **왜 안 빠지는지 사람이 알
# 방법이 없다** — 파일에는 분명히 그 낱말이 적혀 있다.
# 방향은 **NFC 로 통일**한다. `apply_stoplist` 가 대보는 이름 후보는 md 본문에서
# 온 완성형이고(`hangul_runs` 의 `[가-힣]+` 는 애초에 완성형만 잡는다), BOM 을
# 벗기는 것과 같은 자리에서 함께 벗겨야 조용한 실패가 안 남는다.
import unicodedata as _ud  # noqa: E402

with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "stop_nfd.txt"
    _p.write_text(_ud.normalize("NFD", "조정액") + "\n", encoding="utf-8")
    _stop = load_stoplist(_p)
    _kept, _dropped = apply_stoplist(["조정액"], _stop)
    if _dropped == ["조정액"]:
        ok("NFD 로 적힌 줄도 걸린다 (화면에 똑같이 보이는 글자다)")
    else:
        bad(f"NFD 줄이 조용히 안 걸렸습니다 — 남은 것: {_kept}, 뺀 것: {_dropped}, "
            f"읽은 값: {[len(s) for s in _stop]}자")
    if all(s == _ud.normalize("NFC", s) for s in _stop):
        ok("읽어들인 값이 전부 NFC 다 (방향을 하나로 못 박는다)")
    else:
        bad(f"NFC 가 아닌 값이 섞여 있습니다: {_stop}")

print("변환 결과 md 읽기")
_MD = """**2026-08-25 · 원본.xlsx — 시트 1/3: 상황판**

<table>
<tr><th>호실</th><th colspan="2">계약자</th><th>비고</th></tr>
<tr><td>TA-302</td><td>홍길동 (다일 E&amp;C)</td><td>***</td><td>전매</td></tr>
</table>

### 시트 메모

원본 엑셀 셀에 달린 메모다. 표 안에는 보이지 않는다.

- **A1** [심현준] 7/8 최초계약자 김철수 모친에게 연락
"""
_s = parse_sheet(_MD)
if _s["name"] == "상황판":
    ok("시트 이름을 헤더에서 읽는다")
else:
    bad(f"시트 이름이 이상합니다: {_s['name']!r}")

if _s["headers"] == ["호실", "계약자", "계약자", "비고"]:
    ok("colspan 만큼 머리글을 반복해 열 자리를 맞춘다")
else:
    bad(f"머리글이 이상합니다: {_s['headers']}")

if _s["rows"] == [["TA-302", "홍길동 (다일 E&C)", "***", "전매"]]:
    ok("데이터 행을 읽고 HTML 이스케이프를 푼다 (&amp; → &)")
else:
    bad(f"행이 이상합니다: {_s['rows']}")

if _s["memos"] == ["7/8 최초계약자 김철수 모친에게 연락"]:
    ok("시트 메모의 본문만 뽑는다 (셀주소·작성자 제외)")
else:
    bad(f"메모가 이상합니다: {_s['memos']}")

_empty = parse_sheet("**2026-08-25 · 원본.xlsx — 시트 2/3: 빈시트**\n\n")
if _empty["name"] == "빈시트" and _empty["rows"] == [] and _empty["memos"] == []:
    ok("표도 메모도 없는 시트에서 안 터진다")
else:
    bad(f"빈 시트에서 이상합니다: {_empty}")

print("그물 넷")
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d)
    (_dir / "sheet-01.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 1/2: 상황판**\n\n"
        "<table>\n"
        "<tr><th>호실</th><th>업체</th><th>연락처</th><th>구분</th></tr>\n"
        "<tr><td>TA-302</td><td>홍길동 (다일 E&amp;C)</td><td>***</td><td>구분</td></tr>\n"
        "<tr><td>TA-303</td><td>(주)갑전기</td><td>02-555-0000</td><td>합계액</td></tr>\n"
        "</table>\n",
        encoding="utf-8",
    )
    (_dir / "sheet-02.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 2/2: 상담**\n\n"
        "<table>\n"
        "<tr><th>내용</th></tr>\n"
        "<tr><td>TA-302호 존슨님이 매수자로 들어온다</td></tr>\n"
        "<tr><td>문의 010-1234-5678</td></tr>\n"
        "</table>\n",
        encoding="utf-8",
    )
    _res = screen_dir(_dir, {"구분"})

    if _res["sheets"] == ["상황판", "상담"]:
        ok("시트를 파일 이름 순으로 읽는다")
    else:
        bad(f"시트 목록이 이상합니다: {_res['sheets']}")

    # 위 대조는 우연히 통과할 수 있다 — sheet-01 을 먼저 만들었으니 정렬 없이도
    # 파일시스템이 그 순서를 그대로 돌려주면 `sorted()` 를 지워도 초록으로 남는다.
    # 그래서 `glob` 이 **일부러 뒤집어 준** 상황을 만들어 `screen_dir` 이 그래도
    # 파일 이름으로 다시 정렬하는지를 직접 본다.
    _orig_glob = Path.glob
    Path.glob = lambda self, pattern: iter(sorted(_orig_glob(self, pattern), reverse=True))
    try:
        _res_rev = screen_dir(_dir, {"구분"})
    finally:
        Path.glob = _orig_glob
    if _res_rev["sheets"] == ["상황판", "상담"]:
        ok("glob 이 뒤집힌 순서로 줘도 screen_dir 이 파일 이름으로 다시 정렬한다")
    else:
        bad(f"정렬이 깨졌습니다 — glob 순서에 그대로 휘둘립니다: {_res_rev['sheets']}")

    _names = {n["run"] for n in _res["names"]}
    if "홍길동" in _names:
        ok("이름 후보를 잡는다")
    else:
        bad(f"홍길동을 놓쳤습니다: {_names}")
    if "합계액" not in _names:
        ok("성씨가 아닌 글자로 시작하는 말은 안 잡는다 (합은 성씨가 아니다)")
    else:
        bad("합계액이 후보로 잡혔습니다")
    if "구분" not in _names:
        ok("스톱리스트에 든 말은 후보에서 빠진다")
    else:
        bad("스톱리스트가 안 걸렸습니다")
    if [s["run"] for s in _res["stopped"]] == ["구분"]:
        ok("스톱리스트로 뺀 것이 보고에 남는다 — 조용히 안 사라진다")
    else:
        bad(f"뺀 목록이 보고에 없습니다: {_res['stopped']}")

    _hong = [n for n in _res["names"] if n["run"] == "홍길동"][0]
    if _hong["sheets"] == ["상황판"] and "업체" in _hong["headers"]:
        ok("후보에 시트와 열 머리글이 붙는다")
    else:
        bad(f"문맥이 이상합니다: {_hong}")

    if any("다일" in m["text"] for m in _res["masked_row"]):
        ok("*** 와 같은 행에 남은 글자를 보인다")
    else:
        bad(f"가려진 행 그물이 안 걸렸습니다: {_res['masked_row']}")

    if any("존슨" in t["text"] for t in _res["titled"]):
        ok("호칭 낱말이 든 칸을 통째로 보인다 (성씨 목록에 없는 이름을 받친다)")
    else:
        bad(f"호칭 그물이 안 걸렸습니다: {_res['titled']}")

    _inline = [i["text"] for i in _res["inline"]]
    if any("010-1234-5678" in t for t in _inline):
        ok("휴대폰 잔여를 잡는다 — 변환기가 놓친 것이라는 뜻이다")
    else:
        bad(f"인라인 잔여를 놓쳤습니다: {_inline}")
    # 2026-08-26 규칙 2판 전에는 변환기에 유선전화 패턴이 없어 "02-555-0000" 가
    # 인라인 잔여로도 안 잡혔다. Task 1 이 유선전화 패턴을 더했으니 지금은 이 값도
    # 변환기의 _INLINE_MASK 그대로 걸려야 맞다 — 안 걸리면 screen_personal 이
    # 변환기와 다른(옛) 정규식을 쓰고 있다는 뜻이다.
    if any("02-555-0000" in t for t in _inline):
        ok("일반 전화(유선전화)도 잡힌다 (변환기의 _INLINE_MASK 그대로)")
    else:
        bad(f"일반 전화가 안 잡혔습니다 — 변환기와 다른 정규식을 쓰고 있습니다: {_inline}")

    # 설계가 요구하는 것 — 스톱리스트로 뺀 것이 **화면에 실제로 찍혀야** 한다.
    # 자료 구조에만 남고 안 찍히면 조용히 사라지는 것과 같다.
    # 구분(2) — I1 이후 첫 행(머리글 "구분")과 데이터 행의 "구분" 칸이 둘 다 걸린다.
    _out = report(_res)
    if "스톱리스트로 뺀 것" in _out and "구분(2)" in _out:
        ok("뺀 것이 개수와 함께 보고에 찍힌다")
    else:
        bad(f"보고에 안 찍힙니다:\n{_out}")
    if "홍길동" in _out and "업체" in _out:
        ok("후보와 열 머리글이 보고에 함께 찍힌다")
    else:
        bad(f"문맥이 보고에 안 찍힙니다:\n{_out}")

print("호칭 낱말은 하나하나가 제 몫을 한다")
# **`TITLE_WORDS` 를 돌지 않는다.** 자기 자신을 기준 삼으면 낱말을 지워도 반복이
# 하나 줄 뿐 아무것도 실패하지 않는다 (2026-08-25 에 실제로 그랬다).
# 여기 적힌 목록이 기준이고, 낱말을 늘리거나 줄이려면 이 줄도 함께 고쳐야 한다 —
# 그게 의도한 변경임을 남기는 자리다.
EXPECTED_TITLE_WORDS = ("님", "씨", "대표", "계약자", "매수자", "매도자", "임차인", "모친", "부친")

if set(TITLE_WORDS) == set(EXPECTED_TITLE_WORDS):
    ok(f"호칭 낱말 목록이 그대로다 ({len(EXPECTED_TITLE_WORDS)}개)")
else:
    bad(f"호칭 낱말 목록이 바뀌었습니다 — 있어야 할 것: {sorted(set(EXPECTED_TITLE_WORDS) - set(TITLE_WORDS))} / 새로 생긴 것: {sorted(set(TITLE_WORDS) - set(EXPECTED_TITLE_WORDS))}")

for _w in EXPECTED_TITLE_WORDS:
    with tempfile.TemporaryDirectory() as _d:
        _dir = Path(_d)
        (_dir / "sheet-01.md").write_text(
            "**2026-08-25 · 원본.xlsx — 시트 1/1: 상담**\n\n"
            "<table>\n<tr><th>내용</th></tr>\n"
            f"<tr><td>302호 방문 기록 {_w} 확인</td></tr>\n</table>\n",
            encoding="utf-8",
        )
        _r = screen_dir(_dir, set())
        if any(_w in t["text"] for t in _r["titled"]):
            ok(f"{_w} — 그 낱말만으로도 칸이 잡힌다")
        else:
            bad(f"{_w} 가 호칭 그물에서 빠졌습니다")

print("시트 메모 — 표에는 없고 메모에만 있는 글자도 그물에 걸린다")
# C1: 표 칸에는 아무 신호도 없고, 메모에만 이름·전화·호칭 낱말을 하나씩 심는다.
# 표에서 걸릴 수 없으니, 걸렸다면 메모 경로(`_texts` 의 memos 루프)가 실제로 돈 것이다.
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d)
    (_dir / "sheet-01.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 1/1: 상담**\n\n"
        "<table>\n<tr><th>날짜</th><th>확인</th></tr>\n"
        "<tr><td>2026-08-25</td><td>완료</td></tr>\n</table>\n\n"
        "### 시트 메모\n\n"
        "- **A1** [작성자] 최지훈 고객 응대\n"
        "- **A2** [작성자] 연락처 010-9999-8888\n"
        "- **A3** [작성자] 매도자 확인 필요\n",
        encoding="utf-8",
    )
    _res_memo = screen_dir(_dir, set())

    if "최지훈" in {n["run"] for n in _res_memo["names"]}:
        ok("메모 속 이름이 이름 그물에 걸린다")
    else:
        bad(f"메모 속 이름을 놓쳤습니다: {_res_memo['names']}")

    if any("010-9999-8888" in i["text"] for i in _res_memo["inline"]):
        ok("메모 속 전화가 인라인 그물에 걸린다")
    else:
        bad(f"메모 속 전화를 놓쳤습니다: {_res_memo['inline']}")

    if any("매도자" in t["text"] for t in _res_memo["titled"]):
        ok("메모 속 호칭 낱말이 호칭 그물에 걸린다")
    else:
        bad(f"메모 속 호칭 낱말을 놓쳤습니다: {_res_memo['titled']}")

print("첫 행도 훑는다 — 머리글인지 데이터인지는 변환기도 모른다")
# I1: 변환기가 `<th>` 로 적은 첫 행이 실제로는 데이터인 시트. 지금까지는 이 행이
# 어느 그물에도 안 걸렸다 — WHK 결정으로 이제 훑는다.
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d)
    (_dir / "sheet-01.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 1/1: 명부**\n\n"
        "<table>\n<tr><th>김철수</th><th>010-1111-2222</th></tr>\n"
        "<tr><td>비고</td><td>없음</td></tr>\n</table>\n",
        encoding="utf-8",
    )
    _res_first = screen_dir(_dir, set())

    if "김철수" in {n["run"] for n in _res_first["names"]}:
        ok("첫 행(변환기가 <th> 로 적은)의 이름도 잡힌다")
    else:
        bad(f"첫 행의 이름을 놓쳤습니다: {_res_first['names']}")

    if any("010-1111-2222" in i["text"] for i in _res_first["inline"]):
        ok("첫 행의 전화도 잡힌다")
    else:
        bad(f"첫 행의 전화를 놓쳤습니다: {_res_first['inline']}")

print("report() — 표마다 서로 안 겹치는 표식으로 블록이 통째로 빠지지 않는지 본다")
# C2: 여섯 자료(인라인·이름·이름표본·가려진행·호칭·스톱리스트)에 서로 다른 표식을
# 심어, report() 가 어느 블록을 통째로 빼거나 잘못된 ✓/✗ 을 내도 잡히게 한다.
_MARK_INLINE = "MARK_INLINE_9f3a"
_MARK_NAME = "MARK_NAME_7c1e"
_MARK_SAMPLE = "MARK_SAMPLE_2b8d"
_MARK_MASKED = "MARK_MASKED_5e0f"
_MARK_TITLED = "MARK_TITLED_a41c"
_MARK_STOPPED = "MARK_STOPPED_d96b"
_res_c2 = {
    "dir": "c2-fixture", "sheets": ["c2-sheet"],
    "inline": [{"kind": "휴대폰", "text": _MARK_INLINE, "sheet": "c2-sheet", "cell": _MARK_INLINE}],
    "names": [{"run": _MARK_NAME, "count": 1, "sheets": ["c2-sheet"], "headers": ["h"], "samples": [_MARK_SAMPLE]}],
    "masked_row": [{"sheet": "c2-sheet", "header": "h", "text": _MARK_MASKED}],
    "titled": [{"sheet": "c2-sheet", "header": "h", "text": _MARK_TITLED}],
    "stopped": [{"run": _MARK_STOPPED, "count": 3}],
}
_out_c2 = report(_res_c2)
for _mark, _label in (
    (_MARK_INLINE, "인라인"), (_MARK_NAME, "이름"), (_MARK_SAMPLE, "이름 표본"),
    (_MARK_MASKED, "가려진 행"), (_MARK_TITLED, "호칭"), (_MARK_STOPPED, "스톱리스트"),
):
    if _mark in _out_c2:
        ok(f"{_label} 블록이 report() 출력에 남아 있다")
    else:
        bad(f"{_label} 블록이 report() 출력에서 사라졌습니다:\n{_out_c2}")

if _CLEAN_LINE not in _out_c2:
    ok("인라인 잔여가 있으면 ✓ 0건 줄이 안 나온다")
else:
    bad(f"인라인이 있는데 ✓ 0건 줄이 나왔습니다:\n{_out_c2}")

_res_c2_clean = {
    "dir": "c2-clean", "sheets": ["c2-sheet"],
    "inline": [], "names": [], "masked_row": [], "titled": [], "stopped": [],
}
_out_c2_clean = report(_res_c2_clean)
if _CLEAN_LINE in _out_c2_clean:
    ok("인라인이 0건이면 ✓ 0건 줄이 나온다")
else:
    bad(f"0건인데 ✓ 줄이 안 나왔습니다:\n{_out_c2_clean}")

print("계좌 축 — 삽입 전 관문이 5단계 규칙 4 를 다 덮나 (2026-09-03)")
# `screen_personal` 은 `INLINE_PATTERNS` 만 가져오고 `ACCOUNT_WORD`·`ACCOUNT_NUM` 은
# **임포트조차 안 했다.** 그런데 SKILL.md 는 「엑셀은 이 훑기를 거친 것만 넣는다」로
# 이것을 삽입 전 관문으로 지정하고, 5단계 규칙 4 는 계좌를 마스킹 대상에 넣는다.
# 화면 문구도 「전화·이메일·주민번호 잔여 0건」이라 **계좌를 안 센다는 사실을 안 적었다** —
# 안 재고 ✓ 를 찍는 것이 이 저장소가 반복해서 겪은 조용한 0 이다.
#
# 판정 조건은 **변환기와 글자 그대로 같아야 한다** — `mask_inline` 은 셀 하나씩
# 돌면서 그 셀 안에 계좌 낱말이 있을 때만 가린다. 조건이 갈리면 ① 의 뜻(「0이 아니면
# 변환기 결함」)이 무너진다.
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d)
    (_dir / "sheet-01.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 1/1: 자금**\n\n"
        "<table>\n<tr><th>구분</th><th>내용</th></tr>\n"
        "<tr><td>입금</td><td>입금계좌 301-0123-4567</td></tr>\n"
        "<tr><td>납부</td><td>계좌 2026-08-26 확인</td></tr>\n"
        "<tr><td>코드</td><td>301-0123-4567</td></tr>\n</table>\n",
        encoding="utf-8",
    )
    _res_acct = screen_dir(_dir, set())
    _acct = [i for i in _res_acct["inline"] if i["kind"] == "계좌"]

    if any(i["text"] == "301-0123-4567" and "입금계좌" in i["cell"] for i in _acct):
        ok("계좌 낱말이 같은 칸에 있으면 계좌 잔여로 잡는다")
    else:
        bad(f"계좌 잔여를 놓쳤습니다 — 삽입 전 관문이 계좌를 안 봅니다: {_res_acct['inline']}")

    if not any("2026-08-26" in i["text"] for i in _acct):
        ok("날짜로 읽히는 것은 계좌로 안 잡는다 (변환기의 looks_like_date 그대로)")
    else:
        bad(f"날짜를 계좌로 잡았습니다: {_acct}")

    if not any(i["cell"] == "301-0123-4567" for i in _acct):
        ok("낱말 없이 덩그러니 적힌 것은 안 잡는다 (변환기도 안 가리는 자리다)")
    else:
        bad(f"낱말 없는 칸을 잡았습니다 — 변환기와 조건이 갈렸습니다: {_acct}")

    _out_acct = report(_res_acct)
    if "계좌" in _out_acct.split("\n")[2]:
        # 문구에 `✗` 를 안 쓴다 — SKILL.md 가 「정상 실행에서 나오는 ✗ 는 2줄」로
        # 세는 자리라, 시험 이름에 그 글자를 넣으면 그 셈이 조용히 어긋난다.
        ok("잔여가 있을 때 첫 줄이 계좌를 함께 센다고 말한다")
    else:
        bad(f"잔여 줄이 계좌를 안 말합니다:\n{_out_acct}")

# 화면 문구는 **실제로 재는 라벨에서 만든다** — 손으로 적으면 축을 더할 때 조용히 어긋난다.
from screen_personal import SCREENED_KINDS  # noqa: E402

if "계좌" in SCREENED_KINDS and set(_INLINE_KINDS) <= set(SCREENED_KINDS):
    ok("화면 문구가 인라인 다섯 + 계좌를 전부 담는다")
else:
    bad(f"화면 문구와 실제로 재는 것이 갈렸습니다: {SCREENED_KINDS} vs {_INLINE_KINDS}")

print("--quiet 는 스톱리스트로만 걸린 폴더를 「찾은 것 없음」으로 잘못 보이지 않는다")
# I3: 다른 그물은 하나도 안 걸리고 스톱리스트만 걸린 res. `_has_finding` 이
# `stopped` 를 안 보면 이 폴더는 --quiet 아래서 조용히 사라진다.
_res_i3 = {
    "dir": "i3", "sheets": ["i3-sheet"], "inline": [], "names": [],
    "masked_row": [], "titled": [], "stopped": [{"run": "구분", "count": 2}],
}
if _has_finding(_res_i3):
    ok("스톱리스트로만 걸려도 _has_finding 은 참이다")
else:
    bad("스톱리스트로만 걸린 것을 _has_finding 이 놓쳤습니다 — --quiet 아래서 사라집니다")

print("report() — 잘려 나간 글자에는 … 표시가 남는다")
# I4: 80자 상한을 넘는 표본은 잘리면서 표시가 남아야 한다. 안 남으면 그 표본이
# 잘렸는지 원래 짧았는지 화면만 보고는 구별할 수 없다.
_long_sample = "가" * 90
_res_i4_long = {
    "dir": "i4", "sheets": ["i4-sheet"], "inline": [], "masked_row": [], "titled": [], "stopped": [],
    "names": [{"run": "테스트", "count": 1, "sheets": ["i4-sheet"], "headers": [], "samples": [_long_sample]}],
}
_out_i4_long = report(_res_i4_long)
if f"{_long_sample[:80]}…" in _out_i4_long and _long_sample not in _out_i4_long:
    ok("80자를 넘겨 잘린 표본에 … 표시가 붙는다")
else:
    bad(f"잘림 표시가 없습니다:\n{_out_i4_long}")

_short_sample = "짧은표본"
_res_i4_short = {
    "dir": "i4", "sheets": ["i4-sheet"], "inline": [], "masked_row": [], "titled": [], "stopped": [],
    "names": [{"run": "테스트", "count": 1, "sheets": ["i4-sheet"], "headers": [], "samples": [_short_sample]}],
}
_out_i4_short = report(_res_i4_short)
if _short_sample in _out_i4_short and f"{_short_sample}…" not in _out_i4_short:
    ok("안 잘린 표본에는 … 표시가 안 붙는다")
else:
    bad(f"안 잘렸는데 … 이 붙었습니다:\n{_out_i4_short}")

# I4b~e: **같은 성질을 나머지 네 자리에서도 본다.** `report()` 는 `_trunc` 를 다섯 곳에서
# 부르는데, 2026-08-26 실측으로 **위 표본 한 곳만 잡히고 나머지 넷은 맨 슬라이싱(`s[:n]`)
# 으로 되돌려도 시험이 초록**이었다. 되돌아가면 에러가 아니라 **화면이 조용히 거짓말을
# 한다** — 잘린 이름이 「원래 짧았다」로 읽혀서, 사람이 그 칸을 무해하다고 판정한다.
# 이 보고서는 개인정보 여부를 사람이 정하는 자리라 그게 사고다.
#
# **되돌려 재는 그 측정이 같은 날 한 번 틀린 값을 냈다** — 첫 회차만 「2곳이 잡힌다」가
# 나왔고, 그 뒤 8회를 더 재도 전부 「1곳」이었다. **원인은 못 찾았다(재현 안 됨).**
# 다만 조건은 실제로 갖춰져 있다: 다섯 자리를 맨 슬라이싱으로 되돌리면 원본 크기가
# **다섯 다 똑같이 −6바이트**가 되는데, 파이썬은 `.pyc` 유효성을 크기와 mtime 으로만
# 본다. 그 기전으로 어긋나면 에러 없이 판정만 밀린다. 잴 때는
# `PYTHONDONTWRITEBYTECODE=1` 을 켜고, **무엇보다 두 번 이상 잰다.**
_I4_BASE = {"dir": "i4", "sheets": ["i4-sheet"], "inline": [], "names": [],
            "masked_row": [], "titled": [], "stopped": []}
# 자리마다 다른 글자를 쓴다 — 같은 글자면 한 자리의 출력이 다른 자리의 단언을 통과시킨다.
for _label, _long, _limit, _res_i4b in [
    ("전화·이메일·주민번호가 남은 칸 원문", "셀" * 70, 60,
     {**_I4_BASE, "inline": [{"kind": "휴대폰", "text": "010-0000-0000",
                              "sheet": "i4-sheet", "cell": "셀" * 70}]}),
    ("이름 후보의 머리글", "머" * 30, 24,
     {**_I4_BASE, "names": [{"run": "테스트", "count": 1, "sheets": ["i4-sheet"],
                             "headers": ["머" * 30], "samples": []}]}),
    ("*** 와 같은 행에 남은 칸", "행" * 90, 80,
     {**_I4_BASE, "masked_row": [{"sheet": "i4-sheet", "header": "계약자명",
                                  "text": "행" * 90}]}),
    ("호칭·관계 낱말이 든 칸", "호" * 90, 80,
     {**_I4_BASE, "titled": [{"sheet": "i4-sheet", "header": "직책",
                              "text": "호" * 90}]}),
]:
    _out_i4b = report(_res_i4b)
    if f"{_long[:_limit]}…" in _out_i4b and _long not in _out_i4b:
        ok(f"{_label} — {_limit}자를 넘기면 … 표시가 붙는다")
    else:
        bad(f"{_label} 에 잘림 표시가 없습니다 ({_limit}자 상한):\n{_out_i4b}")

print("_INLINE_KINDS 와 _INLINE_MASK 길이가 같다 — zip 이 조용히 자르지 않는다")
if len(_INLINE_KINDS) == len(_INLINE_MASK):
    ok(f"길이가 같다 ({len(_INLINE_KINDS)}개)")
else:
    bad(f"길이가 다릅니다: _INLINE_KINDS {len(_INLINE_KINDS)} vs _INLINE_MASK {len(_INLINE_MASK)}")

print("main() — 없거나 못 읽는 폴더는 ✗ 로 알리고 건너뛰되 종료코드는 0")
# I2: 폴더 하나는 정상, 하나는 없는 경로. 둘 다 요약 줄의 개수에 반영돼야 하고,
# 없는 쪽은 이름과 함께 ✗ 로 찍혀야 「깨끗해서 0건」과 「못 읽어서 0건」이 안 섞인다.
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d)
    (_dir / "sheet-01.md").write_text(
        "**2026-08-25 · 원본.xlsx — 시트 1/1: 상담**\n\n"
        "<table>\n<tr><th>내용</th></tr>\n<tr><td>없음</td></tr>\n</table>\n",
        encoding="utf-8",
    )
    _missing = str(_dir / "없는폴더")
    _buf = io.StringIO()
    _old_argv = sys.argv
    sys.argv = ["screen_personal.py", str(_dir), _missing]
    try:
        with contextlib.redirect_stdout(_buf):
            _code = main()
    finally:
        sys.argv = _old_argv
    _out_main = _buf.getvalue()

    if _code == 0:
        ok("못 읽는 폴더가 있어도 종료코드는 0")
    else:
        bad(f"종료코드가 0 이 아닙니다: {_code}")

    if f"✗ {_missing}" in _out_main:
        ok("없는 폴더 이름과 함께 ✗ 로 알린다")
    else:
        bad(f"없는 폴더가 ✗ 로 안 찍혔습니다:\n{_out_main}")

    if "■ 요약: 1개 훑음 · 0개는 훑을 시트가 없음 · 1개 못 읽음" in _out_main:
        ok("요약 줄에 훑은 개수·못 읽은 개수가 함께 찍힌다")
    else:
        bad(f"요약 줄이 없거나 이상합니다:\n{_out_main}")

print("\n훑을 시트가 0개인 폴더는 「잔여 0건」과 다르게 보인다 (2026-09-03)")
# `screen_dir` 은 `sheet-*.md` 만 모은다. 시트로 안 나뉜 문서(pdf·hwp·docx)를 가리키면
# 훑을 것이 0개인데, 보고는 그때도 `✓ … 잔여 0건` 을 냈다 — SKILL.md 가 이 훑기를
# **삽입 전 관문**으로 쓰므로 그 ✓ 가 통과로 읽힌다. 「안 봤다」와 「보고 깨끗했다」는
# 화면에서 갈려야 한다.
_res_none = {"dir": "x", "sheets": [], "inline": [],
             "names": [], "masked_row": [], "titled": [], "stopped": []}
_out_none = report(_res_none)
if _CLEAN_LINE not in _out_none:
    ok("훑을 시트가 0개면 「잔여 0건 ✓」을 내지 않는다")
else:
    bad(f"훑은 것이 없는데 ✓ 를 냈습니다:\n{_out_none}")

if "훑을 시트가 없습니다" in _out_none:
    ok("훑을 시트가 없다고 말한다")
else:
    bad(f"「훑을 시트가 없다」는 말이 없습니다:\n{_out_none}")

# 시트가 하나라도 있으면 예전 문구 그대로다 — 이 변경이 정상 경로를 안 건드린다.
_res_clean = {"dir": "x", "sheets": ["상담"], "inline": [],
              "names": [], "masked_row": [], "titled": [], "stopped": []}
if _CLEAN_LINE in report(_res_clean):
    ok("시트가 있고 깨끗하면 ✓ 는 그대로 나온다")
else:
    bad(f"정상 경로의 ✓ 가 사라졌습니다:\n{report(_res_clean)}")

# --quiet 가 이것을 「찾은 것 없음」으로 삼키면 안 된다 — 삼키면 사람 눈에서 사라진다.
if _has_finding(_res_none):
    ok("--quiet 아래서도 훑을 시트가 0개인 폴더는 안 사라진다")
else:
    bad("_has_finding 이 「훑을 시트 0개」를 놓쳤습니다 — --quiet 뒤로 사라집니다")

# 요약 줄도 갈린다 — 「2개 훑음」은 아무것도 안 훑은 폴더까지 훑은 것으로 센다.
with tempfile.TemporaryDirectory() as _d:
    _dir = Path(_d) / "있음"
    _dir.mkdir()
    (_dir / "sheet-01.md").write_text(
        "**2026-09-03 · 원본.xlsx — 시트 1/1: 상담**\n\n"
        "<table>\n<tr><th>내용</th></tr>\n<tr><td>없음</td></tr>\n</table>\n",
        encoding="utf-8",
    )
    _empty = Path(_d) / "시트없음"
    _empty.mkdir()
    (_empty / "doc.md").write_text("# 시트로 안 나뉜 문서\n", encoding="utf-8")
    _buf = io.StringIO()
    _old_argv = sys.argv
    sys.argv = ["screen_personal.py", str(_dir), str(_empty)]
    try:
        with contextlib.redirect_stdout(_buf):
            _code0 = main()
    finally:
        sys.argv = _old_argv
    _out0 = _buf.getvalue()

    if "■ 요약: 1개 훑음 · 1개는 훑을 시트가 없음 · 0개 못 읽음" in _out0:
        ok("요약 줄이 「훑음」과 「훑을 시트가 없음」을 갈라 센다")
    else:
        bad(f"요약 줄이 둘을 안 가릅니다:\n{_out0}")

    if _code0 == 0:
        ok("훑을 시트가 0개여도 종료코드는 0 (이 도구는 아무것도 막지 않는다)")
    else:
        bad(f"종료코드가 0 이 아닙니다: {_code0}")

# ── 마스킹 규칙 2판 (2026-08-26) ───────────────────────────────────────
print("\n[마스킹 규칙 2판]")
from xlsx_to_blocks import INLINE_PATTERNS  # noqa: E402
import screen_personal as SP  # noqa: E402

# 라벨을 손으로 또 적으면 변환기가 패턴을 더할 때 조용히 어긋난다.
if SP._INLINE_KINDS == tuple(lbl for lbl, _ in INLINE_PATTERNS):
    ok("훑기의 라벨이 변환기의 라벨과 같다")
else:
    bad(f"라벨이 갈렸습니다: {SP._INLINE_KINDS} vs "
        f"{tuple(lbl for lbl, _ in INLINE_PATTERNS)}")

# 이름은 이제 정상이다 — 보고 문구가 「결함」으로 읽히면 안 된다.
_res = {"dir": "x", "sheets": ["S"], "inline": [],
        "names": [{"run": "홍길동", "count": 2, "sheets": ["S"],
                   "headers": ["계약자명"], "samples": ["홍길동"]}],
        "masked_row": [], "titled": [], "stopped": []}
_txt = SP.report(_res)
if "참고" in _txt and "홍길동" in _txt:
    ok("이름 후보를 참고로 보인다")
else:
    bad(f"보고 문구에 '참고' 가 없습니다:\n{_txt}")

sys.exit(1 if FAILED else 0)
