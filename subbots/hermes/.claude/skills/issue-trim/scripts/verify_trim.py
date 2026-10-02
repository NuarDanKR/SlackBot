#!/usr/bin/env python3
"""제안 줄을 적용 전에 독립 검산한다 — 만든 쪽이 「확인했다」고 한 것을 기계가 다시 잰다.

「새 글자가 안 들어갔다」의 유일한 증명은 **부분수열 검사**다: 제안 줄이 원본 줄에서
글자를 **빼기만** 한 것인지 두-포인터로 확인한다. 눈 대조는 증명이 아니다 — 2026-09-21
회차에서는 11줄 전부 이 검사를 통과시킨 뒤에만 적용했고, 병렬로 만든 제안이라 더더욱
만든 쪽의 「확인했다」를 믿지 않았다.

사용:
  python verify_trim.py --proposals <제안.json>
  (시험은 verify_all() 을 직접 부른다 — channels_dir 로 바탕을 갈아끼운다)

제안 파일 모양 — **커밋에 안 담기는 임시 자리**(세션 스크래치패드 등)에 둘 것.
저장소 안에 두면 커밋에 딸려 들어가고, 세션 임시 폴더에 이 스크립트가 기대면
그 폴더가 사라질 때 절차가 함께 죽는다. 그래서 경로는 늘 인자로 받는다:

  {
    "사업장가.md": "> **핵심 쟁점**: …",                        ← 줄이기만
    "사업장나.md": {                                            ← 줄이기 + 별건 정정
      "line": "> **핵심 쟁점**: …",
      "corrections": [{"old": "약 232억 예상", "new": "약 191억 예상(8/31 오기 수정)"}]
    }
  }

`corrections` 는 **본문 근거를 대고 사람 승인을 받은 별건 정정**만 담는다 (SKILL.md 의
「낡은 숫자」 절). 검산은 정정 문구를 원래 표현으로 되돌린 뒤 부분수열을 재므로,
승인 없이 끼워 넣은 새 문장은 여기 안 적으면 그대로 ★ 로 걸린다.

보는 것:
  1. 그 파일에 `> **핵심 쟁점**` 줄이 정확히 하나, 머리말(첫 `## ` 앞)에 있나
  2. 제안이 `> **핵심 쟁점**: ` 로 시작하고 줄끝 문자가 안 들어 있나
  3. **원본에서 글자를 빼기만 했나** (부분수열). 정정이 있으면 그 `new` 문구가 제안 줄에
     정확히 1회 있는지 보고, `old` 로 되돌린 뒤 잰다
  4. 원본과 같은 줄을 내지 않았나 (할 일이 없는 제안)
  5. 상한을 넘으면 **경고만** 한다 — 「못 줄인다」가 정답인 채널이 실제로 있다
     (2026-09-21 회차 14채널 중 7개). 상한의 정본은 `check-setup.js` 의 `ISSUE_LINE_MAX`

종료코드: 0 이상 없음 / 1 문제 있음(적용 금지) / 2 입력을 못 읽음
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import CHANNELS, CODE_ROOT  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 이 두 모양은 계약이다 — 봇(`src/archive.js` 의 `channelBrief`)과
# 검사(`check-setup.js` 의 ISSUE_LINE_MAX 블록)가 같은 접두로 그 줄을 찾는다.
MARK = "> **핵심 쟁점**"
PREFIX = "> **핵심 쟁점**: "


def issue_line_max() -> int:
    """상한의 정본은 `check-setup.js` 의 `ISSUE_LINE_MAX` 다.

    여기 400 을 다시 적으면 「두 곳에 나뉜 같은 판정」이 되어 언젠가 갈리고, 갈려도
    에러가 안 난다 (CLAUDE.md 「고쳤으면 돌리는 것」). 그래서 그 파일에서 읽고,
    못 찾으면 추측하지 않고 멈춘다 — 이름이 바뀌었으면 이 함수를 함께 고친다."""
    src = (CODE_ROOT / "scripts" / "check-setup.js").read_text(encoding="utf-8")
    m = re.search(r"const ISSUE_LINE_MAX = (\d+);", src)
    if not m:
        raise RuntimeError(
            "check-setup.js 에서 ISSUE_LINE_MAX 를 못 찾았습니다 — "
            "상수 이름이 바뀌었으면 verify_trim.issue_line_max 도 고치세요")
    return int(m.group(1))


def is_subsequence(small: str, big: str) -> bool:
    """small 이 big 에서 글자를 빼기만 한 것인가 (두-포인터)."""
    it = iter(big)
    return all(ch in it for ch in small)


def issue_line(path: Path):
    """(hits, 머리말 끝). hits = [(줄번호, 줄내용 — CR 뗀 것)].

    머리말 = 첫 `## ` 헤딩 앞까지. 그 줄이 머리말 밖에 있으면 봇 프롬프트에 안 실리는
    자리라(`channelBrief` 는 상단 `>` 블록만 읽는다), 고쳐도 목적을 빗나간다."""
    lines = path.read_text(encoding="utf-8").split("\n")
    stop = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    hits = [(i, l.rstrip("\r")) for i, l in enumerate(lines) if l.startswith(MARK)]
    return hits, stop


def _spec(spec):
    """제안 항목을 (줄, 정정 목록) 으로 편다."""
    if isinstance(spec, str):
        return spec, []
    return spec.get("line", ""), list(spec.get("corrections") or [])


def verify_one(md: Path, spec, limit: int):
    """한 파일 검산. (원본 줄 or None, 새 줄, 문제 목록, 경고 목록)"""
    new, corrections = _spec(spec)
    probs, warns = [], []

    if not md.exists():
        return None, new, ["파일이 없다"], warns
    hits, stop = issue_line(md)
    if len(hits) != 1:
        return None, new, [f"핵심 쟁점 줄이 {len(hits)}개다"], warns
    idx, old = hits[0]
    if idx >= stop:
        return old, new, ["그 줄이 머리말 밖에 있다 — 봇 프롬프트에 안 실리는 자리다"], warns

    if not new.startswith(PREFIX):
        probs.append("접두가 다르다")
    if "\n" in new or "\r" in new:
        probs.append("줄끝 문자가 들어 있다")

    # 정정을 원래 표현으로 되돌린 뒤 부분수열을 잰다.
    check = new
    for c in corrections:
        c_old, c_new = str(c.get("old", "")), str(c.get("new", ""))
        if not c_old or not c_new or c_old == c_new:
            probs.append("정정 항목의 old/new 가 비었거나 같다")
            continue
        n = check.count(c_new)
        if n != 1:
            probs.append(f"정정 문구가 제안 줄에 1회가 아니다({n}회): «{c_new[:30]}»")
            continue
        check = check.replace(c_new, c_old, 1)

    if not probs and not is_subsequence(check, old):
        probs.append("★ 원본에 없는 글자가 들어갔다 — 적용 금지")
    if new == old:
        probs.append("원본과 같다 — 낼 것이 없다")
    if len(new) > limit:
        warns.append(f"상한 초과 {len(new) - limit}자 — 더 못 빼면 줄이지 말고 사람에게 묻는다")
    return old, new, probs, warns


def verify_all(proposals: dict, channels_dir: Path, limit: int):
    """전 제안 검산. (표 행 목록, 문제 건수). 행 = (파일, 원본길이, 새길이, 문제, 경고)"""
    rows = []
    bad = 0
    for fname, spec in sorted(proposals.items()):
        old, new, probs, warns = verify_one(channels_dir / fname, spec, limit)
        rows.append((fname, len(old) if old is not None else 0, len(new), probs, warns))
        if probs:
            bad += 1
    return rows, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="핵심 쟁점 줄 제안을 적용 전에 검산한다")
    ap.add_argument("--proposals", required=True, help="제안 JSON (임시 자리에 둔 것)")
    ap.add_argument("--channels-dir", default=str(CHANNELS),
                    help="채널 md 폴더 (기본: 자료 저장소 — 시험이 갈아끼운다)")
    args = ap.parse_args(argv)

    try:
        proposals = json.loads(Path(args.proposals).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"! 제안 파일을 못 읽습니다: {e}", file=sys.stderr)
        return 2
    if not isinstance(proposals, dict) or not proposals:
        print("! 제안이 비었거나 모양이 아닙니다 — {\"<채널>.md\": \"<줄>\"} 사전이어야 합니다.",
              file=sys.stderr)
        return 2

    limit = issue_line_max()
    rows, bad = verify_all(proposals, Path(args.channels_dir), limit)

    print(f"제안 {len(rows)}건 (상한 {limit}자 — 정본: check-setup.js ISSUE_LINE_MAX)\n")
    saved = 0
    for fname, old_len, new_len, probs, warns in rows:
        mark = "✓" if not probs else "✗"
        note = "" if not probs else "  ← " + " · ".join(probs)
        over = "" if not warns else "  ⚠ " + " · ".join(warns)
        print(f"{mark} {fname:<28} {old_len:>5} → {new_len:>4}{over}{note}")
        if not probs:
            saved += old_len - new_len
    print(f"\n전체 감소: {saved:,}자")
    print("이상 없음 — 승인을 받은 뒤 apply_trim.py 로 적용하세요." if not bad
          else f"★ {bad}건 문제 — 적용하지 않는다")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
