#!/usr/bin/env python3
"""
fetch_slack_files.py 의 후보 거르기 다섯 갈래 + 채널 선택(선언 안 된 비공개 채널 거부) 시험.

  python .claude/skills/doc-archive/scripts/test_fetch_filters.py

**슬랙도 파일도 안 부른다** — `load_filters()`·`classify()` 는 주어진 값만 보는
순수 함수라 임시 폴더조차 필요 없다.

**왜 있나** — 2026-08-26 전체 리뷰가 물림(`superseded`) 필터 세 줄을 실제로 지워
보고 **어느 시험도 안 빨개지는 것**을 확인했다. 지워졌을 때의 실패 모양이 에러가
아니라는 것이 요점이다: `prune_cache` 는 물림 원본을 지우고 다음 수집이 그것을 다시
받아와 「매일 받아서 매일 지우는」 루프가 **종료코드 0 으로** 돈다
(`prune_cache.py` 14~17행의 「부분집합」 조건이 깨지는 모양).

종료코드: 0 전부 통과 / 1 실패 있음
"""
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import fetch_slack_files as F

FAILED = 0

# 만기는 오늘을 기준으로 만든다. 날짜를 박아 두면 그날이 지나는 순간
# 「만기 전」 시험이 조용히 「만기 후」를 재게 된다.
LIVE = str(F.today_kst() + timedelta(days=30))
DEAD = str(F.today_kst() - timedelta(days=1))


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def rec(fid, channel, name):
    """수집이 만드는 후보 한 건 — 거르기가 보는 칸 셋만 담는다."""
    return {"id": fid, "channel": channel, "name": name}


def state(**kw):
    """상태 파일의 뼈대. 안 준 칸은 빈 dict."""
    base = {"slack_files": {}, "excluded": {}, "deferred": {}, "superseded": {}}
    base.update(kw)
    return base


SUP = state(superseded={
    "F0OLD01": {"channel": "사업장나", "name": "일보 260710.xlsx",
                "kept": "일보 260714.xlsx", "kept_archived": False, "at": "2026-08-26"},
})

print("[1/24] 물림에 파일 ID 가 있으면 걸러진다")
f = F.load_filters(SUP)
# 이름을 일부러 다르게 준다 — 이름까지 같이 맞으면 이름 경로만 살아 있어도
# 통과해서, ID 경로 자체가 지워졌는지는 이 시험이 못 잡는다.
got = F.classify(rec("F0OLD01", "사업장나", "전혀 다른 이름.xlsx"), f)
if got == "superseded":
    ok("이름이 달라도 ID 로 걸린다 — 최신이 대신한 옛 판이 후보에서 빠진다")
else:
    bad(f"물림 원본이 ID 로 안 걸립니다 ({got!r}) — 이름까지 같을 때만 통과하던 거짓 초록이었습니다")

print("[2/24] 물림은 이름으로도 걸린다 — 다시 올려 파일 ID 가 바뀐 경우")
got = F.classify(rec("F0BRAND1", "사업장나", "일보 260710.xlsx"), f)
if got == "superseded":
    ok("ID 가 달라도 (채널 + 확장자 뺀 이름) 이 같으면 걸린다")
else:
    bad(f"이름 경로가 안 걸립니다 ({got!r}) — 다시 올리면 판정이 무효가 됩니다")

print("[3/24] 채널이 다르면 안 걸린다 — 남의 판정이 옮겨붙으면 안 된다")
got = F.classify(rec("F0BRAND1", "사업장라", "일보 260710.xlsx"), f)
if got is None:
    ok("같은 이름이라도 다른 사업장 것은 후보로 남는다")
else:
    bad(f"다른 채널 동명 파일이 걸렸습니다 ({got!r}) — 안 다룬 자료가 사라집니다")

