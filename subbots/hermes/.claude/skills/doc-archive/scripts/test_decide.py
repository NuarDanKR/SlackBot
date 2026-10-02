#!/usr/bin/env python3
"""
decide.py 의 `--superseded` 시험.

  python .claude/skills/doc-archive/scripts/test_decide.py

**임시 폴더에서만 돈다** — 진짜 `.doc-state.json` 은 안 건드린다.
기존 경로(`--skip`·`--later`·`--undo`)는 안 덮는다. 새로 넣는 길만 본다.

**WORKAROUND (2026-08-26):** 브리프 원문의 mock 파일 ID `F0OLD`/`F0NEW`/`F0EXC`
는 5자라 `decide.py` 의 기존 `SLACK_ID_RE`(`F` + 6자 이상, 이 Task 범위 밖)를
통과하지 못해 정확한-ID 조회 경로를 못 타고, 이름 조각 검색으로도 안 걸려
selector 로 못 쓴다. 실물 슬랙 파일 ID 는 항상 이보다 길어 실제로는 안 나는
문제이지만, 시험에서는 걸린다. 여기서는 그 mock ID 를 7자 이상으로 늘려
(`F0OLD01` 등) 같은 시험을 그대로 통과시켰다 — `decide.py` 는 브리프가 지정한
범위(`put()`·`show()`·`main()`·`resolve()` 의 `pools`)만 고쳤다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent
DECIDE = HERE / "decide.py"
FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def make_state(td, **extra):
    """물림 대상 1건 + 최신 1건이 아카이브에 있는 상태 파일."""
    p = Path(td) / ".doc-state.json"
    base = {
        "slack_files": {
            "F0NEW01": {"channel": "비공개나", "name": "이슈사항_260814.xlsm",
                      "doc": "비공개나/이슈사항.md", "date": "2026-08-18"},
            "F0OLD01": {"channel": "비공개나", "name": "이슈사항_260807.xlsm",
                      "doc": "비공개나/이슈사항-old.md", "date": "2026-08-07"},
        },
        "local_files": {}, "series": {}, "skipped": {},
        "excluded": {}, "deferred": {}, "superseded": {},
    }
    base.update(extra)
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return p


def run(state, *args):
    r = subprocess.run(
        [sys.executable, str(DECIDE), "--state", str(state), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def load(state):
    return json.loads(Path(state).read_text(encoding="utf-8"))


print("[1/12] --superseded 가 파일 ID 를 키로 적는다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    code, out = run(st, "F0OLD01", "--superseded", "이슈사항_260814.xlsm")
    d = load(st).get("superseded", {})
    if "F0OLD01" in d:
        ok("superseded 에 F0OLD01 가 적혔다")
    else:
        bad(f"안 적혔습니다 (종료 {code}) — {out[:200]}")
    v = d.get("F0OLD01", {})
    if v.get("kept") == "이슈사항_260814.xlsm":
        ok("kept 에 최신 이름이 적혔다")
    else:
        bad(f"kept 가 {v.get('kept')!r} 입니다")
    if v.get("channel") == "비공개나" and v.get("name") == "이슈사항_260807.xlsm":
        ok("channel·name 이 excluded 와 같은 모양으로 적혔다")
    else:
        bad(f"channel/name 이 {v.get('channel')!r}/{v.get('name')!r} 입니다")

print("[2/12] 최신이 아카이브에 있으면 kept_archived 가 true")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    run(st, "F0OLD01", "--superseded", "이슈사항_260814.xlsm")
    if load(st)["superseded"]["F0OLD01"].get("kept_archived") is True:
        ok("true")
    else:
        bad("kept_archived 가 true 가 아닙니다")

print("[3/12] 최신이 아카이브에 없으면 경고하고 kept_archived 가 false — 막지는 않는다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td, excluded={"F0EXC01": {"channel": "비공개나",
                                            "name": "이슈사항_260820.xlsm",
                                            "reason": "개인정보", "decided": "2026-08-26"}})
    code, out = run(st, "F0OLD01", "--superseded", "이슈사항_260820.xlsm")
    v = load(st).get("superseded", {}).get("F0OLD01", {})
    if v:
        ok("막지 않고 적었다")
    else:
        bad(f"안 적었습니다 (종료 {code}) — {out[:200]}")
    if v.get("kept_archived") is False:
        ok("kept_archived 가 false")
    else:
        bad(f"kept_archived 가 {v.get('kept_archived')!r} 입니다")
    if "아카이브에 없" in out:
        ok("화면에 경고가 찍혔다")
    else:
        bad(f"경고가 없습니다 — {out[:300]}")
    if "excluded" in out or "일부러 뺀" in out:
        ok("그 최신이 어느 칸에 있는지도 알려준다")
    else:
        bad("최신이 어디 있는지 안 알려줍니다")

print("[4/12] 이미 다른 칸에 결정이 있으면 멈춘다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td, excluded={"F0OLD01": {"channel": "비공개나",
                                            "name": "이슈사항_260807.xlsm",
                                            "reason": "개인정보", "decided": "2026-08-26"}})
    code, out = run(st, "F0OLD01", "--superseded", "이슈사항_260814.xlsm")
    if code != 0 and "이미" in out:
        ok("멈추고 앞선 판정을 보여준다")
    else:
        bad(f"조용히 덮었습니다 (종료 {code}) — {out[:200]}")
    if not load(st).get("superseded"):
        ok("superseded 에 아무것도 안 적혔다")
    else:
        bad("멈춘다면서 적었습니다")

print("[5/12] --clear 가 superseded 도 지운다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    run(st, "F0OLD01", "--superseded", "이슈사항_260814.xlsm")
    code, out = run(st, "F0OLD01", "--clear")
    if not load(st).get("superseded"):
        ok("지워졌다")
    else:
        bad(f"안 지워졌습니다 (종료 {code}) — {out[:200]}")

print("[6/12] --list 에 물림이 뜬다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    run(st, "F0OLD01", "--superseded", "이슈사항_260814.xlsm")
    code, out = run(st, "--list")
    if "이슈사항_260807.xlsm" in out and "260814" in out:
        ok("옛 판과 그것을 대신한 최신이 함께 보인다")
    else:
        bad(f"--list 에 안 보입니다 — {out[:300]}")

print("[7/12] slack_files 에 없는 파일도 물림으로 적고 되돌릴 수 있다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    # 변환한 적 없는 파일 — 매니페스트로만 고를 수 있다
    man = Path(td) / "man.json"
    man.write_text(json.dumps({"downloaded": [
        {"id": "F0GHOST", "channel": "사업장나", "name": "일보 260710.xlsx"}]},
        ensure_ascii=False), encoding="utf-8")
    code, out = run(st, "--manifest", str(man), "F0GHOST",
                    "--superseded", "일보 260714.xlsx")
    if "F0GHOST" in load(st).get("superseded", {}):
        ok("매니페스트로 골라 적었다")
    else:
        bad(f"안 적혔습니다 (종료 {code}) — {out[:200]}")
    # 이번엔 매니페스트 **없이** 되돌린다 — superseded 칸에서 찾아야 한다
    code, out = run(st, "F0GHOST", "--clear")
    if not load(st).get("superseded"):
        ok("매니페스트 없이 되돌렸다")
    else:
        bad(f"못 되돌렸습니다 (종료 {code}) — {out[:200]}")

print("[8/12] --skip 이 excluded.md 를 함께 쓴다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    code, out = run(st, "F0OLD01", "--skip", "--reason", "개인정보")
    md = Path(td) / "excluded.md"
    if md.exists():
        ok("excluded.md 가 생겼다")
    else:
        bad(f"안 생겼습니다 (종료 {code}) — {out[:300]}")
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    if "이슈사항_260807.xlsm" in text and "개인정보" in text and "F0OLD01" in text:
        ok("파일명·사유·파일 ID 가 표에 실렸다")
    else:
        bad(f"내용이 빠졌습니다 — {text[-400:]}")
    if "#비공개나" in text and "총 **1건**" in text:
        ok("채널별로 묶이고 총계가 붙는다")
    else:
        bad(f"묶음/총계가 없습니다 — {text[-400:]}")

print("[9/12] --clear 로 되돌리면 목록에서도 빠진다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    run(st, "F0OLD01", "--skip", "--reason", "개인정보")
    run(st, "F0OLD01", "--clear")
    text = (Path(td) / "excluded.md").read_text(encoding="utf-8")
    if "이슈사항_260807.xlsm" not in text and "총 **0건**" in text:
        ok("빠졌다")
    else:
        bad(f"남아 있습니다 — {text[-400:]}")

print("[10/12] --render 는 상태 파일만 읽어 다시 쓴다 · 사유의 | 로 표가 안 깨진다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td, excluded={"F0PIPE01": {
        "channel": "비공개나", "name": "표.xlsx",
        "reason": "a | b 두 갈래", "decided": "2026-08-27"}})
    code, out = run(st, "--render")
    text = (Path(td) / "excluded.md").read_text(encoding="utf-8")
    row = next((l for l in text.split("\n") if "표.xlsx" in l), "")
    if row.replace(r"\|", "").count("|") == 5 and r"\|" in row:
        ok("파이프가 이스케이프돼 칸이 넷 그대로다")
    else:
        bad(f"표가 깨졌습니다 — {row!r}")
    code, out = run(st, "--render")
    if "변화 없음" in out:
        ok("내용이 그대로면 파일을 안 건드린다")
    else:
        bad(f"헛 diff 를 만듭니다 — {out[:200]}")

print("[11/12] --undo 에 문서 트리 밖의 md 가 오면 손대기 전에 거절한다")
with tempfile.TemporaryDirectory() as td:
    st = make_state(td)
    stray = Path(td) / "남의 폴더에 있는 것.md"
    before = "## 2026-08\n\n**2026-08-18 · 이슈사항_260814.xlsm**\n\n내용\n"
    stray.write_text(before, encoding="utf-8")
    code, out = run(st, "--undo", "--file", str(stray),
                    "--date", "2026-08-18", "--source", "이슈사항_260814.xlsm")
    if code == 1 and "밖의 파일입니다" in out:
        ok("종료코드 1 과 사유를 낸다 — 예전에는 0 으로 조용히 끝났다")
    else:
        bad(f"거절하지 않습니다 (종료 {code}) — {out[:200]}")
    # 거절이 `remove_entry` 앞에 있어야 한다. 뒤에 있으면 남의 파일을 고쳐 놓고 거절한다.
    if stray.read_text(encoding="utf-8") == before:
        ok("그 파일을 건드리지 않았다")
    else:
        bad("거절하면서 md 를 이미 고쳤습니다 — 검사가 remove_entry 뒤에 있습니다")

print("[12/12] --list 는 저장된 kept_archived 를 안 믿는다 — 사슬이 늘어 최신이 빠졌으면 경고가 뜬다")
with tempfile.TemporaryDirectory() as td:
    # 적던 날에는 최신이 아카이브에 있어 kept_archived: true 로 저장됐는데,
    # 그 뒤 그 최신도 물려 slack_files 에서 빠진 상태를 그대로 만든다(실물 사슬 5건의 모양).
    st = make_state(td, superseded={"F0OLD01": {
        "channel": "비공개나", "name": "이슈사항_260807.xlsm",
        "kept": "이슈사항_없어진판.xlsm", "kept_archived": True,
        "at": "2026-08-26"}})
    code, out = run(st, "--list")
    if "아카이브에 없" in out:
        ok("저장값이 true 여도 지금 없는 최신에는 경고가 뜬다")
    else:
        bad(f"경고가 없습니다 — 저장값을 그대로 믿고 있습니다: {out[:300]}")

sys.exit(1 if FAILED else 0)
