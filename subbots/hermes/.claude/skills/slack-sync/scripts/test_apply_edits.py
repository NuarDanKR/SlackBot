#!/usr/bin/env python3
"""
apply_edits.py 시험 — **반영한 건이 `.pending-edits.json` 에서 빠지는가.**

  python .claude/skills/slack-sync/scripts/test_apply_edits.py

종료코드: 0 전부 통과 / 1 실패 있음

**임시 폴더에서만 돌고 저장소는 안 건드린다.** 모듈 전역(`PENDING`·`CHANNELS`·`STATE`·`SYNC`)을
사본 쪽으로 돌려놓고 `main()` 을 그대로 부른다 — `insert_messages.py` 는 진짜를 쓴다.
반영이 실제로 md 를 고치는 경로까지 지나야 「빠진다」가 뜻이 있기 때문이다.

── 왜 남겨 두는가 ──

이 파일이 낡으면 **에러가 아니라 유령으로** 드러난다. 반영을 끝냈는데도 09:00 위생 점검과
`archive-run` 상황판이 「미반영 N건 · N일째」라고 계속 말하고, 사람은 이미 한 일을 다시
하러 간다. 2026-08-20 과 2026-08-25 에 실제로 두 번 났다 (8/20 회차 기록이
`handoff-archive-run.md` 에 「이미 반영한 유령」으로 남아 있다).

경계가 넷이라 넷 다 시험한다 — 하나만 어긋나도 조용히 틀린다.
  · 성공한 건만 뺀다        (실패한 건까지 빼면 슬랙과 md 가 다른 채로 목록에서 사라진다)
  · 전부 빠지면 파일을 지운다 (`slack-archive.js` 의 「남은 것이 없으면 파일을 지운다」 규약과 같다. 빈 목록은 「볼 것이 있다」로 읽힌다)
  · `--dry-run` 은 안 건드린다
  · 쓰는 모양이 JS 와 같다   (indent 2 · 끝 개행 — 번갈아 써도 diff 가 안 부푼다)
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_edits as A  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True

MD = """# #시험채널