print("[4/24] 물림 칸이 비어 있으면 후보로 남는다")
got = F.classify(rec("F0OLD01", "사업장나", "일보 260710.xlsx"), F.load_filters(state()))
if got is None:
    ok("아무 판정도 없는 파일은 안 걸린다")
else:
    bad(f"빈 상태인데 걸렸습니다 ({got!r})")

print("[5/24] 이미 변환한 파일 ID 는 걸러진다")
KNOWN = state(slack_files={
    "F0AAA01": {"channel": "사업장가", "name": "산정 내역.xlsx",
                "doc": "사업장가/산정-내역.md", "date": "2026-08-23"},
})
fk = F.load_filters(KNOWN)
got = F.classify(rec("F0AAA01", "사업장가", "산정 내역.xlsx"), fk)
if got == "known":
    ok("변환 끝난 것은 다시 안 받는다")
else:
    bad(f"변환 끝난 파일이 후보로 남습니다 ({got!r})")

print("[6/24] 일부러 뺀 것은 ID 로도 이름으로도 걸린다")
fe = F.load_filters(state(excluded={
    "F0EXC01": {"channel": "사업장가", "name": "등기부등본.pdf",
                "reason": "개인 인적사항", "decided": "2026-08-23"},
}))
got = F.classify(rec("F0EXC01", "사업장가", "전혀 다른 이름.pdf"), fe)
if got == "excluded":
    ok("이름이 달라도 ID 로 걸린다")
else:
    bad(f"제외분이 ID 로 안 걸립니다 ({got!r}) — 이름까지 같을 때만 통과하던 거짓 초록이었습니다")
got = F.classify(rec("F0BRAND2", "사업장가", "등기부등본.hwp"), fe)
if got == "excluded":
    ok("다른 포맷으로 다시 올려도 걸린다 — 판정은 파일이 아니라 그 자료에 내린 것이다")
else:
    bad(f"이름 경로가 안 걸립니다 ({got!r}) — 제외 결정이 무효가 됩니다")

print("[7/24] 보류는 만기 전만 걸리고, 만기가 지나면 후보로 돌아온다")
DEF = {"channel": "사업장라", "name": "사업장가 요약본-260824.xlsx", "until": LIVE}
got = F.classify(rec("F0DEF01", "사업장라", "사업장가 요약본-260824.xlsx"),
                 F.load_filters(state(deferred={"F0DEF01": DEF})))
if got == "deferred":
    ok("만기 전에는 걸린다")
else:
    bad(f"만기 전 보류가 후보로 올라왔습니다 ({got!r})")
# 이름은 안 맞고 ID 만 맞는 후보 — 이름 경로만 살아 있어도 통과하던 자리를 갈라 본다.
got = F.classify(rec("F0DEF01", "사업장라", "전혀 다른 이름.xlsx"),
                 F.load_filters(state(deferred={"F0DEF01": DEF})))
if got == "deferred":
    ok("이름이 달라도 ID 로 걸린다")
else:
    bad(f"ID 경로가 안 걸립니다 ({got!r}) — 이름까지 같을 때만 통과하던 거짓 초록이었습니다")
got = F.classify(rec("F0DEF01", "사업장라", "사업장가 요약본-260824.xlsx"),
                 F.load_filters(state(deferred={"F0DEF01": {**DEF, "until": DEAD}})))
if got is None:
    ok("만기가 지나면 저절로 후보로 돌아온다 — 안 그러면 어디에도 안 보인다")
else:
    bad(f"만기가 지났는데 계속 걸립니다 ({got!r}) — 영영 안 보이게 됩니다")
# ID 는 안 맞고 이름만 맞는 후보 — ID 경로만 살아 있어도 통과하던 자리를 갈라 본다.
got = F.classify(rec("F0BRAND3", "사업장라", "사업장가 요약본-260824.xlsx"),
                 F.load_filters(state(deferred={"F0DEF01": DEF})))
if got == "deferred":
    ok("ID 가 달라도 이름이 같으면 걸린다")
