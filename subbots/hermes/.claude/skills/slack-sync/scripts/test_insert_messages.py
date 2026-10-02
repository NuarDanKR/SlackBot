#!/usr/bin/env python3
"""
insert_messages.py 의 `insert_block` 묶음 나누기 시험.

  python .claude/skills/slack-sync/scripts/test_insert_messages.py

**임시 폴더에서만 돈다** — 진짜 아카이브는 안 건드린다.

**왜 있나** — 넣을 블록 중 일부가 「이미 반영됨」으로 걸러지면, 남은 것만 다시 이어
붙인다. 그때 묶음을 가르는 판정이 넓으면 **본문 안의 헤더 흉내 줄**에서 한 블록이
둘로 갈리고, 이어 붙이면서 그 자리에 **없던 빈 줄이 낀다.** 아카이브 md 는
`archive.js` 가 파싱하는 계약이라 이런 변형은 에러 없이 모양만 바꾼다.

경계는 최상위 메시지 헤더(`**YYYY-MM-DD HH:MM · 이름**`)와 월 헤딩(`## YYYY-MM`)만
본다. 좁혀도 잃는 것이 없다는 것은 2026-08-29 에 전 채널로 쟀다 — 정상 헤더
1,604건은 전부 `BLOCK_START_RE` 에도 걸리고 `MSG_HEADER_RE` 에만 걸리는 줄은 0건,
한 줄로 줄여 적은 38건은 `BLOCK_START_RE` 가 잡는다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insert_messages as M  # noqa: E402

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


HEAD = """# #시험채널

> **워크스페이스**: 시험
> **채널 ID**: C0TEST

---

"""
TAIL = """
---

## 참여 기록 (요약)