> **워크스페이스**: 시험 (x.slack.com)
> **채널 ID**: C1 · [Slack에서 열기](https://x.slack.com/archives/C1)
> **기간**: 2026-08-18 ~ 2026-08-19 · **실제 메시지**: 2건

---

## 2026-08

**2026-08-19 17:31 · 김철수**
지울 메시지입니다.

**2026-08-18 09:00 · 조은지**
남을 메시지입니다.
"""


def check(label, cond, extra=""):
    global _ok
    print(("✓ " if cond else "✗ ") + label + (f"\n    {extra}" if not cond and extra else ""))
    if not cond:
        _ok = False


def item(day, hhmm, who, before):
    """`.pending-edits.json` 의 한 줄. 실제 파일과 같은 열을 갖춘다."""
    key = f"2026-08-{day} {hhmm} · {who}"
    return {
        "id": f"C1|deleted|message|{key}", "kind": "deleted", "scope": "message",
        "channel": "시험채널", "file": "시험채널", "key": key,
        "header": f"**{key}**", "replyKey": None,
        "before": before, "firstSeen": "2026-08-21",
    }


@contextlib.contextmanager
def archive(items):
    """사본 아카이브 하나. 나올 때 모듈 전역을 되돌린다."""
    keep = (A.PENDING, A.CHANNELS, A.STATE, A.SYNC)
    with tempfile.TemporaryDirectory(prefix="test-apply-edits-") as td:
        arch = Path(td)
        (arch / "channels").mkdir()
        (arch / "channels" / "시험채널.md").write_text(MD, encoding="utf-8")
        pending = arch / ".pending-edits.json"
        # **일부러 다른 모양(indent 4 · 끝 개행 없음)으로 깔아 둔다.** 진짜 파일과 같은
        # 모양으로 깔면 아래 「쓰는 모양이 JS 와 같다」가 픽스처를 보고 통과해 버려,
        # 쓰는 코드가 없어도 ✓ 가 뜬다. 코드가 다시 쓴 경우에만 맞을 수 있게 어긋내 둔다.
        pending.write_text(
            json.dumps({"generated": "2026-08-25T00:00:00.000Z", "items": items},
                       ensure_ascii=False, indent=4), encoding="utf-8")
        # load_state 는 못 읽으면 멈춘다. channels 가 비면 save_state 도 거부한다.
        (arch / ".sync-state.json").write_text(
            json.dumps({"channels": {"C1": {"last_ts": "1"}},
                        "last_sync": "2026-08-25T00:00:00.000Z"},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 파생값 맞추기는 진짜 아카이브를 훑으므로 여기서는 아무것도 안 하는 것으로 바꾼다.
        noop = arch / "noop.py"
        noop.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

        A.PENDING, A.CHANNELS, A.STATE, A.SYNC = (
            pending, arch / "channels", arch / ".sync-state.json", noop)
        try:
            yield arch
        finally:
            A.PENDING, A.CHANNELS, A.STATE, A.SYNC = keep


def run(*argv):
    """`main()` 을 그대로 부른다. (종료코드, 화면)"""
    old = sys.argv
    sys.argv = ["apply_edits.py", *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = A.main()
    finally:
        sys.argv = old
    return code, buf.getvalue()


def keys_left(arch):
    """지금 파일에 남아 있는 건들의 key. 파일이 없으면 None."""
    p = arch / ".pending-edits.json"
    if not p.exists():
        return None
    return [it["key"] for it in json.loads(p.read_text(encoding="utf-8"))["items"]]


# ── 1. 성공한 건은 빠지고, 나머지는 남는다 ──────────────────────────────

with archive([item("19", "17:31", "김철수", "지울 메시지입니다."),
              item("18", "09:00", "조은지", "남을 메시지입니다.")]) as arch:
    code, out = run("--only", "08-19", "--apply")
    left = keys_left(arch)
    check("반영한 건은 .pending-edits.json 에서 빠진다",
          left is not None and not any("08-19" in k for k in left),
          f"종료코드 {code} · 남은 것 {left}\n{out.strip()[:400]}")
    check("고르지 않은 건은 그대로 남는다",
          left is not None and any("08-18" in k for k in left), f"남은 것 {left}")
    check("md 는 실제로 고쳐졌다",
          "지울 메시지입니다." not in (arch / "channels" / "시험채널.md").read_text(encoding="utf-8"))

    body = (arch / ".pending-edits.json").read_text(encoding="utf-8")
    check("쓰는 모양이 JS 의 writeJson 과 같다 (indent 2 · 끝 개행)",
          body.endswith("\n") and '\n  "items": [' in body, repr(body[:80]))
    check("generated 는 그대로 둔다 (감지가 돈 시각이다)",
          json.loads(body).get("generated") == "2026-08-25T00:00:00.000Z")

# ── 2. 전부 빠지면 파일을 지운다 (slack-archive.js 의 「남은 것이 없으면 파일을 지운다」 규약) ─────

with archive([item("19", "17:31", "김철수", "지울 메시지입니다.")]) as arch:
    code, out = run("--only", "08-19", "--apply")
    check("남은 것이 없으면 파일 자체를 지운다 — 빈 목록은 「볼 것이 있다」로 읽힌다",
          keys_left(arch) is None, f"종료코드 {code} · 남은 것 {keys_left(arch)}\n{out.strip()[:400]}")

# ── 3. --dry-run 은 안 건드린다 ─────────────────────────────────────────

with archive([item("19", "17:31", "김철수", "지울 메시지입니다.")]) as arch:
    before = (arch / ".pending-edits.json").read_text(encoding="utf-8")
    run("--only", "08-19", "--apply", "--dry-run")
    check("--dry-run 은 파일을 안 건드린다",
          (arch / ".pending-edits.json").read_text(encoding="utf-8") == before)

# ── 4. 실패한 건은 남는다 ───────────────────────────────────────────────

with archive([item("19", "17:31", "김철수", "md 에 없는 내용입니다.")]) as arch:
    code, out = run("--only", "08-19", "--apply")
    check("반영에 실패한 건은 파일에 그대로 남는다 — 빼면 슬랙과 md 가 다른 채로 사라진다",
          keys_left(arch) is not None and any("08-19" in k for k in keys_left(arch)),
          f"종료코드 {code} · 남은 것 {keys_left(arch)}\n{out.strip()[:400]}")

# ── 5. ambiguous(같은-분 중복) 건 — 목록엔 「중복」, 골라도 거부, 파일엔 남는다 ──
# 갈래 ③ (2026-09-22): 이 건은 기계가 어느 블록인지 못 고르는 것이라 스크립트 반영이
# 없다 — md 를 사람이 고치면 다음 대조(detectChanges)가 저절로 뺀다.

amb = {
    "id": "C1|ambiguous|message|2026-08-19 17:31", "kind": "ambiguous", "scope": "message",
    "channel": "시험채널", "file": "시험채널", "key": "2026-08-19 17:31",
    "header": "**2026-08-19 17:31 · 김철수**", "replyKey": None,
    "before": "**2026-08-19 17:31 · 김철수**", "manual": True, "firstSeen": "2026-08-21",
}
with archive([amb]) as arch:
    code, out = run()
    check("목록의 종류 칸이 「중복」이다 (「삭제」로 오표기하지 않는다)",
          "중복" in out and "삭제" not in out, out.strip()[:400])
    code, out = run("--only", "08-19", "--apply")
    check("골라도 거부하고 무엇을 하라는지 말한다",
          "직접 대보고" in out, f"종료코드 {code}\n{out.strip()[:400]}")
    check("거부된 건은 파일에 그대로 남는다",
          keys_left(arch) is not None and any("08-19" in k for k in keys_left(arch)))
    check("md 는 안 건드렸다",
          (arch / "channels" / "시험채널.md").read_text(encoding="utf-8") == MD)

print("\n" + ("전부 통과" if _ok else "실패 있음"))
sys.exit(0 if _ok else 1)
