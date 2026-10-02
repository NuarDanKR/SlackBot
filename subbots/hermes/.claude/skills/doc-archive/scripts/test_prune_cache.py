#!/usr/bin/env python3
"""
prune_cache.py 시험 + 수집이 시작할 때 자동으로 지우는지.

  python .claude/skills/doc-archive/scripts/test_prune_cache.py

**임시 폴더에서만 돈다** — 진짜 `~/.doc-cache` 와 `.doc-state.json` 은 안 건드린다.
종료코드: 0 전부 통과 / 1 실패 있음
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import prune_cache as P
import fetch_slack_files as F

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def make(td):
    """캐시 원본 5건 + `.blocks` 3건 · 상태 파일 1개.

    변환 1 · 제외 1 · 미변환 1 · 다른 포맷 판 1 · 물림 1.
    `.blocks` 는 **원본 옆**에 산다 (2026-08-26 부터).
    """
    cache = Path(td) / "cache"
    (cache / "사업장가").mkdir(parents=True)
    (cache / "사업장나").mkdir(parents=True)
    conv = cache / "사업장가" / "F0AAA_산정 내역.xlsx"
    excl = cache / "사업장가" / "F0BBB_등기부등본.pdf"
    fresh = cache / "사업장나" / "F0CCC_아직 안 한 것.pdf"
    other = cache / "사업장가" / "F0DDD_산정 내역.pdf"
    old = cache / "사업장나" / "F0EEE_일보 260710.xlsx"      # 물림
    for p in (conv, excl, fresh, other, old):
        p.write_bytes(b"x" * 100)

    blocks = {}
    for tag, src in (("conv", conv), ("excl", excl), ("fresh", fresh), ("old", old)):
        d = src.with_name(src.name + ".blocks")
        d.mkdir()
        (d / "sheet-01.md").write_bytes(b"y" * 50)
        (d / "meta.json").write_bytes(b'{"a":1}')
        blocks[tag] = d

    state = Path(td) / ".doc-state.json"
    state.write_text(json.dumps({
        "slack_files": {"F0AAA": {"channel": "사업장가", "name": "산정 내역.xlsx",
                                  "doc": "사업장가/산정-내역.md", "date": "2026-08-23"}},
        "excluded": {"F0BBB": {"channel": "사업장가", "name": "등기부등본.pdf",
                               "reason": "개인 인적사항", "decided": "2026-08-23"}},
        "superseded": {"F0EEE": {"channel": "사업장나", "name": "일보 260710.xlsx",
                                 "kept": "일보 260714.xlsx", "kept_archived": False,
                                 "at": "2026-08-26"}},
        "local_files": {}, "series": {}, "skipped": {},
    }, ensure_ascii=False), encoding="utf-8")
    return state, cache, conv, excl, fresh, other, old, blocks


print("[1/10] select() 는 변환 끝난 것·일부러 뺀 것·다른 포맷 판만 고르고 미변환은 남긴다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    doomed, kept = P.select(state, cache)
    d, k = {p.name for p in doomed}, {p.name for p in kept}
    if conv.name in d:
        ok("변환 끝난 원본은 지울 것에 든다")
    else:
        bad(f"변환 끝난 원본이 빠졌습니다: {d}")
    if excl.name in d:
        ok("일부러 뺀 것도 지울 것에 든다 (다시 안 받아온다)")
    else:
        bad(f"제외분이 빠졌습니다: {d}")
    if other.name in d:
        ok("이미 변환된 자료의 다른 포맷 판도 지운다")
    else:
        bad(f"다른 포맷 판이 빠졌습니다: {d}")
    if fresh.name in k and fresh.name not in d:
        ok("아직 변환 안 된 것은 남는다 — 지우면 내일 다시 받아와 헛돈다")
    else:
        bad(f"미변환분이 지울 것에 들었습니다: {d}")

print("[2/10] 수집이 시작할 때 스스로 지운다 — 사람이 10단계를 건너뛰어도 안 쌓인다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    n, size = F.autoprune(state, cache, dry_run=False)
    # 원본 4(변환·제외·다른 포맷 판·물림) + 산출물 6(변환·제외·물림의 .blocks 각 2 파일).
    expected_n = 4 + 3 * 2
    if n == expected_n:
        ok(f"{expected_n}건 지웠다 ({size:.3f} MB)")
    else:
        bad(f"{n}건 지웠습니다 — {expected_n}건이어야 합니다")
    if not conv.exists() and not excl.exists() and not other.exists() and not old.exists():
        ok("지울 것이 실제로 사라졌다")
    else:
        bad("파일이 남아 있습니다")
    if fresh.exists():
        ok("미변환 원본은 그대로 있다")
    else:
        bad("미변환 원본이 지워졌습니다 — 내일 다시 받아옵니다")

print("[3/10] --dry-run 은 한 건도 안 지운다 — 「보기만」이 지우는 명령이 되면 안 된다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    n, _size = F.autoprune(state, cache, dry_run=True)
    if all(p.exists() for p in (conv, excl, fresh, other)):
        ok("파일이 하나도 안 지워졌다")
    else:
        bad("dry-run 인데 지워졌습니다")
    if n == 0:
        ok("지운 건수를 0 으로 보고한다")
    else:
        bad(f"dry-run 이 {n}건 지웠다고 보고합니다")

    # 상태 파일이 없으면 「전부 미변환」이므로 한 건도 지우면 안 된다.
    n2, _ = F.autoprune(Path(td) / "없는파일.json", cache, dry_run=False)
    if n2 == 0 and all(p.exists() for p in (conv, excl, fresh, other)):
        ok("상태 파일이 없으면 아무것도 안 지운다")
    else:
        bad(f"상태 파일이 없는데 {n2}건 지웠습니다")

# 엑셀 변환의 중간 산출물(`<파일ID>_<원본명>.<ext>.blocks/`)은 원본 바로 옆에 산다
# (SKILL.md 「엑셀 변환」, 2026-08-26 부터). 원본이 아닌데 원본으로 세어지면 두 가지가
# 깨진다 — ① 「아직 변환 안 된 원본 N건」이 거짓말이 되고(2026-08-23 실측: 85건 중
# 76건이 이것이었다) ② 파일 ID 로 안 걸려 영영 안 지워져 엑셀을 넣을 때마다 쌓인다.
# 원본이 변환됐으면 그 산출물도 쓸모가 끝났다. (`.blocks` 는 make() 가 이미 원본 옆에
# 파일 ID 를 붙여 만들어 둔다 — 여기서 따로 만들지 않는다.)
print("[4/10] 엑셀 중간 산출물(.blocks)은 원본으로 세지 않고, 원본이 변환됐으면 함께 지운다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    doomed, kept = P.select(state, cache)
    d, k = {p.name for p in doomed}, {str(p) for p in kept}
    if all(".blocks" not in s for s in k):
        ok("중간 산출물은 「미변환 원본」으로 안 세어진다")
    else:
        bad(f"산출물이 원본으로 세어졌습니다: {[s for s in k if '.blocks' in s]}")
    if {"sheet-01.md", "meta.json"} <= d:
        ok("변환 끝난 원본의 산출물은 지울 것에 든다")
    else:
        bad(f"산출물이 안 지워집니다 — 엑셀을 넣을 때마다 쌓입니다: {d}")

    F.autoprune(state, cache, dry_run=False)
    if not blocks["conv"].exists():
        ok("빈 산출물 폴더까지 걷힌다")
    else:
        bad(f"산출물 폴더가 남았습니다: {blocks['conv']}")
    if blocks["fresh"].exists() and (blocks["fresh"] / "sheet-01.md").exists():
        ok("아직 변환 안 된 것의 산출물은 그대로 둔다 (작업 중일 수 있다)")
    else:
        bad("변환 중인 산출물이 지워졌습니다")
    if fresh.exists():
        ok("미변환 원본도 그대로다")
    else:
        bad("미변환 원본이 지워졌습니다")

print("[5/10] 일부러 뺀 파일의 .blocks 도 지운다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    P.remove(P.select(state, cache)[0], cache)
    if not blocks["excl"].exists():
        ok("등기부등본의 .blocks 가 지워졌다")
    else:
        bad("개인정보 때문에 뺀 자료의 본문이 캐시에 남았습니다")

print("[6/10] 물림(superseded)의 원본과 .blocks 를 지운다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    P.remove(P.select(state, cache)[0], cache)
    if not old.exists():
        ok("물림 원본이 지워졌다")
    else:
        bad("물림 원본이 남았습니다")
    if not blocks["old"].exists():
        ok("물림의 .blocks 도 지워졌다")
    else:
        bad("물림의 .blocks 가 남았습니다")

print("[7/10] 아직 변환 안 된 것의 .blocks 는 남긴다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    P.remove(P.select(state, cache)[0], cache)
    if blocks["fresh"].exists():
        ok("작업 중인 산출물은 그대로다")
    else:
        bad("변환 중인 산출물이 지워졌습니다")

print("[8/10] .blocks 는 원본으로 세지 않는다 (「미변환 N건」이 거짓말이 되지 않게)")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    _doomed, _kept = P.select(state, cache)
    if all(".blocks" not in str(p) for p in _kept):
        ok("남길 목록에 산출물이 안 들어간다")
    else:
        bad(f"산출물이 원본으로 세어졌습니다: {[str(p) for p in _kept if '.blocks' in str(p)]}")

# 같은 이름의 자료가 두 사업장에 있는 일은 흔하다(`일보`·`요약본`·`산정 내역`).
# 판정은 채널까지 묶어서 내려야 한다 — 안 그러면 한쪽 사업장에서 변환을 끝냈다는
# 이유로 **다른 사업장의 아직 안 다룬 원본**이 지워지고, 그건 에러 없이 사라진다.
# `.blocks` 는 폴더 이름 맨 앞의 파일 ID 로 맞추므로 구조적으로 안 섞이지만,
# 그 사실을 지키는 그물이 없으면 다음에 이름 매칭으로 되돌아가도 아무도 안 막는다.
# (8차 설계 `2026-08-26-doc-archive-ghost-cells-and-superseded-design.md` 266행)
print("[9/10] 채널이 다른 동명 파일은 안 섞인다 — 남의 사업장 것을 지우지 않는다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    # 변환이 끝난 `사업장가/산정 내역.xlsx` 와 **이름만 같고** 채널·파일 ID 가 다른,
    # 아직 아무 판정도 안 난 원본.
    twin = cache / "사업장나" / "F0FFF01_산정 내역.xlsx"
    twin.write_bytes(b"x" * 100)
    tb = twin.with_name(twin.name + ".blocks")
    tb.mkdir()
    (tb / "sheet-01.md").write_bytes(b"y" * 50)

    doomed, kept = P.select(state, cache)
    d = {str(p) for p in doomed}
    if str(twin) not in d:
        ok("다른 사업장의 동명 원본은 안 지운다")
    else:
        bad("남의 사업장 동명 원본이 지울 것에 들었습니다 — 안 다룬 자료가 사라집니다")
    if not any(str(tb) in s for s in d):
        ok("그 원본의 산출물도 안 지운다")
    else:
        bad("남의 사업장 동명 산출물이 지울 것에 들었습니다")
    if str(twin) in {str(p) for p in kept}:
        ok("「아직 변환 안 된 원본」으로 제대로 세어진다")
    else:
        bad("남길 목록에 안 들었습니다")

# `deferred`(만기 붙은 보류)와 `skipped`(변환 실패)는 `known` 에 안 들어간다 —
# `prune_cache.py` 34~45행. `skipped` 는 수집 쪽이 아예 안 봐서 지우면 매일
# 되받아 오고, `deferred` 는 「나중에 넣기로 한 것」의 원본을 만기까지 손에
# 두려는 것이다. 그 규칙을 지키는 그물이 지금까지 없었다.
print("[10/10] `deferred`(만기 보류)·`skipped`(변환 실패)는 지우지 않는다")
with tempfile.TemporaryDirectory() as td:
    state, cache, conv, excl, fresh, other, old, blocks = make(td)
    deferred = cache / "사업장가" / "F0DEF01_보류 중인 것.xlsx"
    deferred.write_bytes(b"x" * 100)
    db = deferred.with_name(deferred.name + ".blocks")
    db.mkdir()
    (db / "sheet-01.md").write_bytes(b"y" * 50)

    skipped = cache / "사업장가" / "F0SKP01_변환 실패한 것.pdf"
    skipped.write_bytes(b"x" * 100)
    sb = skipped.with_name(skipped.name + ".blocks")
    sb.mkdir()
    (sb / "sheet-01.md").write_bytes(b"y" * 50)

    st = json.loads(state.read_text(encoding="utf-8"))
    until = (date.today() + timedelta(days=30)).isoformat()
    st["deferred"] = {"F0DEF01": {"channel": "사업장가", "name": "보류 중인 것.xlsx",
                                   "until": until}}
    st["skipped"] = {"F0SKP01": {"channel": "사업장가", "name": "변환 실패한 것.pdf",
                                  "reason": "변환 실패"}}
    state.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    doomed, kept = P.select(state, cache)
    d = {str(p) for p in doomed}
    if str(deferred) not in d:
        ok("보류 중인 원본은 지우지 않는다 — 만기 전에 지우면 다음에 다시 받아야 한다")
    else:
        bad("보류 중인 원본이 지울 것에 들었습니다 — 만기 전에 사라집니다")
    if not any(str(db) in s for s in d):
        ok("보류 원본의 산출물도 지우지 않는다")
    else:
        bad("보류 원본의 산출물이 지울 것에 들었습니다")
    if str(skipped) not in d:
        ok("변환 실패 원본은 지우지 않는다 — 지우면 수집이 매일 되받아 온다")
    else:
        bad("변환 실패 원본이 지울 것에 들었습니다 — 수집이 매일 되받아 옵니다")
    if not any(str(sb) in s for s in d):
        ok("변환 실패 원본의 산출물도 지우지 않는다")
    else:
        bad("변환 실패 원본의 산출물이 지울 것에 들었습니다")

sys.exit(1 if FAILED else 0)