- 시험
"""


def make_md(td, body):
    p = Path(td) / "시험채널.md"
    p.write_text(HEAD + body + TAIL, encoding="utf-8")
    return p


# 이미 아카이브에 있는 블록 하나. 이것이 있어야 「일부만 걸러짐」이 되어
# 다시 이어 붙이는 경로를 탄다 — 하나도 안 걸리면 원문 그대로 쓰므로 버그가 안 난다.
EXISTING = "**2026-08-20 09:00 · WHK**\n이미 있던 메시지\n"

# 새로 넣을 블록. 본문 두 번째 줄이 **헤더를 흉내 낸 줄**이다 —
# 시각도 `·` 도 없어 진짜 헤더가 아닌데, 전에는 여기서 묶음이 갈렸다.
INCOMING_BODY = "**2026-08-21 10:00 · 최지훈**\n아래 자료 참고 부탁드립니다\n**2026-08-15 회의록**\n항목 셋을 정리했습니다\n"

print("[1/9] 본문의 헤더 흉내 줄에서 묶음이 안 갈린다")
with tempfile.TemporaryDirectory() as td:
    p = make_md(td, "## 2026-08\n\n" + EXISTING)
    before = p.read_text(encoding="utf-8").split("\n")
    M.insert_block(p, "2026-08", EXISTING + "\n" + INCOMING_BODY)
    after = p.read_text(encoding="utf-8").split("\n")

    grew = len(after) - len(before)
    want = len(INCOMING_BODY.rstrip("\n").split("\n")) + 1  # 블록 4줄 + 사이 빈 줄 1
    if grew == want:
        ok(f"늘어난 줄이 넣은 만큼이다 ({grew}줄) — 없던 빈 줄이 안 낀다")
    else:
        bad(f"줄 수가 {grew} 늘었습니다 (기대 {want}) — 빈 줄이 꼈습니다")

    text = p.read_text(encoding="utf-8")
    if "부탁드립니다\n**2026-08-15 회의록**" in text:
        ok("흉내 줄이 앞줄에 그대로 이어 붙어 있다")
    else:
        bad("흉내 줄 앞에 빈 줄이 꼈습니다")

print("[2/9] 진짜 헤더 둘은 여전히 따로 다뤄진다")
with tempfile.TemporaryDirectory() as td:
    p = make_md(td, "## 2026-08\n\n" + EXISTING)
    two = (EXISTING + "\n"
           + "**2026-08-21 10:00 · 최지훈**\n첫째\n\n"
           + "**2026-08-21 11:00 · 박민수**\n둘째\n")
    M.insert_block(p, "2026-08", two)
    text = p.read_text(encoding="utf-8")
    if text.count("**2026-08-21 10:00 · 최지훈**") == 1 and text.count("**2026-08-21 11:00 · 박민수**") == 1:
        ok("둘 다 한 번씩 들어갔다 — 이미 있던 것만 걸러졌다")
    else:
        bad("새 블록 둘이 제대로 안 들어갔습니다")
    if text.count("이미 있던 메시지") == 1:
        ok("이미 있던 블록이 두 벌 안 됐다")
    else:
        bad("이미 있던 블록이 다시 들어갔습니다")

print("[3/9] 사람이 보라고 넣은 중간 절(`## 호텔 운영실적 추이`)에서도 안 갈린다")
with tempfile.TemporaryDirectory() as td:
    p = make_md(td, "## 2026-08\n\n" + EXISTING)
    body = "**2026-08-21 10:00 · 최지훈**\n표를 붙입니다\n## 호텔 운영실적 추이\n7월 92%\n"
    before = p.read_text(encoding="utf-8").split("\n")
    M.insert_block(p, "2026-08", EXISTING + "\n" + body)
    after = p.read_text(encoding="utf-8").split("\n")
    grew = len(after) - len(before)
    want = len(body.rstrip("\n").split("\n")) + 1
    if grew == want:
        ok(f"늘어난 줄이 넣은 만큼이다 ({grew}줄)")
    else:
        bad(f"줄 수가 {grew} 늘었습니다 (기대 {want}) — 빈 줄이 꼈습니다")

# ── 같은 분에 부모 후보가 여럿일 때의 `append_thread` (2026-09-22) ──
#
# 아카이브에는 헤더 줄이 **글자까지 같은** 메시지 쌍이 실제로 있다(2026-09-22 실측
# 57곳·118건, 파일 20개). 그때 `find_parent` 는 고르지 않고 멈추는데, 붙이려는 답글이
# 이미 md 에 다 들어 있어도 **중복 검사보다 먼저** 멈춰서 「이미 반영됨」에 못 갔다.
# 부르는 쪽은 그 실패로 채널 회차를 중단해 `last_ts` 가 얼고, 되돌아보기 창이 그 자리에
# 고정돼 **같은 실패가 매일 되풀이된다** (한 채널이 2026-09-17~09-22 엿새를 그렇게 돌았다).
TWIN = (
    "**2026-08-07 07:37 · 최호중**\n📎 첨부: `공급계획.hwp`\n\n"
    "**2026-08-07 07:37 · 최호중**\nA2블럭 매각일정 관련입니다.\n"
)
REPLY = "> **└ 2026-09-16 08:53 · 정신욱** — A2블럭 매각공고문입니다.\n"

print("[4/9] 부모가 모호해도 붙일 답글이 전부 이미 있으면 멈추지 않는다")
with tempfile.TemporaryDirectory() as td:
    p = make_md(td, "## 2026-08\n\n" + TWIN + REPLY)
    before = p.read_text(encoding="utf-8")
    try:
        msg = M.append_thread(p, "**2026-08-07 07:37 · 최호중**", REPLY)
        if "이미 반영됨" in msg:
            ok(f"정상 종료했다 — {msg}")
        else:
            bad(f"「이미 반영됨」이 아닙니다: {msg}")
    except Exception as e:  # noqa: BLE001
        bad(f"멈췄습니다: {e}")
    if p.read_text(encoding="utf-8") == before:
        ok("md 는 한 글자도 안 바뀌었다 — 추측해서 붙이지 않았다")
    else:
        bad("md 가 바뀌었습니다 — 어느 블록엔가 붙였습니다")

print("[5/9] 하나라도 새것이면 종전대로 멈춘다")
with tempfile.TemporaryDirectory() as td:
    p = make_md(td, "## 2026-08\n\n" + TWIN + REPLY)
    fresh = REPLY + "> **└ 2026-09-17 09:00 · 정신욱** — 공고문 정정본입니다.\n"
    try:
        M.append_thread(p, "**2026-08-07 07:37 · 최호중**", fresh)
        bad("새 답글이 있는데 그냥 지나갔습니다 — 엉뚱한 블록에 붙을 수 있습니다")
    except M.AmbiguousParent:
        ok("AmbiguousParent 로 멈췄다 — 추측해서 붙이지 않는다")
    except Exception as e:  # noqa: BLE001
        bad(f"다른 예외가 났습니다: {e!r}")

print("[6/9] 다른 블록의 「같은 시각·같은 이름」 무관 답글을 「이미 있음」으로 안 먹는다")
with tempfile.TemporaryDirectory() as td:
    # 첫 블록에 같은 분·같은 이름이되 **본문이 다른** 답글이 이미 있다. 한 블록 안이라면
    # 표기 흔들림으로 보고 짝지어 줘야 맞지만, 블록 여럿에 걸치면 그건 남의 스레드다.
    other = (
        "**2026-08-07 07:37 · 최호중**\n📎 첨부: `공급계획.hwp`\n"
        "> **└ 2026-09-16 08:53 · 정신욱** — 다른 스레드에 단 말입니다.\n\n"
        "**2026-08-07 07:37 · 최호중**\nA2블럭 매각일정 관련입니다.\n"
    )
    p = make_md(td, "## 2026-08\n\n" + other)
    try:
        M.append_thread(p, "**2026-08-07 07:37 · 최호중**", REPLY)
        bad("남의 블록 답글을 짝으로 세어 새 답글을 삼켰습니다")
    except M.AmbiguousParent:
        ok("완전일치가 아니라 멈췄다 — 조용한 유실이 안 난다")
    except Exception as e:  # noqa: BLE001
        bad(f"다른 예외가 났습니다: {e!r}")

print("[7/9] 봇 답변 건수는 못 올리되, 그것 때문에 멈추지는 않는다")
with tempfile.TemporaryDirectory() as td:
    # 봇 답변 건수(`--bot-added`)는 어느 블록의 꼬리말을 고칠지 정해야 쓸 수 있는 값이라
    # 모호한 자리에서는 못 올린다. 그렇다고 멈추면 위 고리가 **봇이 답한 스레드에서만**
    # 그대로 살아난다. 그래서 멈추지 않고 못 올렸다고 말한다.
    p = make_md(td, "## 2026-08\n\n" + TWIN + REPLY)
    before = p.read_text(encoding="utf-8")
    rep = {}
    try:
        msg = M.append_thread(p, "**2026-08-07 07:37 · 최호중**", REPLY, bot_added=1, report=rep)
        if "셈에 못 올렸습니다" in msg:
            ok("정상 종료하면서 못 올렸다고 말한다")
        else:
            bad(f"못 올린 사실이 문구에 없습니다: {msg}")
    except Exception as e:  # noqa: BLE001
        bad(f"멈췄습니다 — 봇이 답한 스레드에서 고리가 그대로 삽니다: {e}")
    if rep.get("botAdded") == 0:
        ok("보고의 botAdded 가 0 이다 — 넘겨받은 값을 그대로 돌려주면 「올렸다」로 세어진다")
    else:
        bad(f"보고의 botAdded 가 {rep.get('botAdded')} 입니다")
    if p.read_text(encoding="utf-8") == before:
        ok("md 는 한 글자도 안 바뀌었다 — 아무 블록의 건수도 안 건드렸다")
    else:
        bad("md 가 바뀌었습니다 — 어느 블록의 스레드 건수를 올렸습니다")

print("[8/9] 사람 답글 0건 · 봇 답변만인 회차 — 이 입력이 실제로 온다")
with tempfile.TemporaryDirectory() as td:
    # `slack-archive.js` 는 `human.length === 0 && botAdded > 0` 을 그대로 넘긴다
    # (Hermes 만 답한 스레드). 그때 `--content-file` 이 아예 안 붙어 `block` 이 빈 문자열이다.
    # 그것을 「붙일 것이 없으니 실패」로 읽으면, 이 고침이 없애려던 고리가 **바로 그
    # 경우에만** 그대로 살아난다 — 겉으로는 고쳐진 것처럼 보이면서.
    p = make_md(td, "## 2026-08\n\n" + TWIN + REPLY)
    before = p.read_text(encoding="utf-8")
    try:
        msg = M.append_thread(p, "**2026-08-07 07:37 · 최호중**", "", bot_added=2)
        if "셈에 못 올렸습니다" in msg:
            ok("빈 본문 + 봇 답변에서도 정상 종료한다")
        else:
            bad(f"문구가 다릅니다: {msg}")
    except Exception as e:  # noqa: BLE001
        bad(f"멈췄습니다 — 봇만 답한 스레드에서 고리가 그대로 삽니다: {e}")
    if p.read_text(encoding="utf-8") == before:
        ok("md 는 한 글자도 안 바뀌었다")
    else:
        bad("md 가 바뀌었습니다")

    # 붙일 것도 올릴 것도 없으면 종전대로 멈춘다 — 「빈 본문이면 다 통과」로 넓히면
    # 모호한 부모에 대해 **아무것도 안 하고 성공했다고 말하는** 자리가 생긴다.
    try:
        M.append_thread(p, "**2026-08-07 07:37 · 최호중**", "", bot_added=0)
        bad("붙일 것도 올릴 것도 없는데 성공했다고 말했습니다")
    except M.AmbiguousParent:
        ok("본문도 봇 건수도 없으면 종전대로 멈춘다")
    except Exception as e:  # noqa: BLE001
        bad(f"다른 예외가 났습니다: {e!r}")

print("[9/9] 그 사실이 `NOTE:` 줄로 밖에 나온다 (부르는 쪽이 보고에 싣는 자리)")
with tempfile.TemporaryDirectory() as td:
    # 문구가 아니라 **`NOTE:` 접두**가 계약이다 — `slack-archive.js` 의 `collectNotes` 가
    # 그 접두만 보고 `result.notes` 에 옮긴다. 접두를 바꾸면 사람에게 가는 길이 끊긴다.
    import subprocess  # noqa: PLC0415

    p = make_md(td, "## 2026-08\n\n" + TWIN + REPLY)
    reply_file = Path(td) / "reply.md"
    reply_file.write_text(REPLY, encoding="utf-8")
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "insert_messages.py"),
         "--file", str(p), "--append-thread", "**2026-08-07 07:37 · 최호중**",
         "--content-file", str(reply_file), "--bot-added", "1", "--report"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if r.returncode == 0:
        ok("종료코드 0")
    else:
        bad(f"종료코드 {r.returncode} — {r.stderr.strip()}")
    if any(l.strip().startswith("NOTE:") for l in r.stdout.splitlines()):
        ok("`NOTE:` 줄이 나온다")
    else:
        bad(f"`NOTE:` 줄이 없습니다: {r.stdout!r}")
    if '"botAdded": 0' in r.stdout:
        ok("REPORT 의 botAdded 가 0 이다")
    else:
        bad(f"REPORT 의 botAdded 가 0 이 아닙니다: {r.stdout!r}")

sys.exit(1 if FAILED else 0)
