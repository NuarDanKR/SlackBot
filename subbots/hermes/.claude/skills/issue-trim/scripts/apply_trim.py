#!/usr/bin/env python3
"""검산을 통과한 제안 줄만 적용한다 — 그 한 줄만 바이트로 갈고 줄끝은 손대지 않는다.

`sed -i` 를 쓰지 않는다 — CRLF 를 LF 로 바꾼다. 2026-09-21 에 43개 파일에서 실제로
밟았고 그때 `git diff` 는 깨끗해 보였다. 이 저장소는 줄끝으로 요약 해시가 갈려 관문이
한 번도 안 걸렸던 사고를 겪은 곳이라(archive-inbox SKILL.md 「관문」 절의 「줄끝만 다른
것은 같은 내용으로 본다」가 그 흉터다) 파일을 **바이트로** 읽어 그 줄만 갈아 끼우고,
그 줄이 갖고 있던 줄끝(\r 유무)을 그대로 쓴다.

적용 전에 verify_trim 의 검산을 **스스로 한 번 더 돌린다.** 검산을 건너뛴 적용이
없게 하는 것이 이 장치의 역할이고, 덤이 하나 있다 — 제안을 만든 뒤 VM 자동 반영이
그 줄을 고쳐 base 가 밀렸으면(자료 저장소는 작업 중에도 봇이 커밋한다) 부분수열이
어긋나 여기서 멈춘다. 그때는 `git -C "$DATA" pull` 후 제안을 다시 만든다.

사용:
  python apply_trim.py --proposals <제안.json>

종료코드: 0 전부 적용 / 1 검산 실패(아무것도 안 고침) / 2 입력을 못 읽음
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import CHANNELS  # noqa: E402
import verify_trim as V  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def apply_one(md: Path, new: str) -> int:
    """그 줄만 바이트로 갈아 끼운다. 새 길이를 돌려준다.

    검산이 「정확히 하나」를 이미 봤지만 여기서 다시 센다 — 검산과 적용 사이에
    파일이 바뀌는 창이 있고, 둘이면 어느 쪽을 갈지 정할 근거가 없다."""
    raw = md.read_bytes()
    lines = raw.split(b"\n")
    mark = V.MARK.encode("utf-8")
    hits = [i for i, l in enumerate(lines) if l.startswith(mark)]
    if len(hits) != 1:
        raise RuntimeError(f"{md.name} 의 핵심 쟁점 줄이 {len(hits)}개 — 멈춥니다")
    i = hits[0]
    crlf = lines[i].endswith(b"\r")          # 그 줄의 줄끝을 그대로 쓴다
    lines[i] = new.encode("utf-8") + (b"\r" if crlf else b"")
    md.write_bytes(b"\n".join(lines))
    return len(new)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="검산 통과한 핵심 쟁점 줄을 바이트 단위로 적용한다")
    ap.add_argument("--proposals", required=True, help="제안 JSON (verify_trim.py 와 같은 파일)")
    ap.add_argument("--channels-dir", default=str(CHANNELS),
                    help="채널 md 폴더 (기본: 자료 저장소 — 시험이 갈아끼운다)")
    args = ap.parse_args(argv)

    try:
        proposals = json.loads(Path(args.proposals).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"! 제안 파일을 못 읽습니다: {e}", file=sys.stderr)
        return 2
    if not isinstance(proposals, dict) or not proposals:
        print("! 제안이 비었거나 모양이 아닙니다.", file=sys.stderr)
        return 2

    channels_dir = Path(args.channels_dir)
    rows, bad = V.verify_all(proposals, channels_dir, V.issue_line_max())
    if bad:
        for fname, _o, _n, probs, _w in rows:
            if probs:
                print(f"✗ {fname} — {' · '.join(probs)}", file=sys.stderr)
        print(f"\n★ 검산 실패 {bad}건 — 아무것도 고치지 않았습니다. "
              "verify_trim.py 로 확인하세요.", file=sys.stderr)
        return 1

    changed = 0
    for fname, spec in sorted(proposals.items()):
        new, _corr = V._spec(spec)
        n = apply_one(channels_dir / fname, new)
        changed += 1
        print(f"  {fname} → {n}자")
    print(f"\n{changed}개 파일 적용. 이제 decide_work.py --show 로 도장을 받으세요 "
          "(archive-inbox 6단계 — `>` 메타 전체가 요약 자리라 도장 없이는 커밋이 막힙니다).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
