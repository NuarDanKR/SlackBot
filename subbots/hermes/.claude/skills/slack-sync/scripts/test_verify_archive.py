#!/usr/bin/env python3
"""
verify_archive.py 시험 — 결함을 일부러 넣어 **검사가 실제로 발동하는지** 본다.
사본에서만 돌고 저장소는 안 건드린다.

  python .claude/skills/slack-sync/scripts/test_verify_archive.py

종료코드: 0 전부 통과 / 1 실패 있음

「오늘 돌렸더니 0건」은 검사가 맞다는 증거가 아니다 — 아무것도 안 보는 검사도 0건을 낸다.
그래서 결함을 넣어 잡히는 것과, 정상인 모양을 넣어 **안 잡히는 것**을 함께 확인한다.
후자가 특히 중요하다: 오탐이 나기 시작하면 그날부터 아무도 안 읽는다.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_archive as V  # noqa: E402

# **이 시험은 부르는 쪽 환경에 기대면 안 된다.**
#
# 아래 `mini_repo` 는 임시 저장소마다 `.env` 를 써 두고 관문을 **하위 프로세스로** 부른다.
# 그 프로세스의 `_shared/paths.py` 는 「환경변수 → .env → 이웃 폴더」 순으로 보므로
# (paths.py 의 `data_root_from`), 부르는 쪽에 `HERMES_DATA_ROOT` 가 있으면 그것이 이겨서
# 관문이 임시 저장소가 아니라 **진짜 아카이브**를 본다. 그러면 `git show :경로` 가 저장소
# 밖 파일을 찾다 rc=2 로 죽고, ⑨⑩ 이 「관문이 인덱스를 안 본다」로 **거짓 실패**한다
# (2026-09-03 실측 재현: 그 env 를 준 채로 돌리면 ⑨⑩ 이 rc=2 로 FAIL, 빼면 전부 통과).
#
# `check-setup.js` 는 자기가 물려주는 env 에서 이 값만 빼서 그 자리를 막았지만
# (커밋 729e472), 막힌 것은 그 경로 하나뿐이라 사람이 셸에서 직접 돌리면 그대로 났다.
# 시험 자신이 환경과 무관해지는 것이 근본이라 여기서 지운다.
#
# **`V` 를 임포트한 뒤에 지운다** — 이 프로세스가 바탕 자료로 읽는 실제 아카이브
# (`V.CHANNELS`)는 임포트 시점에 이미 정해졌고, 지우는 것은 앞으로 낳을 하위 프로세스
# 몫이다. 임포트 전에 지우면 `.env` 가 없는 VM 에서 바탕 아카이브를 못 찾는다.
os.environ.pop("HERMES_DATA_ROOT", None)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True

HERE = Path(__file__).resolve().parent
# 관문이 임포트해 쓰는 것까지. 하나라도 빠지면 임시 저장소에서 ImportError 로 죽는다.
GATE_SCRIPTS = ["verify_archive.py", "sync_index.py", "insert_messages.py"]
# 2026-08-31 부터 위 셋이 `_shared/paths.py` 를 임포트한다. 같은 이유로 함께 복사한다.
# 2026-09-03 부터 `sync_index.py` 가 `_shared/tz.py` 도 임포트한다 — 하나라도 빠지면
# 임시 저장소에서 `ModuleNotFoundError` 로 죽고, ⑨⑩⑪ 이 전부 rc=1 로 거짓 실패한다
# (기대하는 rc 는 0·1·2 로 제각각인데 죽으면 늘 1 이라, 「관문이 인덱스를 안 본다」와
# 구별이 안 된다).
SHARED = [HERE.parents[1] / "_shared" / "paths.py", HERE.parents[1] / "_shared" / "tz.py"]


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def mini_repo(root: Path, name: str, staged: str, worktree: str):
    """**별개 저장소**를 만들어 채널 md 를 담는다. 작업 트리와 인덱스에 다른 내용을 둔다.

    관문이 어느 쪽을 보는지는 두 내용이 갈릴 때만 드러난다. 같은 내용으로 시험하면
    작업 트리를 보는 옛 코드도 똑같이 통과해서, 아무것도 확인하지 못한다.

    임시 워크트리를 쓰지 않는다 — 워크트리는 별개 저장소가 아니라 **같은 저장소**다.
    """
    scripts = root / ".claude" / "skills" / "slack-sync" / "scripts"
    scripts.mkdir(parents=True)
    for f in GATE_SCRIPTS:
        shutil.copy2(HERE / f, scripts / f)
    shared = root / ".claude" / "skills" / "_shared"
    shared.mkdir(parents=True)
    for f in SHARED:
        shutil.copy2(f, shared / f.name)
    # 이 임시 저장소는 코드와 자료를 한 곳에 둔다 — 관문이 보는 것은 「어느 저장소인가」이지
    # 「코드와 자료가 갈렸나」가 아니다. `.env` 로 자료 뿌리를 알려 준다.
    (root / ".env").write_text(f"HERMES_DATA_ROOT={root.as_posix()}\n", encoding="utf-8")
    ch = root / "slack-export" / "channels"
    ch.mkdir(parents=True)
    md = ch / f"{name}.md"

    r = _git(root, "init", "-q")
    if r.returncode != 0:
        return None, f"git init 실패 — {r.stderr.strip()[:80]}"
    md.write_bytes(staged.encode("utf-8"))
    r = _git(root, "add", "--", md.relative_to(root).as_posix())
    if r.returncode != 0:
        return None, f"git add 실패 — {r.stderr.strip()[:80]}"
    if worktree != staged:
        md.write_bytes(worktree.encode("utf-8"))       # 인덱스는 그대로 두고 작업 트리만 바꾼다
    return md, ""


def run_gate(root: Path, md: Path, from_index: bool):
    """(종료코드, 출력). 관문이 부르는 것과 같은 방식으로 **하위 프로세스로** 돌린다."""
    script = root / ".claude" / "skills" / "slack-sync" / "scripts" / "verify_archive.py"
    args = [sys.executable, str(script), str(md)] + (["--from-index"] if from_index else [])
    r = subprocess.run(args, cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if not V.CHANNELS.is_dir():
        print(f"✗ 아카이브가 없습니다: {V.CHANNELS}", file=sys.stderr)
        return 1

    base = V._baseline()
    tmp = Path(tempfile.mkdtemp(prefix="verify-archive-test-"))
    try:
        # 스레드 답글이 여러 건 있는 채널 하나를 고른다
        src = next(
            (p for p in sorted(V.CHANNELS.glob("*.md"))
             if len(V._thread_regions(p.read_bytes().decode("utf-8").split("\n"))) >= 3
             and not V.check(p, base)[0]),
            None,
        )
        if src is None:
            print("✗ 시험에 쓸 깨끗한 채널을 못 찾았습니다", file=sys.stderr)
            return 1
        print(f"바탕 파일: {src.name}")
        orig = src.read_bytes().decode("utf-8")

        def run(label, text, expect, name=None):
            p = tmp / (name or f"{src.stem}.md")
            p.write_bytes(text.encode("utf-8"))
            problems, _notes, _n = V.check(p, base)
            hit = [x for x in problems if expect in x]
            check(label, bool(hit), hit[0][:110] if hit else f"안 잡힘 (문제 {len(problems)}건)")

        def run_clean(label, text, forbid, name=None):
            p = tmp / (name or f"{src.stem}.md")
            p.write_bytes(text.encode("utf-8"))
            problems, _notes, _n = V.check(p, base)
            bad = [x for x in problems if forbid in x]
            check(label, not bad, bad[0][:110] if bad else "")

        lines = orig.split("\n")
        regions = V._thread_regions(lines)

        # ── 바탕은 깨끗해야 한다 ──
        run_clean("바탕 파일은 결함 0건", orig, "")

        # ── ① 답글 한 줄을 지우면 잡힌다 ──
        _h, _said, lo, hi = regions[0]
        reply_i = next(k for k in range(lo, hi) if V.REPLY.match(lines[k]))
        run("① 답글이 사라지면 잡음", "\n".join(lines[:reply_i] + lines[reply_i + 1:]),
            "스레드 머리줄은")

        # ── ② 수집 실패 표식 ──
        run("② 실패 표식을 잡음",
            orig.replace(lines[regions[0][0]],
                         lines[regions[0][0]] + " — 답글이 반환되지 않았습니다 (Slack API 응답 공백)"),
            "수집 실패 표식")

        # ── ③ 헤더 건수 ──
        m = re.search(r"\*\*실제 메시지\*\*:\s*(\d+)건", orig)
        run("③ 헤더 건수가 틀리면 잡음",
            orig.replace(m.group(0), f"**실제 메시지**: {int(m.group(1)) + 1}건", 1),
            "헤더")

        # ── ④ 기간 끝날짜가 최신 메시지보다 오래됨 ──
        pm = V.PERIOD.search(orig)
        if pm and pm.group(2):
            run("④ 기간이 낡으면 잡음",
                orig.replace(pm.group(0), f"**기간**: {pm.group(1)} ~ 2020-01-01", 1),
                "기간 끝날짜")

        # ── ④ 반대 방향 — 끝날짜가 실제보다 **새것** (2026-09-03 추가) ──
        # `sync_index.py` 는 끝날짜를 **뒤로만 민다**. 그래서 이 방향은 고치는 쪽이
        # 아무도 없고, 이 검사가 `end < newest` 만 보던 동안에는 **어떤 관문도 안 봤다.**
        # 그 상태는 「이 채널은 그날까지 자료가 있다」를 봇 프롬프트에 사실로 싣는다.
        end_ahead = (
            "> **채널**: #시험채널끝날짜\n"
            "> **기간**: 2026-06-30 ~ 2026-09-30\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run("④ 끝날짜가 실제보다 새것이면 잡음", end_ahead,
            "없는 자료가 있다고 말합니다", name="시험채널끝날짜.md")

        # 대조: 끝날짜가 실제 최신 메시지와 같으면 조용하다. 양방향으로 만들면서
        # 정상까지 잡기 시작하면 채널마다 매일 빨개져 곧 아무도 안 읽는다.
        run_clean("④ 끝날짜가 맞으면 조용함",
                  end_ahead.replace("2026-06-30 ~ 2026-09-30", "2026-06-30 ~ 2026-06-30"),
                  "", name="시험채널끝날짜.md")

        # ── ⑤ 기간 시작날짜가 가장 오래된 메시지보다 새것이면 잡음 ──
        # Task 7 — 백필이 md 에 더 오래된 메시지를 채워도 헤더는 옛 시작날짜에 굳는다.
        # 브리프의 예시 그대로: 헤더는 `2026-08-11 ~ 2026-08-13`, 본문엔 6/30 메시지.
        # 실제 아카이브에는 이 결함이 없다(2026-09-02 실측 42채널 0건) — 재현해서 만든다.
        start_drift = (
            "> **채널**: #시험채널\n"
            "> **기간**: 2026-08-11 ~ 2026-08-13\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run("⑤ 기간 시작날짜가 최고령 메시지보다 새면 잡음", start_drift,
            "기간 시작날짜", name="시험채널.md")

        # 대조: 시작날짜가 실제 가장 오래된 메시지와 같으면(정상) 조용하다.
        start_ok = start_drift.replace("2026-08-11 ~ 2026-08-13", "2026-06-30 ~ 2026-08-13")
        run_clean("⑤ 시작날짜가 맞으면 조용함", start_ok, "기간 시작날짜", name="시험채널.md")

        # ── ⑤ 핀 시험 — 「개설일형」은 결함이 아니다 (리뷰 Finding 2) ──
        # 헤더 시작이 첫 메시지보다 이른 채널(채널 개설일 등)은 정상이다. 조건 부호
        # 하나(`>` → `!=`)만 지워도 이런 채널을 전부 결함으로 잡아 버리는데, 리뷰가
        # 실제로 그렇게 바꿔 42채널 중 22채널이 걸리는 걸 확인했다 — 그때도 이 파일의
        # 다른 시험은 전부 통과했다. 그 사고가 다시 안 나게, 이 모양이 **조용해야**
        # 한다는 것을 직접 못 박는다.
        opening_ok = (
            "> **채널**: #시험채널개설일\n"
            "> **기간**: 2026-05-01 ~ 2026-08-13\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run_clean("⑤ 핀 — 개설일형(시작이 첫 메시지보다 이름)은 결함 아님",
                  opening_ok, "기간 시작날짜", name="시험채널개설일.md")

        # ── ⑨ 자리표시 기간이 굳은 것 (최종 검토 F3, 2026-09-02) ──
        # `(백필 중)`·`(대화 없음)` 은 날짜가 아니라 `PERIOD` 에 안 걸려서, ④⑤ 가
        # 통째로 건너뛰고 파일이 43/43 통과를 냈다. 그 문자열은 `archive.js` 의
        # metaBlock 을 타고 봇 시스템 프롬프트에 **사실로** 실린다.
        stuck_body = (
            "> **채널**: #시험채널자리표시\n"
            "> **기간**: {ph}\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run("⑨ (백필 중) 이 굳으면 잡음", stuck_body.format(ph="(백필 중)"),
            "기간이 자리표시", name="시험채널자리표시.md")
        run("⑨ (대화 없음) 인데 메시지가 있으면 잡음", stuck_body.format(ph="(대화 없음)"),
            "기간이 자리표시", name="시험채널자리표시.md")

        # ⑨ 핀 — **메시지가 없는 채널의 `(대화 없음)` 은 사실이라 결함이 아니다.**
        # 이 핀이 없으면 「고치기」가 빈 채널마다 매번 우는 거짓 경보가 된다.
        empty_ok = (
            "> **채널**: #시험채널빈것\n"
            "> **기간**: (대화 없음)\n"
            "> **실제 메시지**: 0건\n"
            "---\n"
        )
        run_clean("⑨ 핀 — 메시지 없는 채널의 (대화 없음) 은 결함 아님",
                  empty_ok, "기간이 자리표시", name="시험채널빈것.md")

        # ── ⑥ 기준선에 없는 파일에서 한 줄 메시지가 나오면 잡는다 ──
        hdr_i = next(i for i, l in enumerate(lines) if V.STRICT_HDR.match(l))
        squashed = list(lines)
        squashed[hdr_i] = squashed[hdr_i].rstrip() + " — 한 줄로 줄여 적은 본문"
        run("⑥ 한 줄 메시지를 잡음", "\n".join(squashed), "한 줄로 줄여 적은")

        # 기준선 안이면 조용하다
        p = tmp / "사업장나.md"
        p.write_bytes("\n".join(squashed).encode("utf-8"))
        probs, _, _ = V.check(p, {"사업장나": 99})
        check("⑥ 기준선 안이면 조용함", not any("한 줄로" in x for x in probs))

        # ── ⑦ 스레드 안 인용이 빠지면 잡힌다 ──
        broke = list(lines)
        broke[reply_i] = broke[reply_i].lstrip("> ")
        run("⑦ 인용 `>` 가 빠지면 잡음", "\n".join(broke), "인용 `>` 가 없습니다")

        # ── ⑦ 오탐 없음: 스레드 바로 뒤의 월 헤딩 ──
        #    각 달 마지막 메시지의 스레드 뒤에는 `## YYYY-MM` 이 온다. 이걸 위반으로 세면
        #    채널마다 오탐이 나고, 그때부터 이 검사는 아무도 안 읽는다.
        _h2, _s2, lo2, hi2 = regions[0]
        withmonth = lines[:hi2] + ["", "## 2026-07", ""] + lines[hi2:]
        run_clean("⑦ 스레드 뒤 월 헤딩은 오탐 아님", "\n".join(withmonth), "인용 `>` 가 없습니다")

        # ── ⑩ 순서 계약 (2026-09-03 추가) ────────────────────────────────
        #    봇의 월 단위 읽기(`archive.js` 의 `readChannel`)는 `## YYYY-MM` 줄을 찾아
        #    다음 `## ` 전까지를 잘라 준다. 그러니 자료가 **어느 절에 놓였느냐**가 곧
        #    「그 달에 자료가 있느냐」다. 아래 다섯 모양은 전부 파일에는 자료가 있는데
        #    봇이 **「없다」고 답하는** 자리이고, ①~⑨ 는 하나도 안 봤다.
        ORDER_HEAD = (
            "> **채널**: #시험채널순서\n"
            "> **기간**: {period}\n"
            "> **실제 메시지**: {n}건\n"
            "---\n"
        )
        MSG_A = "**2026-08-02 10:00 · 사람가**\n본문\n"
        MSG_B = "**2026-06-30 15:11 · 사람나**\n본문\n"
        ORD = "시험채널순서.md"

        # ⑩-1 6월 메시지가 `## 2026-08` 절 안에 있다 — `read_channel(month:"2026-06")`
        #      은 「없습니다」로 답한다. 파일에는 그대로 있는데.
        run("⑩ 메시지가 남의 달 절에 있으면 잡음",
            ORDER_HEAD.format(period="2026-06-30 ~ 2026-08-02", n=2)
            + "## 2026-08\n" + MSG_A + MSG_B,
            "절에 있습니다", name=ORD)

        # 대조 — 제 달 절에 놓이면 **결함 0건**이다. (forbid="" 는 「아무 문제도 없다」)
        run_clean("⑩ 대조 — 제 달 절에 있으면 결함 0건",
                  ORDER_HEAD.format(period="2026-06-30 ~ 2026-08-02", n=2)
                  + "## 2026-08\n" + MSG_A + "## 2026-06\n" + MSG_B,
                  "", name=ORD)

        # ⑩-2 월 헤딩이 오름차순 — 새 메시지는 위에 꽂히므로 뒤집힌 구간은 삽입 자리가
        #      틀렸다는 뜻이고, 그대로 두면 다음 삽입이 그 틈에 또 쌓인다.
        run("⑩ 월 헤딩이 오름차순이면 잡음",
            ORDER_HEAD.format(period="2026-06-30 ~ 2026-08-02", n=2)
            + "## 2026-06\n" + MSG_B + "## 2026-08\n" + MSG_A,
            "월 헤딩이 내림차순이 아닙니다", name=ORD)

        # ⑩-3 한 달 안이 오름차순
        run("⑩ 한 달 안이 오름차순이면 잡음",
            ORDER_HEAD.format(period="2026-08-01 ~ 2026-08-02", n=2)
            + "## 2026-08\n"
            + "**2026-08-01 10:00 · 사람가**\n본문\n"
            + "**2026-08-02 11:00 · 사람나**\n본문\n",
            "한 달 안이 내림차순이 아닙니다", name=ORD)

        # ⑩-4 월 헤딩이 아예 없다 — 그 채널은 **어느 달로도 안 열린다.**
        run("⑩ 월 헤딩이 하나도 없으면 잡음",
            ORDER_HEAD.format(period="2026-08-02 ~ 2026-08-02", n=1) + MSG_A,
            "월 헤딩이 하나도 없습니다", name=ORD)

        # ⑩-5 `##` 뒤 공백 두 칸 — **관문 여섯 자리는 다 통과시키고**(느슨한 모양)
        #      봇의 월 단위 읽기만 막는다(엄격한 모양). 그 사이로 빠지는 자리다.
        run("⑩ `##` 뒤 공백이 두 칸이면 잡음",
            ORDER_HEAD.format(period="2026-08-02 ~ 2026-08-02", n=1)
            + "## 2026-08\n" + MSG_A + "##  2026-07\n",
            "공백이 한 칸이 아닙니다", name=ORD)

        # ⑩-6 첫 월 헤딩 앞에 놓인 메시지 — 절이 안 열린 자리라 역시 안 읽힌다.
        run("⑩ 첫 월 헤딩 앞의 메시지를 잡음",
            ORDER_HEAD.format(period="2026-08-01 ~ 2026-08-02", n=2)
            + MSG_A + "## 2026-08\n"
            + "**2026-08-01 10:00 · 사람나**\n본문\n",
            "첫 월 헤딩 앞에 메시지가", name=ORD)

        # ── ⑪ 달력에 없는 날짜 (2026-09-03 추가) ─────────────────────────
        #    `PERIOD`·`HEADER_RE` 는 모양만 본다(`\d{4}-\d{2}-\d{2}`). ④⑤ 는 그 값을
        #    **문자열끼리** 대므로 `2026-13-99` 는 어떤 정상 날짜보다도 크고,
        #    `sync_index.py` 가 그것으로 끝날짜를 덮어쓰면 되돌아오지 않는다.
        BAD = "시험채널날짜.md"
        bad_period = (
            "> **채널**: #시험채널날짜\n"
            "> **기간**: 2026-06-30 ~ 2026-13-99\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run("⑪ 기간에 달력에 없는 날짜가 있으면 잡음", bad_period,
            "달력에 없는 날짜", name=BAD)

        bad_header = (
            "> **채널**: #시험채널날짜\n"
            "> **기간**: 2026-02-30 ~ 2026-02-30\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-02\n"
            "**2026-02-30 15:11 · 사람가**\n"
            "본문\n"
        )
        run("⑪ 메시지 헤더에 달력에 없는 날짜가 있으면 잡음", bad_header,
            "달력에 없는 날짜", name=BAD)

        # 대조 — 달력에 있는 날짜만 있으면 조용하다. 여기서 오탐이 나면
        # 2월 말·윤년 같은 날짜가 든 채널이 통째로 빨개진다.
        run_clean("⑪ 대조 — 달력에 있는 날짜(윤년 2/29)는 조용함",
                  bad_header.replace("2026-02-30", "2024-02-29").replace("## 2026-02", "## 2024-02"),
                  "", name=BAD)

        # ⑪ 핀 — 달력에 없는 날짜가 끼면 ④⑤ 는 **판정을 미뤄야 한다.** 문자열 비교라
        # 그 상태의 「어긋났다/안 어긋났다」는 뜻이 없고, 사람이 날짜를 먼저 고쳐야 한다.
        run_clean("⑪ 핀 — 달력에 없는 날짜가 있으면 ④⑤ 판정을 안 낸다",
                  bad_period, "기간 끝날짜", name=BAD)

        # ── 머리줄 정규식: 꼬리말이 볼드 안에 든 형태를 놓치지 않는다 ──
        narrow = re.compile(r"^>\s*💬\s*\*\*스레드\s*\((\d+)\)\*\*")
        n_wide = n_narrow = 0
        for q in sorted(V.CHANNELS.glob("*.md")):
            ql = q.read_bytes().decode("utf-8").split("\n")
            n_wide += len(V._thread_regions(ql))
            n_narrow += sum(1 for l in ql if narrow.match(l))
        check("머리줄 정규식이 `— 제목` 형태까지 잡음", n_wide > n_narrow,
              f"넓게 {n_wide} · 좁게 {n_narrow}")

        # ── ⑧ check() 는 넘긴 내용으로 판정한다 ────────────────────────────
        #    관문이 인덱스에 담긴 내용을 넘길 수 있어야 한다. 디스크는 깨끗한데 넘긴 것이
        #    깨졌으면 잡혀야 하고, 안 넘기면 그대로 디스크를 본다.
        broken = "\n".join(lines[:reply_i] + lines[reply_i + 1:])
        p7 = tmp / f"{src.stem}.md"
        p7.write_bytes(orig.encode("utf-8"))
        probs7, _, _ = V.check(p7, base, text=broken)
        check("⑧ 넘긴 내용으로 판정한다", any("스레드 머리줄은" in x for x in probs7))
        probs7b, _, _ = V.check(p7, base)
        check("⑧ 안 넘기면 디스크를 본다", not probs7b, f"{len(probs7b)}건")

        # ── ⑨⑩⑪ 관문은 작업 트리가 아니라 인덱스를 본다 ────────────────
        #    **여기가 이 시험의 중심이다.** 2026-08-13 까지 이 관문은 디스크를 읽어서,
        #    깨진 판을 git add 한 뒤 작업 트리에서만 고치면 그대로 통과했다 —
        #    커밋에는 깨진 것이 들어간다. 문서·요약 관문은 같은 이유로 인덱스를 본다.
        #    작업 트리와 인덱스에 **다른 내용**을 두어야 어느 쪽을 보는지 드러난다.
        repo = tmp / "repo-staged-broken"
        repo.mkdir()
        md, err = mini_repo(repo, src.stem, staged=broken, worktree=orig)
        if md is None:
            check("⑨ 임시 저장소를 만들지 못했습니다", False, err)
        else:
            rc, out = run_gate(repo, md, from_index=True)
            check("⑨ 인덱스가 깨졌으면 막는다", rc == 1, f"rc={rc} · {out.strip().splitlines()[0][:70] if out.strip() else ''}")
            # 대조: 작업 트리를 보는 옛 방식은 이 상황을 **통과시킨다** (뚫려 있던 자리)
            rc_old, _ = run_gate(repo, md, from_index=False)
            check("⑨ 대조 — 작업 트리를 보면 뚫린다", rc_old == 0, f"rc={rc_old}")

        repo2 = tmp / "repo-worktree-broken"
        repo2.mkdir()
        md2, err2 = mini_repo(repo2, src.stem, staged=orig, worktree=broken)
        if md2 is None:
            check("⑩ 임시 저장소를 만들지 못했습니다", False, err2)
        else:
            # 담은 것이 멀쩡하면 조용해야 한다. 안 그러면 고치다 만 md 를 옆에 둔 채로는
            # 아무 커밋도 못 하게 되고, 그때부터 훅을 끈다.
            rc, out = run_gate(repo2, md2, from_index=True)
            check("⑩ 작업 트리만 깨진 것은 안 막는다", rc == 0, f"rc={rc}")
            rc_old, _ = run_gate(repo2, md2, from_index=False)
            check("⑩ 대조 — 작업 트리를 보면 헛막는다", rc_old == 1, f"rc={rc_old}")

        repo3 = tmp / "repo-not-staged"
        repo3.mkdir()
        md3, err3 = mini_repo(repo3, src.stem, staged=orig, worktree=orig)
        if md3 is None:
            check("⑪ 임시 저장소를 만들지 못했습니다", False, err3)
        else:
            _git(repo3, "rm", "--cached", "-q", "--", md3.relative_to(repo3).as_posix())
            rc, out = run_gate(repo3, md3, from_index=True)
            # 「못 읽음」은 0 도 1 도 아니다 — 0 이면 안 본 것을 이상 없음으로 셈하고,
            # 1 이면 사람이 md 를 고치러 간다(고칠 것이 없다).
            check("⑪ 인덱스에 없으면 종료코드 2", rc == 2, f"rc={rc}")
            check("⑪ 사유를 적는다", "읽지 못했습니다" in out, out.strip()[:70])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n결과:", "전부 통과" if _ok else "실패 있음")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
