#!/usr/bin/env python3
"""
fetch_slack_files.py 의 `--limit` 이 **실제로 내려받는 건수**를 세는지 시험.

  python .claude/skills/doc-archive/scripts/test_fetch_limit.py

**슬랙도 파일도 안 부른다** — `plan_fetch()` 는 주어진 목록과 「캐시에 있나」 판정만
보는 순수 함수다. 캐시 판정을 함수로 받는 것이 그 때문이다(임시 폴더가 필요 없다).

**왜 있나** — 전에는 상한을 자른 **뒤에** 캐시를 확인해서(`todo = found[:limit]` →
`dest.exists()`) 이미 받아둔 파일이 한도를 먹었다. 목록은 최신순이라, 앞쪽 `limit`
건이 전부 캐시에 있으면 **그 회차는 한 건도 안 받고 끝나고 다음 회차도 똑같다** —
뒤에 있는 새 파일이 영영 안 받아진다. 종료코드는 0 이고 「받음 N」도 정상으로 보인다.
옛 수집 래퍼(2026-09-02 삭제)는 `--limit 100` 으로 이걸 우회하고 있었다.

**가장 중요한 시험은 4번**이다 — 회차를 거듭하면 결국 전부 받아지나. 1~3 은 한 회차만
보므로 「영영 안 받아진다」를 직접 재지 못한다.

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
import fetch_slack_files as F

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def check(label, cond, detail=""):
    # **f-string 으로 감싼다.** 전에는 `' — ' + detail` 이라 detail 이 리스트면
    # 실패할 때 TypeError 로 터졌다 — 즉 **실패 경로가 한 번도 안 돌아봤다.**
    # 시험이 통과할 때만 동작하는 시험은 아무것도 지키지 못한다 (2026-08-30, 어긋냄으로 발견).
    if cond:
        ok(label)
        return
    bad(f"{label} — {detail}" if detail != "" else label)


def rec(fid):
    return {"id": fid, "channel": "사업장나", "name": f"{fid}.pdf"}


def ids(rs):
    return [r["id"] for r in rs]


def cached_set(*fids):
    """캐시에 있는 것을 id 로 판정하는 함수를 만든다."""
    have = set(fids)
    return lambda r: r["id"] in have


# 목록은 늘 최신순이다 (`found.sort(..., reverse=True)`).
NEWEST_FIRST = [rec(f"F{i}") for i in range(1, 6)]  # F1(최신) … F5(가장 오래됨)


print("\n1. 캐시에 있는 것은 한도를 안 먹는다")

# F1·F2·F3 이 이미 캐시에 있고 한도가 2. 옛 방식이면 앞 2건(F1·F2)이 한도를 다 먹어
# **받는 것이 0건**이었다.
to_fetch, in_cache, remaining = F.plan_fetch(
    NEWEST_FIRST, limit=2, is_cached=cached_set("F1", "F2", "F3"))
check("캐시 3건이 한도를 안 먹는다", ids(in_cache) == ["F1", "F2", "F3"], ids(in_cache))
check("한도 2로 안 받은 것 둘을 받는다", ids(to_fetch) == ["F4", "F5"], ids(to_fetch))
check("남은 것 없음", remaining == [], ids(remaining))

print("\n2. 한도를 넘으면 남는다")

to_fetch, in_cache, remaining = F.plan_fetch(
    NEWEST_FIRST, limit=2, is_cached=cached_set())
check("최신부터 2건만 받는다", ids(to_fetch) == ["F1", "F2"], ids(to_fetch))
check("나머지 3건은 남는다", ids(remaining) == ["F3", "F4", "F5"], ids(remaining))
check("캐시는 없다", in_cache == [], ids(in_cache))

print("\n3. 한도가 0이어도 캐시는 그대로 실린다")

# 캐시에 있는 것은 이미 디스크에 있으니 변환 대상이다 — 한도와 무관하게 매니페스트에
# 실려야 한다. 안 그러면 받아만 놓고 영영 변환 안 되는 파일이 생긴다.
to_fetch, in_cache, remaining = F.plan_fetch(
    NEWEST_FIRST, limit=0, is_cached=cached_set("F2", "F4"))
check("받는 것 0건", to_fetch == [], ids(to_fetch))
check("캐시 2건은 실린다", ids(in_cache) == ["F2", "F4"], ids(in_cache))
check("나머지는 남은 것으로 센다", ids(remaining) == ["F1", "F3", "F5"], ids(remaining))

print("\n4. 회차를 거듭하면 결국 전부 받아진다 — 「영영 안 받아진다」가 안 난다")

# 실제 운영 모양: 받은 것은 캐시가 되고, 변환은 아직 안 됐으니 다음 회차 후보에 그대로
# 남는다. 그 상태로 한도 2씩 돌린다.
have = set()
rounds = []
for _ in range(10):
    tf, ic, rem = F.plan_fetch(NEWEST_FIRST, limit=2, is_cached=lambda r: r["id"] in have)
    rounds.append(ids(tf))
    if not tf:
        break
    have |= set(ids(tf))

check("세 회차 만에 5건을 다 받는다", rounds[:3] == [["F1", "F2"], ["F3", "F4"], ["F5"]], str(rounds[:3]))
check("그 다음 회차는 받을 것이 없다", rounds[3] == [] if len(rounds) > 3 else False, str(rounds))
check("전부 받아졌다", have == {"F1", "F2", "F3", "F4", "F5"}, str(sorted(have)))

print("\n5. 순서를 뒤집지 않는다")

to_fetch, _, remaining = F.plan_fetch(
    NEWEST_FIRST, limit=3, is_cached=cached_set("F2"))
check("받는 것은 최신순 그대로", ids(to_fetch) == ["F1", "F3", "F4"], ids(to_fetch))
check("남는 것도 최신순 그대로", ids(remaining) == ["F5"], ids(remaining))

print("\n6. 전부 캐시면 받을 것도 남을 것도 없다")

to_fetch, in_cache, remaining = F.plan_fetch(
    NEWEST_FIRST, limit=2, is_cached=lambda r: True)
check("받는 것 0건", to_fetch == [], ids(to_fetch))
check("남은 것 0건", remaining == [], ids(remaining))
check("다섯 건 다 캐시로 실린다", len(in_cache) == 5, str(len(in_cache)))

print("\n7. 빈 목록에서 죽지 않는다")

to_fetch, in_cache, remaining = F.plan_fetch([], limit=2, is_cached=cached_set())
check("셋 다 빈 목록", (to_fetch, in_cache, remaining) == ([], [], []))

print("")
if FAILED:
    print(f"실패 {FAILED}건", file=sys.stderr)
    sys.exit(1)
print("전부 통과")
