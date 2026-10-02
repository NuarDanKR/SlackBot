#!/usr/bin/env python3
"""
decide_work.py 시험. **임시 파일에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/archive-inbox/scripts/test_decide_work.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

이 스크립트가 지키는 것은 **결정(「빼·나중에·반영」)과 도장(커밋을 막는 관문이 실제로
보는 값)의 경계**다. 여기가 느슨해지면 아무 소리 없이 승인 없는 반영이 통과한다 — 에러가
아니라 「통과」로 드러나는 종류라 사람이 알아챌 자리가 없다. 특히 `--only` 를 뺐을 때
목록만 찍고 종료코드 0 으로 끝나면 미룬 줄 알고 넘어가게 되는데, 수정·삭제 쪽에서
2026-08-10 에 실제로 그렇게 돼 있었다. 도장은 `approved` 기록이 아니라 `--show` 가
화면에 낸 내용이 만든다는 것도 이 시험이 지킨다(2026-08-15) — 아래 `--show` 절 참조.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decide_work as D  # noqa: E402
import review_work as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True
# 건너뛴 항목 수. **마지막 줄에 함께 낸다** (2026-09-01).
# 이 시험을 부르는 쪽(계획서의 회귀 명령)이 `python … >/dev/null 2>&1` 로 종료코드만 보므로,
# 화면에만 적은 「건너뜀」은 통째로 버려진다. 그러면 아카이브를 못 찾은 날에도 화면은
# 똑같이 「17/17 통과」라고 말한다 — 그 숫자가 「기능 하락 없음」의 증명으로 쓰이는데,
# 무엇을 안 쟀는지가 거기 안 실린다.
_skipped = 0


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def skip(why):
    global _skipped
    _skipped += 1
    print(f"  - 건너뜀: {why}")


# 실제로 있는 채널을 쓴다 — 요약 해시는 JS 쪽(pending-work.js)이 진짜 md 를 읽어 낸다.
#
# **이름을 여기 박지 않는다.** 코드 저장소는 팀끼리 나눠 쓰므로 사업장·채널 이름이
# 들어가면 안 되고(2026-09-01), 박아 두면 그 채널이 없는 팀에서 이 시험이 「해시가 안
# 적힌다」로 빨개진다 — 원인이 자기네 아카이브에 그 채널이 없다는 것뿐인데 그 사실이
# 화면에 안 나온다. 그래서 아카이브에서 하나 골라 쓴다. 어느 것이든 상관없다.
_MDS = sorted(p.stem for p in R.CHANNELS.glob("*.md")) if R.CHANNELS.exists() else []
# 아카이브가 빈 새 팀에는 md 가 없다. 그때도 나머지는 다 돌리고, 진짜 md 를 읽어야
# 하는 「요약 해시」 한 줄만 건너뛴다 — 못 잰 것을 통과로 내지 않으려고 따로 알린다.
HAVE_MD = bool(_MDS)
REAL_CHANNEL = _MDS[0] if HAVE_MD else "빈아카이브"
ITEM_ID = f"{REAL_CHANNEL}|summary|수치|핵심 쟁점"

ITEMS = [
    {
        "id": ITEM_ID, "kind": "summary", "channel": REAL_CHANNEL, "file": REAL_CHANNEL,
        "type": "수치", "where": "핵심 쟁점", "was": "A", "now": "B",
        "evidence": '"조기상환 완료"', "firstSeen": "2026-08-10",
    },
    {"id": "영업|new-channel", "kind": "new-channel", "channel": "영업", "firstSeen": "2026-08-11"},
]


def run(argv):
    """decide_work 를 한 번 돌린다 (종료코드만)."""
    try:
        return D.main(argv)
    except SystemExit as e:          # argparse 가 인자 오류로 죽는 경우
        return int(e.code or 0)


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    work = tmp / ".pending-work.json"
    state = tmp / ".sync-state.json"
    stamp = tmp / ".decision-stamp.json"

    work.write_text(json.dumps({"items": ITEMS}, ensure_ascii=False), encoding="utf-8")
    state.write_text("{}", encoding="utf-8")

    # 경로만 임시로 갈아끼운다. 저장소의 아카이브는 읽기만 한다(요약 해시).
    R.PENDING_WORK = work
    R.STATE = state
    # **`R.STAMP` 도 갈아끼운다.** `main()` 은 매번 `R.load_items()` 를 부르고 그쪽은
    # 승인 기록으로 항목을 거른다 — 안 갈아끼우면 시험이 이 PC 의 진짜 기록을 읽는다.
    R.STAMP = stamp
    D.STATE = state
    D.STAMP = stamp
    D.changed_channel_mds = lambda: []      # 작업 트리는 안 본다

    def read(p):
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    print("\n승인의 경계")

    # ① --only 를 빼면 거절한다. 목록만 찍고 0 으로 끝나면 미룬 줄 알고 넘어간다.
    check("--only 없는 --apply 는 거절", run(["--apply"]) != 0)

    # ② 못 맞춘 토큰을 조용히 넘기지 않는다 — 오타 하나로 안 정해진 건이 생긴다
    check("못 맞춘 토큰은 거절", run(["--only", "없는것", "--apply"]) != 0)

    # ③ 사유 없는 「빼」는 거절 — 왜 뺐는지가 없으면 나중에 되짚을 수 없다
    check("--drop 에 사유가 없으면 거절", run(["--only", "1", "--drop"]) != 0)
    check("  그때 상태 파일은 안 바뀐다", not (read(state).get("dismissed")))

    print("\n기록")

    # ④ 「나중에」는 만기를 적는다. 만기 없는 보류는 삭제와 같다.
    check("--later 는 만기를 적는다", run(["--only", "2", "--later", "14", "--reason", "판정 보류"]) == 0)
    until = (read(state).get("deferred") or {}).get("영업|new-channel", {}).get("until")
    want = str(R.today_kst() + timedelta(days=14))
    check("  만기가 오늘+14", until == want, f"{until} vs {want}")

    # ⑤ 「빼」는 그때의 요약 해시를 함께 적는다 — 요약이 고쳐지면 후보로 돌아와야 한다
    check("--drop 은 기록된다", run(["--only", "1", "--drop", "--reason", "원문과 다름"]) == 0)
    dis = (read(state).get("dismissed") or {}).get(ITEM_ID, {})
    if HAVE_MD:
        check("  요약 해시가 함께 적힌다", len(dis.get("summaryHash", "")) == 16, dis.get("summaryHash"))
    else:
        skip("채널 md 가 하나도 없어 요약 해시를 잴 수 없습니다")
    check("  사유가 적힌다", dis.get("reason") == "원문과 다름")

    # ⑥ 「반영」은 **저장소에 들어가는 상태 파일**에 남는다 — 「빼·나중에」와 같은 자리다.
    #
    #    전에는 로컬 전용 `.decision-stamp.json` 에만 적혔다(.gitignore). 그래도 굴러간
    #    이유는 `mergeItems` 의 자동 치움이 그 자리를 메웠기 때문인데, 그 전제가 틀린
    #    것이어서 2026-09-07 에 걷어냈다 — 그러면 반영한 항목이 VM 목록에서 영영 안
    #    사라진다. 도장(`--show` 가 찍는 `files`)은 그대로 로컬이다.
    work.write_text(json.dumps({"items": ITEMS}, ensure_ascii=False), encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    stamp.write_text("{}", encoding="utf-8")
    check("--apply 는 승인으로 기록된다", run(["--only", "1", "--apply"]) == 0)
    applied = read(state).get("applied", {})
    check("  저장소에 들어가는 상태 파일에 적힌다", ITEM_ID in applied, list(applied))
    check("  정한 시각이 함께 적힌다", bool(applied.get(ITEM_ID, {}).get("at")),
          applied.get(ITEM_ID, {}).get("at"))
    check("  로컬 도장 파일에는 안 쓴다", not read(stamp).get("approved"))

    # ⑦ **요약이 아닌 항목에 「반영」은 거절한다.**
    #
    # 「반영」은 「요약 문장을 고쳤다」는 뜻이다. 요약이 아닌 것(새 채널·개명·note·파생값)에는
    # 고칠 요약 줄이 없어서 그 말이 성립하지 않는다 — 이 종류의 종결 수단은 「빼」다.
    # `DISMISSED_COMMENT` 가 「해시가 없는 것(요약이 아닌 항목)은 돌아올 근거가 없어 계속
    # 빠진다」고 이미 정의하고 있다.
    #
    # (2026-09-07 전에는 이 거절의 이유가 달랐다 — 그때는 「반영」이 로컬 기록에만 남아
    # 이 PC 화면은 0건인데 07:00 DM 은 N건이 되는 갈림이 났고, 재검출로 사라질 길이 없는
    # 이 종류에서 그 갈림이 영구가 됐다. 2026-09-04 에 실물 2건이 그 상태였다. 지금은
    # 「반영」도 저장소에 들어가므로 갈리지는 않지만, 뜻이 안 맞는 것은 그대로다.)
    work.write_text(json.dumps({"items": ITEMS}, ensure_ascii=False), encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    stamp.write_text("{}", encoding="utf-8")
    check("요약이 아닌 항목에 --apply 는 거절", run(["--only", "2", "--apply"]) != 0)
    check("  그때 승인 기록도 안 남는다", not read(state).get("applied"))
    # 섞여 들어와도 **아무것도 하지 않는다** — 일부만 반영하면 무엇이 남았는지가 사라진다
    check("요약+요약아닌 것이 섞이면 통째로 거절", run(["--only", "1,2", "--apply"]) != 0)
    check("  그때도 승인 기록이 안 남는다", not read(state).get("applied"))
    # 요약 항목만 고르면 여전히 통과한다 (위 ⑥ 이 지키는 길을 막지 않았나)
    check("요약 항목만이면 그대로 통과", run(["--only", "1", "--apply"]) == 0)

    print("\n반영을 되돌리는 길")

    # ⑧ **「반영」한 항목은 목록에서 사라진다.** 그래서 `--clear` 가 승인까지 취소하지
    #    못하면 되돌릴 길이 아예 없다 — 「빼」로 바꾸려 해도 목록이 비어 고를 수가 없다.
    #    2026-09-04 에 실물 2건이 그 상태였고, 손으로 `.decision-stamp.json` 을 고치는
    #    것 말고는 나갈 문이 없었다.
    # `generated` 가 있어야 「반영 시각 > 그 항목을 마지막으로 본 시각」 판정이 선다
    # (`review_work.py` 의 `confirmed_at`). 없으면 감추지 않는 쪽으로 물러선다.
    work.write_text(json.dumps({"generated": "2026-08-11T00:00:00.000Z", "items": ITEMS},
                               ensure_ascii=False), encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    # 도장 칸을 **미리 채워 둔다** — 취소가 그걸 지우는지는 빈 도장으로는 못 잰다.
    # 전에는 stamp 를 `{}` 로 두고 「"files" 가 없거나 None 이 아니면 통과」를 쟀는데,
    # 그 조건은 빈 stamp 에서 항상 참이라 clear() 가 도장을 지워도 통과했다 (2026-09-04 리뷰).
    _FILES = {"50-resources/slack-export/channels/시험.md": "도장해시"}
    _SHOWN = "2026-08-11T09:00:00+09:00"
    stamp.write_text(json.dumps({"files": _FILES, "shown_at": _SHOWN},
                                ensure_ascii=False), encoding="utf-8")
    check("반영하면 목록에서 사라진다",
          run(["--only", "1", "--apply"]) == 0 and not any(it["id"] == ITEM_ID for it in R.load_items()))
    check("--clear 가 반영도 취소한다", run(["--clear", ITEM_ID]) == 0)
    check("  승인 기록에서 빠진다", ITEM_ID not in (read(state).get("applied") or {}))
    check("  목록에 다시 나온다", any(it["id"] == ITEM_ID for it in R.load_items()))
    # 도장(`--show` 가 찍는 값)은 승인과 별개다 — 취소가 그걸 지우면 관문이 헐거워진다
    check("  도장 칸은 안 건드린다",
          read(stamp).get("files") == _FILES and read(stamp).get("shown_at") == _SHOWN,
          read(stamp))

    # **`--clear` 는 번호를 안 받는다.** id 에는 16자리 16진수 해시가 들어 있어
    # (예: `…|note|9f6b91b7a57e25c5`) 숫자 한 자리는 무관한 기록에 부분 일치로 걸린다 —
    # 화면 번호인 줄 알고 4 를 넘기면 남의 결정이 조용히 취소되고 0 으로 끝난다.
    state.write_text(json.dumps({"dismissed": {"영업점4|new-channel": {
        "reason": "r", "at": "2026-08-11T09:00:00+09:00", "summaryHash": None}}},
        ensure_ascii=False), encoding="utf-8")
    _rc = run(["--clear", "4"])
    check("--clear 에 숫자 토큰은 거절", _rc != 0, f"rc={_rc}")
    check("  그때 아무것도 안 지운다", "영업점4|new-channel" in (read(state).get("dismissed") or {}))

    # 못 맞춘 토큰이 섞이면 통째로 그만둔다 — select() 와 같은 전부-아니면-전무
    state.write_text(json.dumps({"dismissed": {"영업점4|new-channel": {
        "reason": "r", "at": "2026-08-11T09:00:00+09:00", "summaryHash": None}}},
        ensure_ascii=False), encoding="utf-8")
    _rc = run(["--clear", "영업점,없는조각"])
    check("--clear 에 못 맞춘 토큰이 섞이면 통째로 거절", _rc != 0, f"rc={_rc}")
    check("  맞춘 쪽도 안 지운다", "영업점4|new-channel" in (read(state).get("dismissed") or {}))

    print("\n목록을 못 읽을 때")

    # ⑨ 목록 파일이 깨져 있으면 **트레이스백이 아니라 사유 한 줄**로 멈춘다.
    #    `review_work.py` 는 처음부터 그렇게 했는데 여기만 트레이스백이었다 — 같은 원인에
    #    두 얼굴이 나오면 사람은 자기가 뭘 잘못 쳤나부터 찾는다 (2026-08-13 리뷰).
    #    「없다(0건)」와 구별되어야 해서 종료코드도 1 이 아니라 2 다.
    work.write_text("<<<<<<< HEAD\n{\"items\": []}\n", encoding="utf-8")   # git pull 충돌 표식
    try:
        rc = run(["--only", "1", "--apply"])
    except R.PendingWorkUnreadable:
        rc = "트레이스백"
    check("깨진 목록은 종료코드 2 로 멈춘다", rc == 2, f"rc={rc}")

    # 빈 목록(0건)은 여전히 1 이다 — 못 읽은 것과 같은 값이면 구별이 사라진다
    work.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")
    check("  빈 목록은 1 이다 (2 와 구별된다)", run(["--only", "1", "--apply"]) == 1)

print("\nbot_visible_lines — 봇에게 가는 줄과 안 가는 줄을 가른다")

with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "가짜채널.md"
    # 메타 블록 안의 `>` 줄과, 메타 밖(메시지 본문)의 `>` 줄을 함께 둔다.
    # **이 둘이 서로 다른 판정을 받아야 한다** — 같은 판정을 받으면 아무것도 안 재는 시험이다.
    _p.write_text("> **핵심 쟁점**: 메타 안의 줄\n\n---\n\n## 2026-08\n\n"
                  "**2026-08-14 09:50 · WHK**\n> 스레드 인용 — 메타 밖의 꺾쇠 줄\n",
                  encoding="utf-8")
    _vis = R.bot_visible_lines(_p)
    check("메타 블록 안의 줄은 봇에게 간다",
          "**핵심 쟁점**: 메타 안의 줄" in _vis, sorted(_vis))
    check("메타 밖의 `>` 줄은 봇에게 안 간다",
          not any("스레드 인용" in s for s in _vis), sorted(_vis))
    check("빈 문자열은 안 들어간다", "" not in _vis)

    # node 가 없으면 조용히 넘어가지 않고 멈춘다
    import shutil as _sh
    _orig = _sh.which
    try:
        _sh.which = lambda n: None
        try:
            R.bot_visible_lines(_p); _raised = False
        except RuntimeError:
            _raised = True
    finally:
        _sh.which = _orig
    check("node 가 없으면 조용히 넘어가지 않고 멈춘다", _raised)

check("norm_summary_line 은 `>` 와 뒤 공백을 벗긴다",
      R.norm_summary_line("> **핵심 쟁점**: 값") == "**핵심 쟁점**: 값")
check("  `>` 가 없으면 그대로 다듬기만 한다",
      R.norm_summary_line("  | 표 |  ") == "| 표 |")

print("\nsummary_changed_vs_head — 인덱스가 아니라 HEAD 와 견준다")

with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    _g = lambda *a: subprocess.run(["git", *a], cwd=_repo, capture_output=True, text=True)
    _g("init", "-q"); _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
    _ch = _repo / "50-resources" / "slack-export" / "channels"
    _ch.mkdir(parents=True)
    _md = _ch / "테스트.md"
    _md.write_text("> **핵심 쟁점**: 처음\n\n## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n",
                   encoding="utf-8")
    _g("add", "-A"); _g("commit", "-q", "-m", "first")

    _keep = R.ROOT
    try:
        R.ROOT = _repo
        check("안 고쳤으면 False", not R.summary_changed_vs_head(_md))
        check("HEAD 판을 읽을 수 있다", "처음" in R.head_text(_md))

        # 스테이징하지 않고 요약만 고친다 — --cached 는 못 보고 HEAD 는 본다
        _md.write_text("> **핵심 쟁점**: 고침\n\n## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n",
                       encoding="utf-8")
        check("스테이징 전에도 요약 변경을 본다", R.summary_changed_vs_head(_md))

        # 메시지 블록만 고치면 요약 변경이 아니다
        _md.write_text("> **핵심 쟁점**: 처음\n\n## 2026-08\n\n**2026-08-01 09:00 · 김**\n다른 본문\n",
                       encoding="utf-8")
        check("메시지 블록만 바뀐 것은 요약 변경이 아니다", not R.summary_changed_vs_head(_md))
    finally:
        R.ROOT = _keep

print("\n--show — 도장은 「보여준 내용」이 만든다")

with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    _g = lambda *a: subprocess.run(["git", *a], cwd=_repo, capture_output=True, text=True)
    _g("init", "-q"); _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
    _ch = _repo / "50-resources" / "slack-export" / "channels"
    _ch.mkdir(parents=True)
    _md = _ch / "테스트.md"
    _rel = "50-resources/slack-export/channels/테스트.md"
    _md.write_text("> **핵심 쟁점**: 처음\n\n---\n\n## 표\n\n| 월 | 값 |\n|---|---|\n| 7월 | 1 |\n\n"
                   "## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n", encoding="utf-8")
    _g("add", "-A"); _g("commit", "-q", "-m", "first")

    _st = _repo / "stamp.json"
    _keepR, _keepD = R.ROOT, D.ROOT
    _keepS, _keepC = R.STAMP, R.CHANNELS
    try:
        R.ROOT = D.ROOT = _repo
        R.STAMP = D.STAMP = _st
        R.CHANNELS = _ch
        D.changed_channel_mds = lambda: [_md]

        # ① 승인 기록이 있어도 --show 없이는 안 찍힌다 ← 이번 구멍
        _st.write_text(json.dumps({"approved": {"테스트|summary|핵심 쟁점": {
            "file": _rel, "channel": "테스트", "at": "2026-08-14T09:00:00+09:00"}}},
            ensure_ascii=False), encoding="utf-8")
        _md.write_text("> **핵심 쟁점**: 승인 없이 고침\n\n---\n\n## 표\n\n| 월 | 값 |\n|---|---|\n"
                       "| 7월 | 1 |\n\n## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n",
                       encoding="utf-8")
        check("--show 를 안 돌리면 도장이 안 생긴다",
              not (read(_st).get("files") or {}).get(_rel), read(_st).get("files"))
        check("--stamp 는 이제 없다", run(["--stamp"]) != 0)

        # ② --show 는 지금 내용에 찍는다 — **그리고 화면에 전·후가 실제로 나온다.**
        #    이 고침의 가치 전부가 「바뀐 요약이 화면에 출력됐다」인데, 전에는 종료코드와
        #    도장만 봐서 화면에 아무것도 안 찍혀도 시험이 몰랐다 (2026-08-15 리뷰).
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc_show = run(["--show"])
        _out = _buf.getvalue()
        check("--show 는 0 으로 끝난다", _rc_show == 0)
        check("화면에 「전」이 나온다", "-> **핵심 쟁점**: 처음" in _out, _out[:200])
        check("화면에 「후」가 나온다", "+> **핵심 쟁점**: 승인 없이 고침" in _out, _out[:200])
        _h1 = (read(_st).get("files") or {}).get(_rel)
        check("찍힌 해시가 지금 내용의 것이다", _h1 == R.fingerprint(_md), _h1)

        # ④ approved 를 안 지운다 — 목록 필터가 계속 써야 한다
        check("approved 가 그대로 있다", len(read(_st).get("approved") or {}) == 1)

        # ③ 또 고치면 해시가 갈리고, 다시 --show 하면 다시 찍힌다 (막다른 길 없음)
        _md.write_text("> **핵심 쟁점**: 오타 고침\n\n---\n\n## 표\n\n| 월 | 값 |\n|---|---|\n"
                       "| 7월 | 1 |\n\n## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n",
                       encoding="utf-8")
        check("고친 뒤에는 도장이 안 맞는다", _h1 != R.fingerprint(_md))
        check("다시 --show 하면 통과한다", run(["--show"]) == 0)
        check("도장이 새 내용으로 갱신된다",
              (read(_st).get("files") or {}).get(_rel) == R.fingerprint(_md))

        # ⑤ **승인 기록이 남아 있어도**, 요약은 그대로고 메시지 블록만 바뀌면 안 찍는다.
        #    옛 로직(승인 파일 목록에 있으면 무조건 찍는다)은 여기서 **찍는다** — 그래서
        #    한 번 승인된 채널은 그 뒤 아무 승인 없이 요약을 고쳐도 통과했다 (2026-08-15,
        #    43개 중 10개가 그 상태였다). 이 입력이 옛 로직과 새 로직을 가른다.
        _g("add", "-A"); _g("commit", "-q", "-m", "second")
        _md.write_text(_md.read_text(encoding="utf-8") + "\n**2026-08-02 09:00 · 박**\n추가\n",
                       encoding="utf-8")
        _st.write_text(json.dumps({"approved": {"테스트|summary|핵심 쟁점": {
            "file": _rel, "channel": "테스트", "at": "2026-08-14T09:00:00+09:00"}}},
            ensure_ascii=False), encoding="utf-8")
        run(["--show"])
        check("승인 기록이 있어도, 메시지 블록만 바뀐 파일은 안 찍는다",
              not (read(_st).get("files") or {}).get(_rel), read(_st).get("files"))

        # ⑥ 반대로 **승인 기록이 하나도 없어도**, 요약이 실제로 바뀌면 찍는다. 도장은
        #    이제 「보여준 내용」이 만들지 승인 여부가 만들지 않는다. 옛 로직은 여기서
        #    **못 찍는다**(approved_files 에 이 파일이 없으므로) — 이 입력도 가른다.
        _g("add", "-A"); _g("commit", "-q", "-m", "third")
        _st.write_text(json.dumps({"approved": {}}, ensure_ascii=False), encoding="utf-8")
        _md.write_text(_md.read_text(encoding="utf-8").replace(
            "> **핵심 쟁점**: 오타 고침", "> **핵심 쟁점**: 승인 없이 또 고침"),
            encoding="utf-8")
        check("바뀐 요약이 이번 대상이다 (다음 검사의 전제)", R.summary_changed_vs_head(_md))
        run(["--show"])
        check("승인 기록이 없어도, 요약이 바뀌면 찍는다",
              (read(_st).get("files") or {}).get(_rel) == R.fingerprint(_md),
              read(_st).get("files"))
    finally:
        R.ROOT, D.ROOT = _keepR, _keepD
        R.STAMP = D.STAMP = _keepS
        R.CHANNELS = _keepC

print("\ncarry_dismissals — 「빼」가 남의 요약 편집에 되살아나지 않는다")

# 여기서 재는 것은 **경계**다: 무엇을 새 판으로 옮기고 무엇을 안 옮기나.
# 해시 계산 자체는 위 ⑤ 가 진짜 md 로 이미 쟀으므로, 여기서는 갈아끼워 격리한다 —
# 안 그러면 이 시험이 실물 아카이브 상태에 흔들린다.
with tempfile.TemporaryDirectory() as _d:
    _tmp = Path(_d)
    _work, _state = _tmp / ".pending-work.json", _tmp / ".sync-state.json"
    _GEN = "2026-09-03T07:00:00+09:00"
    _BEFORE = "2026-09-01T10:00:00+09:00"     # 목록보다 앞 = 예전 회차의 결정
    _AFTER = "2026-09-03T09:00:00+09:00"      # 목록보다 뒤 = 오늘 보고 정한 것

    # 목록에 떠 있는 것 둘. 개명된 채널은 **목록에 없다** — 그런 채널의 짝을 어디서
    # 얻는지가 이 시험의 요점이라, 목록에 넣으면 아무것도 안 재는 시험이 된다.
    _work.write_text(json.dumps({"generated": _GEN, "items": [
        {"id": "나온것|summary|수치|핵심 쟁점", "kind": "summary",
         "channel": "나온것", "file": "나온것"},
        {"id": "오늘뺀것|summary|수치|핵심 쟁점", "kind": "summary",
         "channel": "오늘뺀것", "file": "오늘뺀것"},
    ]}, ensure_ascii=False), encoding="utf-8")
    _state.write_text(json.dumps({
        # 개명 지도의 원본 — 자동 반영이 채널마다 적어 두는 자리
        "channels": {"C0TEST": {"name": "옛이름", "file": "새파일"}},
        "dismissed": {
        # ① 목록에 안 뜬 「빼」 — 오늘 편집의 대상이 아니었다 → 옮긴다
        "안나온것|summary|수치|핵심 쟁점": {"reason": "후보가 틀림", "at": _BEFORE,
                                            "summaryHash": "옛해시"},
        # ② 목록에 뜬 「빼」 + 예전 결정 — 이미 되돌아온 질문이다 → 안 옮긴다
        "나온것|summary|수치|핵심 쟁점": {"reason": "후보가 틀림", "at": _BEFORE,
                                          "summaryHash": "옛해시"},
        # ③ 목록에 떴지만 **오늘** 다시 뺀 것 → 옮긴다
        "오늘뺀것|summary|수치|핵심 쟁점": {"reason": "오늘 다시 뺌", "at": _AFTER,
                                            "summaryHash": "옛해시"},
        # ④ 해시가 없는 것(요약 아닌 항목) — 애초에 안 돌아온다 → 손대지 않는다
        "해시없음|new-channel": {"reason": "안 다룸", "at": _BEFORE, "summaryHash": None},
        # ⑤ 이번에 안 보인 채널 → 손대지 않는다
        "안보인채널|summary|수치|핵심 쟁점": {"reason": "후보가 틀림", "at": _BEFORE,
                                              "summaryHash": "옛해시"},
        # ⑥ 개명된 채널 — id 는 옛 채널명, md 는 새 파일명이다
        "옛이름|summary|수치|핵심 쟁점": {"reason": "후보가 틀림", "at": _BEFORE,
                                          "summaryHash": "옛해시"},
    }}, ensure_ascii=False), encoding="utf-8")

    _keepPW, _keepRS, _keepDS, _keepSH = R.PENDING_WORK, R.STATE, D.STATE, R.summary_hash
    try:
        R.PENDING_WORK, R.STATE, D.STATE = _work, _state, _state
        R.summary_hash = lambda name: f"새해시:{name}"
        _shown = {"안나온것", "나온것", "오늘뺀것", "해시없음", "새파일"}
        _carried = {did for did, _rec, _h in D.carry_dismissals(_shown)}

        check("목록에 안 뜬 「빼」는 새 판으로 옮긴다",
              "안나온것|summary|수치|핵심 쟁점" in _carried, sorted(_carried))
        check("목록에 떠 있는 「빼」는 안 옮긴다 (아직 답 안 한 질문)",
              "나온것|summary|수치|핵심 쟁점" not in _carried, sorted(_carried))
        check("오늘 다시 뺀 것은 옮긴다",
              "오늘뺀것|summary|수치|핵심 쟁점" in _carried, sorted(_carried))
        check("해시가 없는 것은 손대지 않는다",
              "해시없음|new-channel" not in _carried, sorted(_carried))
        check("이번에 안 보인 채널은 손대지 않는다",
              "안보인채널|summary|수치|핵심 쟁점" not in _carried, sorted(_carried))
        check("개명된 채널도 파일명으로 찾는다",
              "옛이름|summary|수치|핵심 쟁점" in _carried, sorted(_carried))

        # 해시가 이미 같으면 옮길 것이 없다 — 안 바뀐 요약에 도장만 다시 찍지 않는다
        R.summary_hash = lambda name: "옛해시"
        check("요약이 안 바뀌었으면 옮길 것이 없다", D.carry_dismissals(_shown) == [])

        # **목록을 못 읽으면 아무것도 안 옮긴다** — 「목록에 없다」를 못 세는 상태다
        R.summary_hash = lambda name: f"새해시:{name}"
        _work.write_text("<<<<<<< HEAD\n{}\n", encoding="utf-8")
        check("목록을 못 읽으면 아무것도 안 옮긴다", D.carry_dismissals(_shown) == [])
    finally:
        R.PENDING_WORK, R.STATE, D.STATE = _keepPW, _keepRS, _keepDS
        R.summary_hash = _keepSH

print("\n깨진 상태 파일은 덮어쓰지 않는다")

# `.sync-state.json` 에는 채널별 동기화 기준점(`last_ts`)이 들어 있다. 깨진 파일을
# `{}` 로 읽고 그 위에 결정만 얹어 다시 쓰면 **한 번의 읽기 실패로 전 채널의 기준점이
# 사라진다** — slack-sync 의 `apply_edits.load_state` 가 2026-08-10 에 같은 사고를 겪고
# fail-closed 로 바꾼 자리다 (2026-09-04 리뷰에서 이 파일에 같은 패턴이 다시 있는 것을 확인).
# 깨짐의 대표 사례가 pull 충돌 표식이라 그 모양 그대로 만든다.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    _work = tmp / ".pending-work.json"
    _state = tmp / ".sync-state.json"
    _work.write_text(json.dumps({"items": [
        {"id": "영업|new-channel", "kind": "new-channel", "channel": "영업", "firstSeen": "2026-08-11"},
    ]}, ensure_ascii=False), encoding="utf-8")
    _corrupt = "<<<<<<< HEAD\n{\"channels\": {}}\n=======\n"
    _state.write_text(_corrupt, encoding="utf-8")

    _keep = R.PENDING_WORK, R.STATE, R.STAMP, D.STATE, D.STAMP
    try:
        R.PENDING_WORK, R.STATE, R.STAMP = _work, _state, tmp / ".decision-stamp.json"
        D.STATE, D.STAMP = _state, tmp / ".decision-stamp.json"
        _rc_drop = run(["--only", "1", "--drop", "--reason", "시험"])
        check("--drop 이 멈춘다", _rc_drop != 0, f"rc={_rc_drop}")
        check("  상태 파일이 그대로다", _state.read_text(encoding="utf-8") == _corrupt)
        _rc_later = run(["--only", "1", "--later", "7", "--reason", "시험"])
        check("--later 도 멈춘다", _rc_later != 0, f"rc={_rc_later}")
        check("  상태 파일이 그대로다", _state.read_text(encoding="utf-8") == _corrupt)
        _rc_clear = run(["--clear", "영업"])
        check("--clear 도 멈춘다", _rc_clear != 0, f"rc={_rc_clear}")
        check("  상태 파일이 그대로다", _state.read_text(encoding="utf-8") == _corrupt)
        # 없는 파일은 깨진 파일이 아니다 — 잃을 것이 없으니 평소처럼 적는다
        _state.unlink()
        check("상태 파일이 없으면 평소처럼 적는다",
              run(["--only", "1", "--drop", "--reason", "시험"]) == 0
              and "영업|new-channel" in (read(_state).get("dismissed") or {}))
    finally:
        R.PENDING_WORK, R.STATE, R.STAMP, D.STATE, D.STAMP = _keep

print("\n「빼」의 요약 해시 — md 가 없으면 영구가 된다고 알린다")

# drop() 은 요약 계열에 R.summary_hash 를 적는데, 채널 md 가 없으면 JS 쪽이 빈 문자열을
# 돌려주고, suppress(JS)·_still_dismissed(파이썬)는 「해시 없음 = 계속 뺌」으로 읽는다 —
# 요약이 바뀌어도 안 돌아온다. 그 사실이 화면에 안 나오면 사람은 「요약 고치면 돌아온다」로
# 알고 넘어간다. 판정 규칙은 두 언어에 걸쳐 있어 안 바꾸고, 알리기만 하는지를 잰다.
with tempfile.TemporaryDirectory() as d:
    _state = Path(d) / ".sync-state.json"
    _state.write_text("{}", encoding="utf-8")
    _keepDS = D.STATE
    try:
        D.STATE = _state
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = D.drop([{"id": "없는채널-시험|summary|수치|핵심 쟁점", "kind": "summary",
                           "channel": "없는채널-시험", "file": "없는채널-시험"}], "시험")
        check("md 없는 채널의 「빼」도 기록은 된다", _rc == 0, f"rc={_rc}")
        check("  영구가 된다는 경고가 나온다",
              "요약 해시를 못 냈습니다" in _buf.getvalue(), _buf.getvalue())
        if HAVE_MD:
            _buf2 = io.StringIO()
            with contextlib.redirect_stdout(_buf2):
                _rc2 = D.drop([{"id": ITEM_ID, "kind": "summary",
                                "channel": REAL_CHANNEL, "file": REAL_CHANNEL}], "시험")
            check("md 가 있으면 경고가 안 나온다",
                  _rc2 == 0 and "요약 해시를 못 냈습니다" not in _buf2.getvalue())
        else:
            skip("채널 md 가 하나도 없어 「경고 없음」 쪽을 잴 수 없습니다")
    finally:
        D.STATE = _keepDS

_tail = f" (건너뜀 {_skipped}개)" if _skipped else ""
print(("\n전부 통과" if _ok else "\n실패 있음") + _tail)
sys.exit(0 if _ok else 1)
