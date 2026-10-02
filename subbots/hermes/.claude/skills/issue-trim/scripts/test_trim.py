#!/usr/bin/env python3
"""verify_trim / apply_trim 시험 — 결함을 일부러 넣어 **검사가 실제로 발동하는지** 본다.
임시 폴더에서만 돌고 진짜 아카이브는 안 건드린다.

  python .claude/skills/issue-trim/scripts/test_trim.py

종료코드: 0 전부 통과 / 1 실패 있음

「오늘 돌렸더니 0건」은 검사가 맞다는 증거가 아니다 — 아무것도 안 보는 검사도 0건을
낸다. 그래서 결함(새 글자 유입·줄 위치 이탈·중복 줄)을 넣어 **빨개지는 것**과, 정상
제안이 **안 잡히는 것**을 함께 확인한다. 줄끝은 바이트로 확인한다 — `git diff` 는
CRLF→LF 를 안 보여줘서 이 시험 말고는 잡을 자리가 없다.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_trim as A  # noqa: E402
import verify_trim as V  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


# ── 픽스처 — 실물 채널 md 와 같은 뼈대, 이름은 가짜 ─────────────────────────

OLD_LINE = ("> **핵심 쟁점**: 분양 잔여 세대 협의 · 8/10 미납입 107실 → 102실 · "
            "9/10 미납입 94실 → 93실 · 준공 검사 예정")
NEW_LINE = ("> **핵심 쟁점**: 분양 잔여 세대 협의 · "
            "9/10 미납입 94실 → 93실 · 준공 검사 예정")          # 덮인 8/10 기록만 뺐다

BODY = [
    "# #사업장가",
    "",
    "> **기간**: 2026-01-05 ~ 2026-09-10 · **실제 메시지**: 2건",
    OLD_LINE,
    "",
    "---",
    "",
    "## 2026-09",
    "",
    "**2026-09-10 09:00 · 김담당**",
    "미납입 94실 → 93실 갱신 보고드립니다.",
    "",
    "## 2026-08",
    "",
    "**2026-08-10 09:00 · 김담당**",
    "미납입 107실 → 102실입니다.",
    "",
]


def make(dirpath: Path, name: str, eol: str, lines=None) -> Path:
    p = dirpath / name
    p.write_bytes(eol.join(lines or BODY).encode("utf-8") + eol.encode("utf-8"))
    return p


def run(tmp: Path):
    limit = 400

    # ① 상한을 정본에서 읽는다 — 값 자체를 하드코딩해 재지 않는다 (바뀌면 시험이 아니라
    #    verify_trim 이 따라가야 하는 값이다). 읽히고 양수인 것까지만 본다.
    real = V.issue_line_max()
    check("① ISSUE_LINE_MAX 를 check-setup.js 에서 읽는다", isinstance(real, int) and real > 0,
          f"({real})")

    # ② 정상 트림 — 통과해야 한다 (오탐이 나기 시작하면 아무도 안 읽는다)
    make(tmp, "사업장가.md", "\r\n")
    rows, bad = V.verify_all({"사업장가.md": NEW_LINE}, tmp, limit)
    check("② 덮인 기록만 뺀 제안은 통과", bad == 0, str(rows[0][3]))

    # ③ 새 글자 유입 — ★ 로 걸려야 한다 (「108실」은 원본에 없다)
    inj = NEW_LINE.replace("93실", "108실")
    rows, bad = V.verify_all({"사업장가.md": inj}, tmp, limit)
    check("③ 원본에 없는 글자는 ★ 로 걸린다",
          bad == 1 and any("★" in p for p in rows[0][3]))

    # ④ 원본과 같은 줄 — 낼 것이 없다
    rows, bad = V.verify_all({"사업장가.md": OLD_LINE}, tmp, limit)
    check("④ 원본 그대로는 거절", bad == 1)

    # ⑤ 핵심 쟁점 줄이 둘 — 멈춘다
    twice = BODY[:4] + [OLD_LINE] + BODY[4:]
    make(tmp, "중복.md", "\n", twice)
    rows, bad = V.verify_all({"중복.md": NEW_LINE}, tmp, limit)
    check("⑤ 줄이 2개면 거절", bad == 1 and "2개" in rows[0][3][0])

    # ⑥ 줄이 머리말 밖(첫 `## ` 뒤) — 봇 프롬프트에 안 실리는 자리다
    moved = [l for l in BODY if l != OLD_LINE]
    moved.insert(moved.index("## 2026-09") + 1, OLD_LINE)
    make(tmp, "이탈.md", "\n", moved)
    rows, bad = V.verify_all({"이탈.md": NEW_LINE}, tmp, limit)
    check("⑥ 머리말 밖의 줄은 거절", bad == 1 and any("머리말" in p for p in rows[0][3]))

    # ⑦ 별건 정정 — 되돌리면 부분수열이라 통과, 문구가 안 보이면 거절
    fixed = NEW_LINE.replace("94실 → 93실", "94실 → 92실(9/12 정정)")
    spec = {"line": fixed, "corrections": [{"old": "94실 → 93실", "new": "94실 → 92실(9/12 정정)"}]}
    rows, bad = V.verify_all({"사업장가.md": spec}, tmp, limit)
    check("⑦-a 승인된 정정은 되돌려 재서 통과", bad == 0, str(rows[0][3]))
    spec2 = {"line": fixed, "corrections": [{"old": "94실 → 93실", "new": "없는 문구"}]}
    rows, bad = V.verify_all({"사업장가.md": spec2}, tmp, limit)
    check("⑦-b 정정 문구가 제안 줄에 없으면 거절",
          bad == 1 and any("1회가 아니다" in p for p in rows[0][3]))
    spec3 = {"line": fixed, "corrections": []}
    rows, bad = V.verify_all({"사업장가.md": spec3}, tmp, limit)
    check("⑦-c 정정 선언 없이 새 글자를 넣으면 ★",
          bad == 1 and any("★" in p for p in rows[0][3]))

    # ⑧ 상한 초과 — 문제(✗)가 아니라 경고(⚠)다. 「못 줄인다」가 정답인 채널이 있다
    long_old = "> **핵심 쟁점**: " + "가나다 " * 130          # 400자 훌쩍
    long_new = "> **핵심 쟁점**: " + "가나다 " * 120
    make(tmp, "긴채널.md", "\n", [l if l != OLD_LINE else long_old.rstrip() for l in BODY])
    rows, bad = V.verify_all({"긴채널.md": long_new.rstrip()}, tmp, limit)
    check("⑧ 상한 초과는 경고만 — 적용은 막지 않는다",
          bad == 0 and rows[0][4] and "초과" in rows[0][4][0])

    # ⑨ 적용 — CRLF 파일에서 그 줄만 바뀌고 나머지 바이트·줄끝은 그대로
    md = tmp / "사업장가.md"
    before = md.read_bytes()
    prop = tmp / "prop.json"
    prop.write_text(json.dumps({"사업장가.md": NEW_LINE}, ensure_ascii=False), encoding="utf-8")
    rc = A.main(["--proposals", str(prop), "--channels-dir", str(tmp)])
    after = md.read_bytes()
    expect = before.replace(OLD_LINE.encode("utf-8") + b"\r",
                            NEW_LINE.encode("utf-8") + b"\r", 1)
    check("⑨-a 적용 성공 (rc 0)", rc == 0)
    check("⑨-b 그 줄만 바뀌고 CRLF 는 바이트 그대로", after == expect)
    check("⑨-c LF 가 새로 생기지 않았다", after.count(b"\r\n") == before.count(b"\r\n") )

    # ⑩ LF 파일에는 \r 을 새로 만들지 않는다
    md_lf = make(tmp, "엘에프.md", "\n")
    prop.write_text(json.dumps({"엘에프.md": NEW_LINE}, ensure_ascii=False), encoding="utf-8")
    rc = A.main(["--proposals", str(prop), "--channels-dir", str(tmp)])
    check("⑩ LF 파일 적용 뒤에도 \\r 0개", rc == 0 and b"\r" not in md_lf.read_bytes())

    # ⑪ 검산에 걸리는 제안은 apply 가 스스로 거절하고 파일을 안 건드린다
    md2 = make(tmp, "사업장나.md", "\r\n")
    before = md2.read_bytes()
    prop.write_text(json.dumps({"사업장나.md": inj}, ensure_ascii=False), encoding="utf-8")
    rc = A.main(["--proposals", str(prop), "--channels-dir", str(tmp)])
    check("⑪ 검산 실패면 적용 거절 + 파일 그대로", rc == 1 and md2.read_bytes() == before)

    # ⑫ 없는 파일 — 조용한 0건이 아니라 거절
    rows, bad = V.verify_all({"없는채널.md": NEW_LINE}, tmp, limit)
    check("⑫ 없는 파일은 거절", bad == 1 and "파일이 없다" in rows[0][3])


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        run(Path(d))
    print("\n전부 통과" if _ok else "\n실패 있음")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