else:
    bad(f"이름 경로가 안 걸립니다 ({got!r}) — ID 만 보게 축소돼도 이 시험은 못 잡았을 자리입니다")

print("[8/24] 이미 변환된 자료의 다른 포맷 판은 other_format 으로 걸린다")
got = F.classify(rec("F0PDF01", "사업장가", "산정 내역.pdf"), fk)
if got == "other_format":
    ok("같은 자료가 두 벌 쌓이지 않는다")
else:
    bad(f"다른 포맷 판이 안 걸립니다 ({got!r}) — 검색이 중복 히트로 찹니다")

print("[9/24] 거르는 순서 — 같은 파일이 제외와 보류에 함께 있으면 제외가 이긴다")
got = F.classify(rec("F0BOTH1", "사업장가", "겹친 것.pdf"), F.load_filters(state(
    excluded={"F0BOTH1": {"channel": "사업장가", "name": "겹친 것.pdf",
                          "reason": "x", "decided": "2026-08-26"}},
    deferred={"F0BOTH1": {"channel": "사업장가", "name": "겹친 것.pdf", "until": LIVE}},
)))
if got == "excluded":
    ok("순서가 지금과 같다 — 바뀌면 화면 숫자가 조용히 갈린다")
else:
    bad(f"순서가 바뀌었습니다 ({got!r})")
# 제외와 물림이 겹친 경우도 본다 — excluded↔deferred 쌍만으로는 물림을 제외보다
# 앞으로 옮기는 변이를 못 잡는다.
got = F.classify(rec("F0BOTH2", "사업장가", "겹친 것 둘.pdf"), F.load_filters(state(
    excluded={"F0BOTH2": {"channel": "사업장가", "name": "겹친 것 둘.pdf",
                          "reason": "x", "decided": "2026-08-26"}},
    superseded={"F0BOTH2": {"channel": "사업장가", "name": "겹친 것 둘.pdf",
                            "kept": "y", "kept_archived": True, "at": "2026-08-26"}},
)))
if got == "excluded":
    ok("제외와 물림이 겹쳐도 제외가 이긴다 — 물림을 앞으로 옮기면 이 자리가 갈린다")
else:
    bad(f"제외·물림 순서가 바뀌었습니다 ({got!r})")

# ---------------------------------------------------------------------------
# 원자적 쓰기 — 여기부터는 임시 폴더를 쓴다 (위 아홉과 달리 파일을 실제로 만든다).
# 슬랙은 여전히 안 부른다.
# ---------------------------------------------------------------------------

