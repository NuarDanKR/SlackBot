#!/usr/bin/env python3
"""
대화 아카이브(자료 저장소의 `slack-export/`)의 **구조 결함**을 찾는다. 고치지는 않는다.

사용:
  python verify_archive.py --all                 # 전 채널 + index.md
  python verify_archive.py <채널md> [...]        # 고른 파일만
  python verify_archive.py --all --json          # 마지막 줄에 결과 JSON 한 줄
  python verify_archive.py <채널md> --from-index # 스테이징된 내용을 본다 (커밋 관문용)

종료코드: 0 이상 없음 / 1 결함 있음 / 2 못 읽음 (git·경로 문제)

── 무엇을 찾나 ──

파생값(건수·표)은 `sync_index.py` 가 **다시 계산해 고친다**. 여기서 찾는 것은 계산으로 못
고치는 것들이다 — 슬랙에는 있는데 md 에 안 들어온 것, 봇이 남의 발언으로 읽게 되는 모양.

  ① 스레드 머리줄의 건수 ≠ 실제 답글 줄 수
  ② 수집 실패 표식이 남아 있음 (「답글이 반환되지 않았습니다」 등)
  ③ 헤더의 `**실제 메시지**: N건` ≠ 실제      ← ①③④⑤⑧ 은 sync_index 가 고친 뒤엔 늘 0이다.
  ④ `**기간**` 끝날짜가 실제 최신 메시지와 다름 — **양쪽 방향 다**. 「오래됨」은
     sync_index 의 회귀 탐지기지만 **「실제보다 새것」은 여기서만 잡는다**: 그쪽은
     끝날짜를 뒤로만 밀어 이 방향을 고치는 코드가 아예 없다. 그 상태는 「이 채널은
     그날까지 자료가 있다」를 봇 프롬프트에 사실로 싣는다 (2026-09-03 추가)
  ⑤ `**기간**` 시작날짜가 실제 가장 오래된 메시지보다 새것 — 백필이 md 에 더 오래된
     메시지를 채웠는데 헤더는 그 과거를 여전히 「없다」고 말하는 경우다. ③④⑧ 과
     마찬가지로 sync_index.py 가 고치는 값이지만(Task 7, 2026-09-02), 그래도 따로
     검사를 두는 것은 이 파일 전체의 존재 이유와 같다 — 관문(`--from-index`)은
     `sync_index.py` 를 안 부르고 **이미 커밋된 md 를 그대로** 본다. 그러니 누가
     sync_index.py 를 안 돌리고 커밋해 시작날짜가 낡은 채로 들어오면, 그 회귀를
     잡는 것은 이 검사뿐이다.
  ⑥ 한 줄로 줄여 적은 메시지 (봇이 앞 메시지 작성자의 말로 읽는다)
  ⑦ 스레드 구역 안에 인용 `>` 가 빠진 줄
  ⑧ index.md 의 표·소계·총계가 실제와 다름 · 기계가 못 고친 행
  ⑨ `**기간**` 이 자리표시(`(백필 중)`·`(대화 없음)`)로 굳음 — 백필이 중간에 끊기거나
     (finally 가 Ctrl-C 를 안 덮는다) 빈 채널에 나중에 대화가 생겼을 때 남는다. ④⑤ 는
     날짜를 요구해 이 모양을 건너뛰므로 그 둘로는 절대 안 잡힌다. `sync_index.py` 가
     메시지가 있을 때 고치고(⑨ 도 ③④⑤⑧ 처럼 그쪽의 회귀 탐지기다), 메시지가 하나도
     없는 채널의 `(대화 없음)` 은 **사실이라 결함이 아니다.**
  ⑩ 순서 계약 — 메시지가 **제 달 절**에 있나 · 월 헤딩과 한 달 안이 내림차순인가 ·
     월 헤딩이 하나라도 있나 · `##` 뒤 공백이 한 칸인가. 봇의 월 단위 읽기
     (`archive.js` 의 `readChannel`)가 자료를 못 찾는 자리다. 자세한 것은
     `_order_problems` (2026-09-03 추가)
  ⑪ `**기간**`·메시지 헤더의 날짜가 **달력에 있는 날**인가. ④⑤ 는 문자열 비교라
     `2026-13-99` 같은 값이 끼면 판정이 뜻을 잃고, `sync_index.py` 는 그것으로
     덮어쓴다 (2026-09-03 추가)

── 왜 이 파일이 따로 있나 ──

`doc-archive/scripts/verify_format.py` 는 **절대 확장하지 않는다.** 자동 반영의 관문
(`src/ingest/verify.js` 의 runGate)이 그 종료코드를 그대로 쓰기 때문에, 거기에
대화 쪽 검사를 끼워 넣으면 대화 md 하나가 어긋난 날 **그날 반영이 통째로 롤백된다.**

── 부르는 곳이 둘이고, 보는 자리가 다르다 ──

  · **매일 09:00 위생 점검** — 알리기만 하고 아무것도 안 막는다. **작업 트리**를 본다
  · **커밋 관문** (`.githooks/pre-commit`, 2026-08-12 부터) — 커밋을 **막는다**.
    `--from-index` 로 불려 **스테이징된 내용**을 본다

2026-08-13 까지 이 자리는 「관문이 아니라 09:00 이 부른다 — 아무것도 안 막는다」고 적고
있었다. 훅이 이 파일을 관문으로 쓰기 시작한 커밋에서 이 줄을 안 고쳤기 때문이다. 그 회차의
주제가 「같은 판정이 한 곳뿐이라던 주석 넷 중 셋이 거짓이었다」였는데 같은 종류를 하나 새로
만든 셈이라, 리뷰에서 지적으로 돌아왔다.

**관문은 인덱스를 본다.** 사람이 본 것은 작업 트리이지만 **커밋되는 것은 인덱스**라,
작업 트리를 대면 두 방향으로 어긋난다 — 깨진 판을 `git add` 하고 작업 트리에서만 고치면
그대로 통과하고(깨진 것이 커밋된다), 멀쩡한 판을 담은 뒤 작업 트리를 더 고치면 잘못이
없는데 막힌다. 문서·요약 관문(`review_batch.py`·`review_work.py` 의 해시)이 같은 이유로
인덱스를 보는데, 이 관문만 2026-08-13 까지 작업 트리를 보고 있었다.

출력 관례(`✓/✗` · 들여쓴 `-` · `check()` 3-튜플 · 종료코드 0/1)는 `verify_format.py` 를 본떴다.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from sync_index import (  # noqa: E402
    ARCHIVE, CHANNELS, INDEX, ROOT, HEADER_RE,
    count_messages, is_date, newest_date, oldest_date, sync_channel_headers, sync_index, Invariant,
)
from paths import CONFIG  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# 메시지 헤더 — **느슨하게**. 한 줄로 줄여 적은 것까지 잡는다.
HDR = re.compile(r"^\*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2} · ")
# archive.js 의 splitMessages 가 블록을 자르는 모양 — 줄이 거기서 끝나야 한다.
STRICT_HDR = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$")

# 스레드 머리줄. 꼬리말이 **볼드 안**에 드는 형태가 15건 있다
# (`> 💬 **스레드 (2) — 오산세교 현황 및 향후 일정**`). `\((\d+)\)\*\*` 로만 잡으면 그 15건을
# 조용히 건너뛴다 — 실측 117 vs 132.
THREAD_HEAD = re.compile(r"^>\s*💬\s*\*\*스레드\s*\((\d+)\)[^*]*\*\*")
# 볼드 없는 `> 💬 스레드 N건 (봇 답변 — 미수록)` 류까지 포함. 구역을 끊는 데만 쓴다.
THREAD_ANY = re.compile(r"^>?\s*💬")
REPLY = re.compile(r"^>\s*\*\*└")
PERIOD = re.compile(r"\*\*기간\*\*:\s*(\d{4}-\d{2}-\d{2})(?:\s*~\s*(\d{4}-\d{2}-\d{2}))?")
# 백필이 적어 두는 자리표시 기간. **`sync_index.py` 의 `PERIOD_PLACEHOLDER_RE` 와 짝이다**
# — 그쪽이 고치고 이쪽이 회귀를 잡는다(③④⑤⑧ 과 같은 관계). 날짜가 아니라 위 `PERIOD`
# 에는 안 걸리고, 그래서 예전에는 ④⑤ 가 통째로 건너뛴 채 43/43 통과가 났다.
PERIOD_PLACEHOLDER = re.compile(r"\*\*기간\*\*:\s*\((백필 중|대화 없음)\)")

# 월 헤딩 — **읽는 쪽과 같은 엄격함**(`archive.js` 의 `MONTH_HEADING` · `readChannel` 의
# `l.trim() === '## ' + month`). `##` 뒤 공백이 **한 칸**이어야 한다.
MONTH_STRICT = re.compile(r"^## (\d{4}-\d{2})\s*$")
# 쓰는 쪽·검사 쪽 여섯 자리(`summary.js`·`ingest/verify.js`·`insert_entry.py`·
# `verify_format.py`·`insert_messages.py`·`review_work.py`)가 쓰는 느슨한 모양.
# 둘 사이로 빠지는 줄(`##  2026-08` — 공백 두 칸)은 **관문을 다 통과하면서** 봇의 월 단위
# 읽기만 막는다. 그래서 여기서는 느슨한 쪽으로 찾아 놓고 엄격한 쪽에 안 걸리면 결함으로 센다.
MONTH_LOOSE = re.compile(r"^##\s+(\d{4}-\d{2})\s*$")

# 한 파일에서 순서 결함을 몇 줄까지 펼칠지. 넘치면 개수만 적는다 — 포맷이 통째로
# 어긋난 파일 하나가 화면을 덮으면 나머지 41개를 아무도 안 본다.
ORDER_MAX_SHOWN = 10

# 「슬랙에는 있는데 md 에 안 들어왔다」는 뜻인 문구만 본다.
# 머리줄에 딸린 제목을 전부 의심하면 안 된다 — `— Claude 앱 응답 (요약 실패)` 는
# 슬랙에서 실제로 일어난 일이지 아카이브 결함이 아니다.
FAIL_MARKS = ["반환되지 않", "수집 실패", "추출 실패", "응답 공백", "미수집 구간"]


def _thread_regions(lines):
    """[(머리줄 인덱스, 적힌 건수, 구역 [시작, 끝))] — 볼드 머리줄만.

    구역의 끝을 어디로 보느냐가 ①⑦의 오탐을 전부 좌우한다. 특히 **`## ` 를 끝으로 봐야**
    한다 — 각 달 마지막 메시지의 스레드 바로 뒤에 `## YYYY-MM` 월 헤딩이 오는데, 그것을
    위반으로 세면 채널마다 오탐이 난다. 위반이 아니라 구역이 거기서 끝난 것이다.
    """
    out = []
    for i, line in enumerate(lines):
        m = THREAD_HEAD.match(line)
        if not m:
            continue
        j = i + 1
        while j < len(lines):
            s = lines[j]
            if HDR.match(s) or s.startswith("## ") or s.startswith("---") or THREAD_ANY.match(s):
                break
            j += 1
        out.append((i, int(m.group(1)), i + 1, j))
    return out


def _order_problems(name: str, lines: list) -> list:
    """⑩ 순서 계약 — **메시지가 제 달 절에 있고, 월 헤딩과 한 달 안이 내림차순인가.**

    ── 왜 필요한가 ──

    봇의 월 단위 읽기(`archive.js` 의 `readChannel`)는 `## YYYY-MM` 줄을 찾아 **다음
    `## ` 전까지를 잘라** 돌려준다. 그러니 자료가 어느 절에 놓였느냐가 곧 「그 달에
    자료가 있느냐」다. 6월 메시지가 `## 2026-08` 절 안에 놓이면 파일에는 있는데
    `read_channel(month:"2026-06")` 은 **「2026-06 섹션이 없습니다」로 답한다** —
    에러가 아니라 **없다고 답하는** 오답이고, ①~⑨ 어느 검사도 이것을 안 봤다
    (2026-09-03 실측: 실물 한 채널이 이 상태인 채 `--all` 이 43/43 통과를 냈다).

    월 헤딩이 **하나도 없는** 파일도 같은 자리다 — 그 채널은 어느 달로도 안 열린다.
    ④⑤ 는 기간만 보고 ①~③⑥~⑨ 는 블록 안만 보므로 역시 아무도 안 잡았다.

    ── 무엇을 기준으로 「제 달」을 정하나 ──

    절을 여는 것은 **읽는 쪽 규칙**(`MONTH_STRICT`)이다. 느슨한 쪽에만 걸리는 줄
    (`##  2026-08`)은 봇에게는 월 헤딩이 아니므로 절을 열지 않고, 그 사실 자체를
    결함으로 적는다. 검사가 쓰는 쪽 규칙으로 절을 열면 **봇이 못 읽는 배치를
    통과시키게 된다.**

    내림차순인 것은 이 아카이브의 쓰기 규칙이다 — 새 메시지가 위에 꽂힌다
    (`insert_messages.py`). 오름차순으로 뒤집힌 구간은 삽입 자리가 틀렸다는 뜻이고,
    그대로 두면 다음 삽입이 그 틈에 또 쌓인다.
    """
    out = []
    cur = None          # 지금 열려 있는 월 절 (읽는 쪽 기준)
    prev_month = None
    prev_date = None
    n_month = 0
    stray = 0
    stray_first = None
    has_msg = False

    for i, line in enumerate(lines):
        loose = MONTH_LOOSE.match(line)
        if loose:
            if not MONTH_STRICT.match(line):
                out.append(
                    f"{name}:{i + 1} 월 헤딩의 `##` 뒤 공백이 한 칸이 아닙니다 — 관문은 "
                    f"통과시키지만 봇의 월 단위 읽기가 {loose.group(1)} 을 못 찾습니다: "
                    f"{line.strip()[:40]}"
                )
                continue
            n_month += 1
            if prev_month and loose.group(1) > prev_month:
                out.append(
                    f"{name}:{i + 1} 월 헤딩이 내림차순이 아닙니다 "
                    f"({prev_month} 다음에 {loose.group(1)})"
                )
            prev_month, cur, prev_date = loose.group(1), loose.group(1), None
            continue

        h = HEADER_RE.match(line)
        if not h:
            continue
        has_msg = True
        d = h.group(1)
        if cur is None:
            stray += 1
            if stray_first is None:
                stray_first = (i + 1, d)
            continue
        if d[:7] != cur:
            out.append(
                f"{name}:{i + 1} {d} 메시지가 `## {cur}` 절에 있습니다 — 봇이 "
                f"read_channel(month:\"{d[:7]}\") 로는 **없다고 답합니다**"
            )
        if prev_date and d > prev_date:
            out.append(
                f"{name}:{i + 1} 한 달 안이 내림차순이 아닙니다 ({prev_date} 다음에 {d})"
            )
        prev_date = d

    if has_msg and n_month == 0:
        out.append(
            f"{name} 월 헤딩이 하나도 없습니다 — 봇의 월 단위 읽기가 이 채널의 "
            "어느 달도 못 찾습니다"
        )
    elif stray:
        out.append(
            f"{name}:{stray_first[0]} 첫 월 헤딩 앞에 메시지가 {stray}건 있습니다 "
            f"({stray_first[1]} …) — 그 메시지들은 어느 달로도 안 열립니다"
        )

    if len(out) > ORDER_MAX_SHOWN:
        rest = len(out) - ORDER_MAX_SHOWN
        out = out[:ORDER_MAX_SHOWN] + [f"{name} 순서 결함 그 외 {rest}건 (한 파일에서 접었습니다)"]
    return out


class GitError(RuntimeError):
    """git 을 못 읽었다. **빈 내용으로 돌려주지 않는다** — 그러면 관문이 「볼 것 없음」과
    「못 봄」을 같은 0 으로 답하고, 훅이 걸려 있는데 아무것도 안 지키게 된다.

    **`archive-inbox/review_work.py`·`doc-archive/review_batch.py` 에도 같은 것이 따로 있다.**
    스킬끼리 임포트를 늘리면 한쪽만 가져간 저장소에서 훅이 ImportError 로 죽어 남의 커밋을
    전부 막는다 — 고치려던 문제를 다른 자리에 만든다.
    """


def index_text(path: Path) -> str:
    """**스테이징(인덱스)에 담긴 내용.** 관문이 보는 자리다 (머리말 참조).

    인덱스에는 줄끝이 LF 로 들어가 있어서 작업 트리(CRLF)와 바이트가 다르다. 여기서는
    내용을 줄 단위로만 보므로 정규화가 필요 없다 — 해시를 대는 쪽(`review_work.py` 의
    `_digest`)과 달리 그 차이가 판정에 안 닿는다.
    """
    try:
        rel = path.resolve().relative_to(Path(ROOT).resolve()).as_posix()
    except ValueError:
        raise GitError(f"{path} 가 저장소({ROOT}) 밖입니다")
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", f":{rel}"],
        cwd=str(ROOT), capture_output=True,
    )
    if r.returncode != 0:
        head = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git show :{rel} — "
                       + (head[0] if head else f"종료코드 {r.returncode}"))
    return r.stdout.decode("utf-8")


def _baseline():
    try:
        cfg = json.loads(CONFIG.read_bytes().decode("utf-8"))
        return (cfg.get("digest", {}).get("health", {}).get("archive", {})
                   .get("onelineBaseline", {})) or {}
    except Exception:
        return {}


def check(path: Path, baseline: dict, text=None):
    """(문제 목록, 참고 목록, 메시지 건수). verify_format.py 의 check() 와 같은 계약.

    `text` 를 주면 그 내용으로 판정한다 — 관문이 **인덱스에 담긴 내용**을 넘길 때 쓴다.
    `path` 는 그때도 필요하다: 이름이 문제 문장에 들어가고, 파일 이름(`stem`)이 ⑥의
    기준선을 고르는 열쇠다.
    """
    if text is None:
        text = path.read_bytes().decode("utf-8")
    lines = text.split("\n")
    problems, notes = [], []
    total = count_messages(text)

    # ① 스레드 머리줄 건수 ≠ 실제 답글 줄 수
    #    날짜 없는 옛 답글(`> **└ WHK** — …`, 초기 수기 추출분)도 정상으로 센다 — 날짜를
    #    요구하면 그것들이 전부 오탐이 된다. 여러 줄 답글의 이어지는 줄은 `> **└` 가 아니라
    #    저절로 빠진다.
    for head_i, said, lo, hi in _thread_regions(lines):
        actual = sum(1 for k in range(lo, hi) if REPLY.match(lines[k]))
        if actual != said:
            problems.append(
                f"{path.name}:{head_i + 1} 스레드 머리줄은 {said}건인데 답글 줄은 {actual}개"
                + ("  ← 슬랙에는 있는데 안 들어온 것일 수 있습니다" if actual < said else "")
            )

    # ② 수집 실패 표식 잔존
    for i, line in enumerate(lines):
        for mark in FAIL_MARKS:
            if mark in line:
                problems.append(f"{path.name}:{i + 1} 수집 실패 표식이 남아 있습니다 — {line.strip()[:80]}")
                break

    # ③ 헤더 건수
    m = re.search(r"\*\*실제 메시지\*\*:\s*(?:약\s*)?(\d+)건", text)
    if not m:
        problems.append(f"{path.name} 헤더에 '**실제 메시지**: N건' 이 없습니다")
    elif int(m.group(1)) != total:
        problems.append(f"{path.name} 헤더 {m.group(1)}건인데 실제 {total}건 (sync_index.py 로 맞추세요)")

    # ⑪ 날짜가 달력에 있는 값인가. **④⑤ 보다 먼저 본다** — 그 둘은 문자열 비교라,
    #    `2026-13-99` 같은 값이 끼면 「어긋났다/안 어긋났다」 판정 자체가 뜻이 없다.
    #    `sync_index.py` 도 같은 값을 만나면 기간에 손을 안 대고 `bad_date` 로 넘긴다.
    pm = PERIOD.search(text)
    newest = newest_date(text)
    oldest = oldest_date(text)
    bad_dates = sorted({
        d for d in (newest, oldest, pm.group(1) if pm else None, pm.group(2) if pm else None)
        if d and not is_date(d)
    })
    if bad_dates:
        problems.append(
            f"{path.name} 달력에 없는 날짜: {', '.join(bad_dates)} — 기간 비교는 문자열끼리라 "
            "이런 값이 끼면 sync_index.py 가 더 기형적인 값으로 덮어씁니다"
        )

    # ④ 기간 끝날짜 — **양방향으로 본다.**
    #
    #    `sync_index.py` 는 끝날짜를 **뒤로만 민다**(최신 메시지는 늘어나기만 하므로).
    #    그래서 반대 방향 — 헤더가 실제 최신 메시지보다 **새것**인 경우 — 은 고치는 쪽이
    #    아무도 없고, 여기서도 `end < newest` 만 봐서 **어떤 관문도 안 봤다.** 그 상태는
    #    「이 채널은 그날까지 자료가 있다」는 거짓을 봇 시스템 프롬프트에 사실로 싣는다
    #    (`archive.js` 의 metaBlock 이 `>` 메타를 통째로 싣는다). 2026-09-03 실측으로
    #    실물 두 채널이 그 상태인 채 `--all` 이 43/43 통과를 냈다.
    #
    #    **시작날짜(⑤)는 양방향으로 만들지 않는다** — 기간 시작을 채널 개설일로 적는
    #    채널이 여럿이라(21개 실측), 「시작이 첫 메시지보다 이르다」를 결함으로 세면
    #    그 21개가 통째로 빨개진다. 끝날짜에는 그런 관례가 없다.
    if pm and newest and not bad_dates:
        end = pm.group(2) or pm.group(1)
        if end < newest:
            problems.append(f"{path.name} 기간 끝날짜 {end} 인데 최신 메시지는 {newest}")
        elif end > newest:
            problems.append(
                f"{path.name} 기간 끝날짜 {end} 인데 실제 최신 메시지는 {newest} — "
                "없는 자료가 있다고 말합니다 (sync_index.py 는 끝을 뒤로만 밀어 이 방향을 안 고칩니다)"
            )

    # ⑤ 기간 시작날짜 — ④ 와 방향이 반대다. 끝은 「실제보다 오래됨」이 결함이지만
    # (최신 메시지는 늘어나기만 하므로), 시작은 **「실제보다 새것」이 결함이다**
    # (백필이 더 오래된 메시지를 채워 내려가기만 하므로). 시작이 실제보다 오래된
    # 쪽으로 어긋나 있는 것은 결함이 아니다 — 채널 개설일처럼 원래 첫 메시지보다
    # 이른 시작날짜를 쓰는 채널이 있고, sync_index.py 도 그 경우는 안 건드린다.
    if pm and oldest and not bad_dates:
        start = pm.group(1)
        if start > oldest:
            problems.append(f"{path.name} 기간 시작날짜 {start} 인데 가장 오래된 메시지는 {oldest}")

    # ⑨ 기간이 자리표시(`(백필 중)`·`(대화 없음)`)로 굳음 — ④⑤ 는 날짜를 요구해서
    #    이 모양을 통째로 건너뛴다. 그 문자열은 `archive.js` 의 metaBlock 을 타고 봇
    #    시스템 프롬프트에 **사실로** 실린다.
    #    `(백필 중)` 은 언제나 결함이다 — `run-backfill.js` 의 finally 가 끝에 실제
    #    범위로 바꾸므로, 남아 있다는 것은 그 finally 가 안 돌았다는 뜻이다(Ctrl-C·강제
    #    종료). `(대화 없음)` 은 **메시지가 있을 때만** 결함이다 — 정말 빈 채널에는
    #    그게 사실이라, 거기까지 잡으면 빈 채널마다 매 회차 우는 거짓 경보가 된다.
    php = PERIOD_PLACEHOLDER.search(text)
    if php and (php.group(1) == "백필 중" or newest):
        problems.append(
            f"{path.name} 기간이 자리표시 ({php.group(1)}) 로 남아 있습니다"
            + (f" — 실제 메시지는 {oldest} ~ {newest}" if newest else " — 백필이 끝나지 않았습니다")
        )

    # ⑥ 한 줄로 줄여 적은 메시지 — 총량이 아니라 **파일별 기준선**으로 본다.
    #    총량만 두면 "한 채널에서 줄고 다른 채널에서 는" 상황을 못 잡는다.
    oneline = [i + 1 for i, l in enumerate(lines) if HDR.match(l) and not STRICT_HDR.match(l)]
    base = int(baseline.get(path.stem, 0))
    if len(oneline) > base:
        where = ", ".join(f"{path.name}:{n}" for n in oneline[:5])
        extra = f" (기준선 {base}건)" if base else " (기준선에 없는 파일입니다 — 새로 생긴 것은 결함입니다)"
        problems.append(
            f"한 줄로 줄여 적은 메시지 {len(oneline)}건{extra} — "
            f"splitMessages 가 안 잘라 **앞 메시지 작성자의 말로 읽힙니다**. {where}"
        )
    elif len(oneline) < base:
        notes.append(f"한 줄 메시지가 기준선({base})보다 줄었습니다 — 지금 {len(oneline)}건. 기준선을 낮추세요")

    # ⑩ 순서 계약 — 메시지가 제 달 절에 있고, 월 헤딩과 한 달 안이 내림차순인가
    problems += _order_problems(path.name, lines)

    # ⑦ 스레드 구역 안에 인용 `>` 가 빠진 줄
    for _head_i, _said, lo, hi in _thread_regions(lines):
        for k in range(lo, hi):
            s = lines[k]
            if s.strip() and not s.lstrip().startswith(">"):
                problems.append(
                    f"{path.name}:{k + 1} 스레드 안인데 인용 `>` 가 없습니다 — {s.strip()[:60]}"
                )

    return problems, notes, total


def check_index():
    """index.md — sync_index.py 를 dry 로 돌려 「고칠 것이 남아 있나」를 본다.

    검사를 여기 다시 구현하지 않는다. 파생값의 정답은 그쪽 하나뿐이고, 두 곳에 두면 갈린다.
    """
    problems, notes = [], []
    try:
        counts, ch, un = sync_channel_headers(dry=True)
        ic, iu = sync_index(counts, dry=True)
    except Invariant as e:
        return [f"index.md 쓰기 불변식 위반 — {e}"], [], 0
    for c in ch + ic:
        problems.append(f"{c['where']} 파생값이 낡았습니다 — {c['what']} (sync_index.py --all 로 맞추세요)")
    for u in un + iu:
        notes.append(f"{u['where']} [{u['kind']}] {u['detail']}")
    return problems, notes, len(counts)


def main():
    ap = argparse.ArgumentParser(description="대화 아카이브 구조 검사")
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true", help="전 채널 + index.md")
    ap.add_argument("--json", action="store_true", help="마지막 줄에 결과 JSON 한 줄")
    ap.add_argument("--from-index", action="store_true",
                    help="작업 트리가 아니라 스테이징된 내용을 본다 (커밋 관문용)")
    args = ap.parse_args()

    targets = list(args.paths)
    if args.all:
        targets += sorted(CHANNELS.glob("*.md"))
    if not targets:
        print("✗ 검사할 파일이 없습니다 (--all 또는 파일 경로)", file=sys.stderr)
        return 1

    # `--all` 은 작업 트리를 훑어 목록을 만들고, `index.md` 검사는 `sync_index` 로 작업
    # 트리의 파생값을 다시 센다. 거기에 인덱스 내용을 섞으면 **두 자리를 대는 검사**가 되어
    # 어느 쪽이 틀렸는지 알 수 없다. 관문은 늘 경로를 주므로 함께 쓸 일이 없다.
    if args.from_index and args.all:
        print("✗ --from-index 는 --all 과 함께 쓸 수 없습니다 (관문은 파일 경로를 줍니다)",
              file=sys.stderr)
        return 2

    baseline = _baseline()
    results, all_problems = [], []
    passed = 0
    unreadable = 0

    for p in targets:
        text = None
        if args.from_index:
            # **못 읽은 것을 「이상 없음」으로 셈하지 않는다** — 종료코드 2 로 끝난다.
            try:
                text = index_text(p)
            except GitError as e:
                unreadable += 1
                if not args.json:
                    print(f"✗ {p} — 스테이징된 내용을 읽지 못했습니다: {e}")
                continue
        elif not p.exists():
            all_problems.append(f"{p} — 파일 없음")
            if not args.json:
                print(f"✗ {p} — 파일 없음")
            continue
        problems, notes, n = check(p, baseline, text)
        rel = p.relative_to(ARCHIVE) if ARCHIVE in p.parents else p
        if not args.json:
            print(f"{'✓' if not problems else '✗'} {rel}  (메시지 {n}건)")
            for x in problems:
                print(f"    - {x}")
            for x in notes:
                print(f"    · {x}")
        if problems:
            results.append({"file": str(rel), "problems": problems, "notes": notes})
            all_problems += problems
        else:
            passed += 1
            if notes:
                results.append({"file": str(rel), "problems": [], "notes": notes})

    if args.all and INDEX.exists():
        problems, notes, n = check_index()
        if not args.json:
            print(f"{'✓' if not problems else '✗'} index.md  (채널 {n}개)")
            for x in problems:
                print(f"    - {x}")
            for x in notes:
                print(f"    · {x}")
        if problems or notes:
            results.append({"file": "index.md", "problems": problems, "notes": notes})
        all_problems += problems
        if not problems:
            passed += 1

    if args.json:
        print(json.dumps({
            "total": len(all_problems),
            "unreadable": unreadable,
            "files": [r for r in results if r["problems"] or r["notes"]],
        }, ensure_ascii=False))
    else:
        print(f"\n{passed}/{passed + len([r for r in results if r['problems']])} 통과"
              f" · 결함 {len(all_problems)}건"
              + (f" · 못 읽은 것 {unreadable}건" if unreadable else ""))
    # 못 읽은 것이 있으면 결함 여부와 무관하게 2 다 — 「봤는데 이상 없다」와 구별돼야 한다.
    if unreadable:
        return 2
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
