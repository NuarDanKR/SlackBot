#!/usr/bin/env python3
"""
`archive-template/slack-export/index.md`(새 팀이 처음 받는 빈 씨앗)에서 `sync_index.py`
가 첫 반영부터 통과하나.

  python .claude/skills/slack-sync/scripts/test_fresh_seed.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 필요한가 (2026-09-02 전수조사) ──

`sync_index.py` 의 `TOTAL_RE` 는 `'> **채널**: 총 N개 (공개 A + 비공개 B)'` 모양을 요구하는데,
씨앗의 그 줄은 `'> **채널**: 총 0개'` — 괄호 안 공개/비공개 내역이 아예 없었다. 그러면
`sync_index()` 가 그 줄을 못 찾아 매 회차 `total_not_found` 를 `unresolved` 에 얹고,
자동 반영은 그걸 "'…' 줄을 못 찾았습니다" 로 DM 에 띄운다 — **새 팀은 첫날부터 매일** 이
경고를 본다.

우리 기계에는 이미 채워진 실물 `index.md` 가 있어서 `test_sync_index.py`(실물 아카이브
사본으로 도는 시험)는 이 경로를 영영 안 밟는다. 그래서 씨앗만 따로 복사해 재는 시험이
따로 필요하다 — 2026-09-03 에 씨앗의 그 줄을 `'> **채널**: 총 0개 (공개 0 + 비공개 0)'`
로 맞췄다(파서 쪽 대신 씨앗 쪽을 고쳤다 — 실물 아카이브가 전부 이 모양이라 파서를
느슨하게 두면 실물과 다른 두 번째 모양이 하나 더 생긴다).
"""

import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import sync_index as S  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SEED = _HERE.parents[3] / "archive-template" / "slack-export" / "index.md"

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def run_against(text: str):
    """주어진 index.md 내용으로 빈 채널 폴더에서 sync_index() 를 한 번 돌려 결과를 낸다."""
    tmp = Path(tempfile.mkdtemp(prefix="fresh-seed-test-"))
    try:
        (tmp / "channels").mkdir()
        (tmp / "index.md").write_text(text, encoding="utf-8", newline="\n")
        old_channels, old_index, old_state = S.CHANNELS, S.INDEX, S.STATE
        S.CHANNELS, S.INDEX, S.STATE = tmp / "channels", tmp / "index.md", tmp / ".sync-state.json"
        try:
            return S.sync_index({}, dry=True)
        finally:
            S.CHANNELS, S.INDEX, S.STATE = old_channels, old_index, old_state
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if not SEED.exists():
        print(f"✗ 씨앗이 없습니다: {SEED}", file=sys.stderr)
        return 1

    # ── 1. 지금 씨앗 그대로 — 통과해야 한다 ──
    seed_text = SEED.read_text(encoding="utf-8")
    changed, unresolved = run_against(seed_text)
    kinds = [u["kind"] for u in unresolved]
    check("지금 씨앗에서 total_not_found 가 안 뜬다", "total_not_found" not in kinds, kinds)
    check("지금 씨앗에서 아무것도 안 고친다 (이미 맞는 값)", not changed, changed)

    # ── 2. 이빨 확인 — 예전 모양(괄호 없음)을 먹이면 실제로 잡히나 ──
    # 2026-09-02 전수조사 당시의 실물 문구를 그대로 재현한다(파일에서 다시 읽지 않고
    # 문자열로 박아 둔다 — 씨앗이 이미 고쳐진 지금, 그 사고 모양은 여기서만 남는다).
    old_shape = seed_text.replace(
        "> **채널**: 총 0개 (공개 0 + 비공개 0)",
        "> **채널**: 총 0개",
    )
    check("이빨 확인 — 예전 모양(seed_text 안에 실제로 있었다)", old_shape != seed_text)
    _, unresolved_old = run_against(old_shape)
    kinds_old = [u["kind"] for u in unresolved_old]
    check("예전 모양이면 total_not_found 가 뜬다 (이 시험에 이빨이 있다)",
          "total_not_found" in kinds_old, kinds_old)

    print("\n" + ("전부 통과" if _ok else "실패 있음"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