print("[10/24] 원자적으로 쓴 파일의 내용이 준 것과 정확히 같다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "새 폴더" / "받은 것.pdf"
    F.atomic_write_bytes(p, b"\x00\x01ABC\xff")
    if p.read_bytes() == b"\x00\x01ABC\xff":
        ok("바이트가 그대로다 — 상위 폴더가 없어도 만들어 준다")
    else:
        bad(f"내용이 다릅니다 ({p.read_bytes()!r})")

    q = Path(d) / "매니페스트.json"
    F.atomic_write_text(q, '{"a": "한글"}')
    if q.read_text(encoding="utf-8") == '{"a": "한글"}':
        ok("텍스트도 그대로다 (utf-8)")
    else:
        bad(f"텍스트가 다릅니다 ({q.read_text(encoding='utf-8')!r})")

print("[11/24] 쓰고 나면 `.tmp` 가 남지 않는다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "받은 것.pdf"
    F.atomic_write_bytes(p, b"x" * 200)
    F.atomic_write_text(Path(d) / "m.json", "{}")
    left = sorted(x.name for x in Path(d).iterdir() if x.name.endswith(".tmp"))
    if not left:
        ok("조각이 안 남는다 — 남으면 다음 수집이 그걸 원본으로 착각한다")
    else:
        bad(f"`.tmp` 가 남았습니다: {left}")

print("[12/24] 바꿔 끼우기 전에 죽으면 원본이 그대로다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "이미 있던 것.pdf"
    OLD = "예전 내용".encode("utf-8")
    p.write_bytes(OLD)

    # `tmp.replace(path)` 자리에서 죽는 상황을 만든다. 여기서 죽는 것이 가장 나쁜
    # 시점이라 — 새 내용은 다 썼고 원본은 아직 안 바뀐 그 순간 — 이걸 재야
    # 「반쪽짜리 파일이 남지 않는다」가 증명된다.
    original_replace = Path.replace

    def boom(self, target):
        raise RuntimeError("쓰다 죽었다 (시험)")

    Path.replace = boom
    try:
        F.atomic_write_bytes(p, "새 내용".encode("utf-8"))
    except RuntimeError:
        pass
    finally:
        Path.replace = original_replace

    if p.read_bytes() == OLD:
        ok("원본이 안 깨졌다 — 직접 write_bytes 로 되돌리면 여기가 빨개진다")
    else:
        bad(f"원본이 깨졌습니다 ({p.read_bytes()!r})")

# ---------------------------------------------------------------------------
# --refetch — 규칙을 고쳐 이미 든 문서를 다시 변환해야 할 때 쓰는 정규 경로.
# 없던 동안 두 번(8/26 마스킹 2판 · 8/27 머리글 규칙) 손으로 우회했다.
# ---------------------------------------------------------------------------

REFETCH_STATE = state(slack_files={
    "F0AAA01": {"channel": "사업장가", "name": "산정 내역.xlsx",
                "doc": "사업장가/산정-내역.md", "date": "2026-08-23"},
    "F0BBB02": {"channel": "사업장나", "name": "잔금수금.pdf",
                "doc": "사업장나/잔금수금.md", "date": "2026-08-24"},
})

print("[13/24] --refetch 로 준 ID 는 이미 들어 있어도 후보로 돌아온다")
fr = F.load_filters(REFETCH_STATE, refetch={"F0AAA01"})
got = F.classify(rec("F0AAA01", "사업장가", "산정 내역.xlsx"), fr)
if got is None:
    ok("그 한 건이 다시 받아진다 — 규칙을 고쳤을 때 쓰는 정규 경로")
else:
    bad(f"여전히 걸립니다 ({got!r}) — known 에서 안 뺐습니다")
# 이름 경로도 함께 열려야 한다. ID 만 빼면 `other_format` 으로 다시 막힌다.
if got != "other_format":
    ok("이름 경로(other_format)로도 안 막힌다")
else:
    bad("known_names 에 이름이 남아 other_format 으로 막힙니다")

print("[14/24] --refetch 에 없는 다른 기록분은 여전히 걸린다")
got = F.classify(rec("F0BBB02", "사업장나", "잔금수금.pdf"), fr)
if got == "known":
    ok("넓게 열리지 않았다 — 지목한 것만 다시 받는다")
else:
    bad(f"다른 문서까지 열렸습니다 ({got!r}) — 아카이브가 통째로 두 벌이 됩니다")
# 인자를 안 주면 지금까지와 똑같아야 한다.
got = F.classify(rec("F0AAA01", "사업장가", "산정 내역.xlsx"), F.load_filters(REFETCH_STATE))
if got == "known":
    ok("--refetch 를 안 주면 전과 같다")
else:
    bad(f"기본 동작이 바뀌었습니다 ({got!r})")

# ---------------------------------------------------------------------------
# 선언 안 된 비공개 채널 — **내려받지 않는다** (fail closed).
#
# 대화 수집(`slack-archive.js:97-104`)은 같은 상황을 이미 거부한다. 문서 수집만
# 경고하고 계속 내려받았고, 그러면 그 채널 이름의 사업장 폴더가 생겨 읽기 계층이
# `isPrivateChannel(폴더)=false` 로 **공개 문서**로 취급한다 (2026-08-05 사고와 같은 모양).
# ---------------------------------------------------------------------------

def ch(name, private=False):
    """슬랙이 돌려주는 채널 한 줄 — 거르기가 보는 칸 둘만 담는다."""
    return {"name": name, "is_private": private}


# 개명 지도: 아카이브가 아는 이름 `pc-was` → 슬랙의 현재 이름 `pc-now`
ALIAS_STATE = {"channels": {"C1": {"name": "pc-now", "file": "pc-was"}}}
CMAP = F.current_channel_names(ALIAS_STATE)

print("[15/24] 개명 지도는 아카이브 이름에서 현재 이름으로 간다")
if CMAP == {"pc-was": "pc-now"}:
    ok("개명한 채널만 한 줄 — 안 바뀐 채널은 안 넣는다")
else:
    bad(f"지도가 다릅니다 ({CMAP!r})")

print("[16/24] 비공개인데 선언이 없으면 거부한다")
kept, rejected = F.select_channels([ch("undeclared", private=True)], set(), set(), {})
if not kept and len(rejected) == 1:
    ok("후보에서 빠지고 거부 목록에 든다")
else:
    bad("선언 없는 비공개 채널이 통과했습니다 — 공개 문서로 아카이브됩니다")

print("[17/24] 비공개라도 선언돼 있으면 남는다")
kept, rejected = F.select_channels([ch("declared", private=True)], {"declared"}, set(), {})
if len(kept) == 1 and not rejected:
    ok("정상 경로가 안 막힌다")
else:
    bad("선언된 비공개 채널까지 막았습니다 — 자료가 안 쌓입니다")

print("[18/24] 공개 채널은 선언이 없어도 남는다")
kept, rejected = F.select_channels([ch("plain")], set(), set(), {})
if len(kept) == 1 and not rejected:
    ok("공개 채널은 이 판정과 무관하다")
else:
    bad("공개 채널을 막았습니다 — 거부가 너무 넓습니다")

print("[19/24] 안 다루기로 한 채널은 조용히 빠진다 (경고 없음)")
kept, rejected = F.select_channels([ch("dropped", private=True)], set(), {"dropped"}, {})
if not kept and not rejected:
    ok("skip 을 먼저 걷어낸다 — 넣어 둔 채널로 매일 경고가 뜨지 않는다")
else:
    bad("제외한 채널이 거부 목록에 올랐습니다 — 안내문과 모순입니다")

print("[20/24] config 에 옛 이름을 적어 두어도 개명된 비공개 채널은 선언된 것으로 본다")
kept, rejected = F.select_channels([ch("pc-now", private=True)], {"pc-was"}, set(), CMAP)
if len(kept) == 1 and not rejected:
    ok("양쪽을 되짚어 댄다 — 어느 철자를 적어도 산다")
else:
    bad("옛 철자를 적은 비공개 채널을 거부했습니다 — 그 채널 문서가 조용히 안 쌓입니다")
# 되짚기가 판정을 넓히기만 하는지도 본다. 지도에 없는 이름까지 선언으로 보면 구멍이다.
kept, rejected = F.select_channels([ch("pc-other", private=True)], {"pc-was"}, set(), CMAP)
if not kept and len(rejected) == 1:
    ok("관계 없는 비공개 채널까지 열리지는 않는다")
else:
    bad("되짚기가 너무 넓습니다 — 선언 안 한 채널이 통과합니다")

print("[21/24] `#` 접두는 JS 와 같이 한 글자만 뗀다")
# JS `normalizeChannel` 은 `replace(/^#/, '')` 로 **한 글자만** 뗀다. 파이썬이
# `lstrip("#")` 로 전부 떼면 `##` 로 시작하는 이름에서 두 언어가 조용히 갈린다.
kept, rejected = F.select_channels([ch("##double", private=True)], {"#double"}, set(), {})
if not kept and len(rejected) == 1:
    ok("`##` 는 `#` 하나만 떨어져 다른 이름으로 남는다 — JS 와 같은 판정")
else:
    bad("`#` 을 전부 떼어 JS 와 갈립니다")

print("[22/24] config 에 옛 이름을 적어 두어도 skip 은 계속 막는다 (개명 되짚기)")
# `is_declared_private` 와 같은 판정이다. skip 만 글자 그대로 대면, 개명 순간
# skip 줄이 죽어 안 다루기로 한 **공개** 채널이 다시 내려받아진다 — 2026-08-10 에
# 대화 수집 쪽에서 21일간 벌어진 것과 같은 모양이다.
kept, rejected = F.select_channels([ch("pc-now")], set(), {"pc-was"}, CMAP)
if not kept and not rejected:
    ok("옛 철자를 적어 둔 skip 줄이 개명 뒤에도 산다")
else:
    bad("skip 이 개명을 못 되짚습니다 — 제외한 채널이 다시 내려받아집니다")
# 되짚기가 판정을 넓히기만 하는지도 본다. 지도에 없는 이름까지 막으면 구멍이다.
kept, rejected = F.select_channels([ch("pc-other")], set(), {"pc-was"}, CMAP)
if len(kept) == 1 and not rejected:
    ok("관계 없는 채널까지 막지는 않는다")
else:
    bad("skip 되짚기가 너무 넓습니다 — 정상 채널이 조용히 빠집니다")

print("[23/24] 개명 지도가 `aka` 의 사이 이름까지 철자로 인식한다")
# x → y → z 로 두 번 개명하면 `file` 은 x, `name` 은 z 만 남아 가운데 y 가 어디에도
# 없었다. 수집이 `.sync-state.json` 의 `aka` 에 y 를 남기므로(같은 계약으로 읽기 쪽도
# 고쳐진다), 지도 로딩이 그 사슬을 포함해야 y 시절 철자로 적은 config 줄이 산다.
AKA_STATE = {"channels": {"C1": {"name": "pc-now", "file": "pc-was", "aka": ["pc-mid"]}}}
CMAP_AKA = F.current_channel_names(AKA_STATE)
if CMAP_AKA == {"pc-was": "pc-now", "pc-mid": "pc-now"}:
    ok("첫 이름과 사이 이름이 전부 현재 이름으로 모인다")
else:
    bad(f"aka 가 지도에 안 들었습니다 ({CMAP_AKA!r})")
kept, rejected = F.select_channels([ch("pc-now")], set(), {"pc-mid"}, CMAP_AKA)
if not kept and not rejected:
    ok("사이 이름 철자로 적은 skip 줄도 산다")
else:
    bad("사이 이름 철자의 skip 줄이 죽습니다 — 두 번 개명하면 다시 샙니다")

print("[24/24] 목록 적재 함수도 `#` 을 한 글자만 뗀다 (JS normalizeChannel 과 통일)")
# 비교 함수(`canon_channel`)는 이미 한 글자 규칙인데, 적재 함수만 `lstrip(\"#\")` 로
# 전부 떼면 `##` 로 시작하는 이름에서 두 언어가 조용히 갈린다.
with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td) / "config.json"
    cfg_path.write_text(
        '{"privateChannels": ["##double"], "digest": {"skipChannels": ["##skipd"]}}',
        encoding="utf-8")
    got_priv = F.private_channels(cfg_path)
    got_skip = F.skip_channels(cfg_path)
if got_priv == {"#double"}:
    ok("privateChannels 적재가 한 글자 규칙이다")
else:
    bad(f"privateChannels 적재가 # 을 전부 뗍니다 ({got_priv!r})")
if got_skip == {"#skipd"}:
    ok("skipChannels 적재가 한 글자 규칙이다")
else:
    bad(f"skipChannels 적재가 # 을 전부 뗍니다 ({got_skip!r})")

sys.exit(1 if FAILED else 0)
