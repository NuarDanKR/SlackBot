#!/usr/bin/env python3
"""
변환이 끝난 첨부 원본을 `~/.doc-cache/` 에서 지운다.

원본의 보관처는 슬랙이고 `~/.doc-cache/` 는 변환하려고 잠깐 받아둔 자리다.
쌓아두면 디스크만 먹는다 (2026-08-04 기준 309건 282MB).

**지워도 다시 안 받아온다.** `fetch_slack_files.py` 가 "이미 다룬 것"을 판정하는 기준은
캐시에 파일이 있는지가 아니라 `.doc-state.json` 에 기록이 있는지다(그쪽 264~271행).
캐시 존재 확인(319행)은 그 필터를 통과한 것, 즉 **아직 변환 안 된 것**에만 걸린다.
그래서 이 스크립트는 **상태 파일에 기록이 있는 것만** 지운다 — 미변환분을 지우면
내일 자동 수집이 그걸 다시 내려받아 헛돈다.

지우는 대상 다섯 갈래. **원본 네 갈래는 `fetch_slack_files.py` 가 거르는 것의 부분집합이어야
한다** (그쪽 `history_with_threads` 언저리). 여기서만 지우고 그쪽이 안 거르면, 내일 다시
받아서 또 지우는 일이 매일 반복된다. 반대쪽(그쪽은 거르는데 여기서 안 지우는 것)은
원본이 캐시에 남을 뿐이라 안전하다:
  - `slack_files` 에 있는 파일 ID → 변환 완료
  - `excluded` 에 있는 ID → 일부러 뺀 것
  - `superseded` 에 있는 ID → 최신 판이 대신한 것(물림). 수집 쪽도 같은 필터를 두므로
    (2026-08-26 부터) 부분집합 조건이 안 깨진다
  - 이미 변환된 자료와 (채널 + 확장자 뺀 이름)이 같은 것 → 다른 포맷 판
  - `<파일ID>_<원본명>.<ext>.blocks/` 안의 파일 → 엑셀 변환의 중간 산출물. **원본이 아니다.**
    2026-08-26 부터 **원본 바로 옆**에 살고, 폴더 이름 맨 앞의 **파일 ID** 로 맞춘다(전에는
    확장자 뺀 이름으로만 맞추는 어림이라, 같은 이름이 두 채널에 있으면 엉뚱한 쪽 산출물이
    지워질 수 있었다). 위 네 갈래 중 어느 것으로든 판정이 끝난 원본의 산출물만 함께
    지운다 — `excluded`·`superseded` 를 빠뜨리면 산출물이 캐시에 영영 남는다
    (2026-08-25 에 `excluded` 를 빠뜨려 실명 55곳이 든 산출물 폴더가 남아 손으로 지웠다)

**이 판정을 수집도 쓴다** — `fetch_slack_files.py` 의 `autoprune()` 이 받기 전에
`select()`·`remove()` 를 부른다(2026-08-23 부터). 10단계를 사람이 빠뜨려도 안 쌓이게
하려는 것이다. 그래서 **판정을 여기 한 곳에 둔다** — 두 벌이 되면 조용히 갈린다.

**`skipped` 와 `deferred` 는 대상이 아니다 — 이유가 다르다.**

`skipped` 는 수집 쪽이 아예 안 본다. 변환에 실패해 `skipped` 에만 오른 파일을 여기서
지우면 매일 되받아 오고, 다시 시도하려 할 때마다 원본이 사라져 있다. (2026-08-04 현재
`skipped` 3건은 `excluded` 3건과 같은 파일이라 실제 차이는 없지만, 변환 실패가 생기면
갈린다.)

`deferred`(만기가 붙은 보류)는 수집 쪽이 **거른다** — 그래서 위 「부분집합」 조건은
안 깨지고 매일 되받아 오는 일도 없다. 여기서 안 지우는 것은 **「나중에 넣기로 한 것」의
원본을 만기까지 손에 두려는 것**이다. 대신 그 원본은 아래 「남깁니다 — 아직 변환 안 된
원본」에 계속 뜬다(만기 전이라 정상이다 — `decide.py --list` 로 확인). 지우고 싶으면
지워도 된다: 만기가 지나 후보로 돌아올 때 수집이 슬랙에서 다시 받아온다.

점(`.`)으로 시작하는 파일은 건드리지 않는다 — `--manifest` 로 받아 둔 회차 결과처럼
사람이 남겨 둔 것들이다.

사용:
  python prune_cache.py            # 뭐가 지워질지만 본다 (기본)
  python prune_cache.py --apply    # 실제로 지운다

종료코드: 0 성공 / 1 실패
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_slack_files import DEFAULT_OUT, DEFAULT_STATE, name_key, safe_name

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def mb(paths) -> float:
    return sum(p.stat().st_size for p in paths) / 1024 / 1024


def select(state_path, cache):
    """(지울 것, 남길 것). **고르기만 하고 안 지운다** — 수집 쪽에서도 같은 판정을 쓴다.

    캐시 폴더나 상태 파일이 없으면 **아무것도 안 고른다.** 상태 파일이 없다는 것은
    「전부 미변환」이라는 뜻이고, 그때 지우면 내일 자동 수집이 통째로 다시 받아온다.
    """
    state_path, cache = Path(state_path), Path(cache)
    if not cache.exists() or not state_path.exists():
        return [], []

    st = json.loads(state_path.read_text(encoding="utf-8"))
    slack_files = st.get("slack_files", {})
    # `skipped` 는 일부러 뺀다 — 위 설명 참고. 수집 쪽 필터의 부분집합이어야 한다.
    known = (set(slack_files)
             | set(st.get("excluded") or {})
             | set(st.get("superseded") or {}))
    # 캐시 파일명은 safe_name() 을 거친 이름이라, 상태 파일의 원래 이름과 그대로는 안 맞는 것이
    # 있다(대괄호·작은따옴표를 `_` 로 바꾼다). 양쪽 다 넣어 둔다.
    known_names = set()
    for v in slack_files.values():
        ch, nm = v.get("channel", ""), v.get("name", "")
        known_names.add(name_key(ch, nm))
        known_names.add(name_key(safe_name(ch), safe_name(nm)))
    for v in (st.get("superseded") or {}).values():
        ch, nm = v.get("channel", ""), v.get("name", "")
        known_names.add(name_key(ch, nm))
        known_names.add(name_key(safe_name(ch), safe_name(nm)))

    # 엑셀 변환의 중간 산출물 `<원본명>.<ext>.blocks/` 는 **원본 바로 옆**에 산다
    # (2026-08-26 부터 — 그전에는 캐시 루트에 원본명으로만 있어 같은 이름 파일이
    # 두 채널에 있으면 덮였다). 폴더 이름이 `<파일ID>_<원본명>.<ext>.blocks` 라
    # **파일 ID 로 정확히 맞출 수 있다** — 전에는 이름으로만 맞추는 어림이었다.
    #
    # **원본이 아니다.** 원본으로 세면 「아직 변환 안 된 원본 N건」이 거짓말이 되고
    # (2026-08-23 실측: 85건 중 76건이 이것이었다), 영영 안 지워져 엑셀을 넣을
    # 때마다 쌓인다.
    #
    # 판정이 끝난 것의 산출물은 함께 지운다 — `slack_files`(변환 완료) ·
    # `excluded`(판단해서 뺀 것) · `superseded`(최신이 대신한 것). **`excluded`
    # 를 넣는 것이 2026-08-25 에 실명 55곳이 든 폴더가 남아 손으로 지운 이유다.**
    # `deferred`(만기 보류)·`skipped`(변환 실패)는 원본을 남기는 것과 같은 이유로
    # 산출물도 남긴다.
    doomed, kept = [], []
    for p in sorted(cache.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.parent.name.endswith(".blocks"):
            fid = p.parent.name.partition("_")[0]
            if fid in known:
                doomed.append(p)
            # 아직 판정 안 난 것의 산출물은 작업 중일 수 있다 — 두되 원본으로 세지 않는다.
            continue
        fid, _, orig = p.name.partition("_")
        if fid in known or name_key(p.parent.name, orig) in known_names:
            doomed.append(p)
        else:
            kept.append(p)
    return doomed, kept


def remove(doomed, cache):
    """지우고 빈 채널 폴더를 걷는다. [(경로, 오류)] 를 돌려준다 (빈 목록이면 다 지웠다)."""
    failed = []
    for p in doomed:
        try:
            p.unlink()
        except OSError as e:
            failed.append((p, e))
    # 빈 채널 폴더 정리. 캐시 루트 자체는 남긴다 — 2단계 수집이 여기에 받는다.
    for d in sorted(Path(cache).rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(DEFAULT_STATE), help=".doc-state.json 경로")
    ap.add_argument("--cache", default=str(DEFAULT_OUT), help="원본 캐시 폴더")
    ap.add_argument("--apply", action="store_true", help="실제로 지운다 (기본은 목록만)")
    args = ap.parse_args()

    state_path, cache = Path(args.state), Path(args.cache)
    if not cache.exists():
        print(f"캐시 폴더가 없습니다: {cache}")
        return 0
    if not state_path.exists():
        # 상태 파일이 없으면 전부 미변환으로 봐야 한다. 지우면 안 된다.
        print(f"ERROR: 상태 파일이 없습니다: {state_path}")
        return 1

    doomed, kept = select(state_path, cache)

    if kept:
        print(f"남깁니다 — 아직 변환 안 된 원본 {len(kept)}건 ({mb(kept):.1f} MB)")
        for p in kept:
            print(f"  · #{p.parent.name} {p.name.partition('_')[2]}")
        print()

    if not doomed:
        print("지울 것이 없습니다.")
        return 0

    size = mb(doomed)
    if not args.apply:
        print(f"[목록만] 지울 것 {len(doomed)}건 ({size:.1f} MB) — 실제로 지우려면 --apply")
        for p in doomed:
            print(f"  - #{p.parent.name} {p.name.partition('_')[2]}")
        return 0

    failed = remove(doomed, cache)

    print(f"지웠습니다 — {len(doomed) - len(failed)}건 ({size:.1f} MB)")
    if failed:
        print(f"! 못 지운 것 {len(failed)}건:")
        for p, e in failed:
            print(f"  ✗ {p.name} — {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
