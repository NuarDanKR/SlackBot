#!/usr/bin/env python3
"""
아카이브의 **파생값**을 다시 계산해 맞춘다. 판단이 필요한 값은 손대지 않는다.

파생값 = md 파일에서 100% 계산되는 값이다. 사람이 정할 것이 없으므로 감시하고 알릴 이유도
없다 — 그냥 다시 계산해 넣으면 된다. 셋을 맡는다.

  ① 채널 md 헤더의 `**실제 메시지**: N건`  (그리고 필요할 때만 `**기간**` 의 시작·끝날짜 —
     끝은 뒤로만 밀고 시작은 앞으로만 당긴다, Task 7)
  ② index.md 채널별 건수 표의 숫자 칸
  ③ index.md 의 섹션 소계 `### 제목 (N)` · 총계 줄 · 꼬리말의 최종 동기화 시각

사용:
  python sync_index.py --all                 # 다시 계산해서 쓴다
  python sync_index.py --all --dry-run       # 계산만 하고 안 쓴다
  python sync_index.py --all --json          # 마지막 줄에 결과 JSON 한 줄 (자동 반영이 읽는다)
  python sync_index.py --check               # --dry-run 과 같다 (읽기 전용이라는 뜻을 이름으로)

종료코드: 0 정상 / 1 쓰기 불변식 위반 또는 실패

── 왜 이 파일 하나인가 ──

계산을 두 곳에 두면 언젠가 갈린다. 2026-08-10 에 실제로 갈렸다 — 사람은 봇이 보는 **블록
수**(55)를 셌고 코드(`src/ingest/slack-archive.js`)는 **사람 발언 수**(54)를 세서,
사람이 헤더를 55로 "고쳤다". 그래서 계산은 여기 한 곳에 두고, VM 의 자동 반영도 사람이 쓰는
slack-sync 스킬도 **이 스크립트를 부른다**.

── 왜 게이트가 아니라 여기서 막는가 ──

이 스크립트는 **쓰는** 쪽이다. 자동 반영의 관문(`ingest/verify.js` 의 runGate)에 검사를 더하면
실패했을 때 `git reset --hard` 로 그날 대화·문서·로그 반영이 통째로 사라진다. 파생값 하나
때문에 치를 값이 아니다. 대신 쓰기 직전에 불변식 셋을 확인하고, 하나라도 깨지면 **아무것도
쓰지 않고** 종료코드 1 로 멈춘다. 그래서 이 스크립트는 「고치거나, 손대지 않거나」 둘 중
하나만 한다.

── index.md 를 고칠 때의 제약 ──

index.md 는 봇 시스템 프롬프트에 **통째로** 실리고(`src/archive.js`), 공개 답변에서는
`redactPrivateMentions` 가 비공개 채널명이 든 **줄을 통째로 버린다**. 그래서:

  · 줄을 추가하지 않는다. 「갱신한 채널: …」 같은 요약 줄을 쓰면 공개 사용자에게는 그 줄이
    통째로 사라져 거기 적힌 공개 채널 숫자까지 함께 없어진다.
  · 행을 자동으로 추가하지 않는다. 3번째 칸(성격)은 판단이고, 🔒 를 잘못 붙이면 비공개
    채널명이 전사에 노출된다. 「표에 없는 채널이 있다」고 알리고 사람이 한 줄 쓴다.
  · 치환은 재렌더가 아니라 **매치 자리의 숫자만** 갈아 끼운다.
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from insert_messages import read_lines, write_lines, update_header  # noqa: E402
from paths import ARCHIVE, CHANNELS, DATA_ROOT  # noqa: E402,F401
import tz as _tz  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# `ROOT` 는 **자료 저장소**다 — 채널 md 가 있고, git 을 돌리며, `relative_to` 의 기준이
# 되는 자리. 코드 저장소가 필요한 곳은 `paths.CODE_ROOT` 를 따로 가져다 쓴다.
# 이름을 그대로 둔 것은 여기서 `ROOT` 를 가져다 쓰는 다섯 자리
# (`verify_archive`·`apply_edits`·`board`·`review_work`·`decide_work`)가 전부
# 자료 저장소를 뜻하기 때문이다.
ROOT = DATA_ROOT
INDEX = ARCHIVE / "index.md"
STATE = ARCHIVE / ".sync-state.json"

# 정본은 `_shared/tz.py` (2026-09-03) — config.json 의 timezone 을 읽는다. 전에는 여기서
# 고정 +09:00 을 직접 만들었는데, 그 값이 config.json 과 갈리는 날 조용히 어긋난다.
KST = _tz.TZINFO

# ── 계약: 아래 둘은 봇 소스(`src/`)와 글자 그대로 같아야 한다 ────────────────
# slack-archive.js 의 MD_HEADER (그쪽 이름은 다르고, 잡는 모양이 같아야 한다)
HEADER_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2}) \d{2}:\d{2} · ", re.M)
# slack-live.js 의 BOT_BLOCK_BODY — 봇이 올린 글의 자리표시 본문 줄
BOT_BLOCK_BODY = "(봇 발신 — 본문 미수록)"
# ─────────────────────────────────────────────────────────────────────────────

CHANNEL_SECTION = "## 채널 목록"
SEP_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")
SUBHEAD_RE = re.compile(r"^(###\s+.*?\s*\()(\d+)(\)\s*)$")
TOTAL_RE = re.compile(r"^(>\s*\*\*채널\*\*:\s*총\s*)(\d+)(개\s*\(공개\s*)(\d+)(\s*\+\s*비공개\s*)(\d+)(\)\s*)$")
FOOTER_RE = re.compile(r"(최종 동기화\s*)(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
# group(1) 시작날짜 · group(2) 끝날짜(단일 날짜 채널이면 없음).
# 예전에는 `PERIOD_END_RE` 로 끝날짜만 캡처했다 — 시작날짜는 백필이 없던 시절엔
# 절대 안 바뀌는 값이라 캡처할 이유가 없었다. 백필이 생겨 시작날짜도 움직이게
# 되면서 verify_archive.py 의 `PERIOD` 와 같은 모양으로 맞췄다 (Task 7, 2026-09-02).
# (줄번호는 안 적는다 — 이 주석을 쓴 커밋에서 이미 한 번 낡아 있었다: 그 커밋이
# docstring 에 다섯 줄을 더하면서 `PERIOD` 가 86행에서 91행으로 밀렸다.)
PERIOD_RE = re.compile(r"\*\*기간\*\*:\s*(\d{4}-\d{2}-\d{2})(?:\s*~\s*(\d{4}-\d{2}-\d{2}))?")
# 백필이 적어 두는 **자리표시** 기간. 날짜가 아니라 `PERIOD_RE` 에 안 걸린다.
#   `(백필 중)`   — 뼈대(`ingest/backfill.js` 의 channelSkeleton)가 읽기 **전에** 적는다.
#                  `run-backfill.js` 의 finally 가 실제 범위로 바꾸는데, 그 finally 는
#                  **Ctrl-C·강제 종료에는 안 돈다** — 42채널 백필은 몇 시간짜리다.
#   `(대화 없음)` — 백필이 끝났는데 그 채널에 담을 메시지가 0건일 때 적는다.
# 굳으면 아무도 못 고쳤다: PERIOD_RE 가 안 걸려 여기서 손을 못 댔고 verify_archive 의
# `PERIOD` 도 같은 이유로 못 봤다. 그런데 그 `>` 메타 블록은 `archive.js` 의 metaBlock 이
# 접기도 상한도 없이 **봇 시스템 프롬프트에 그대로** 싣는다 — 에러가 아니라 틀린 사실로
# 나간다. (최종 검토 F3, 2026-09-02: 검토자 둘이 각각 찾았고 한쪽은 사본에서 재현했다.)
#
# **메시지가 있을 때만 고친다.** 메시지가 하나도 없는 채널의 `(대화 없음)` 은 **사실**이라
# 고칠 것이 없다 — 거기까지 손대면 「고치기」가 매 회차 도는 거짓 경보가 된다.
PERIOD_PLACEHOLDER_RE = re.compile(r"\*\*기간\*\*:\s*\((?:백필 중|대화 없음)\)")

# 표 행. 링크 있는 행과 링크 없는 행(건수 0인 채널)을 함께 잡는다.
ROW_RE = re.compile(
    r"^\|\s*"
    r"(?:\[(?P<name>[^\]]+)\]\(channels/(?P<file>[^)]+)\.md\)"
    r"|(?P<plain>[^|\[\]]+?))"
    r"\s*(?P<lock>🔒)?\s*\|\s*(?P<count>\d+)\s*\|"
)


# ══ 파생값 계산 ══════════════════════════════════════════════════════════════

def is_date(s) -> bool:
    r"""`YYYY-MM-DD` 가 **달력에 실제로 있는 날**인가.

    `PERIOD_RE`·`HEADER_RE` 는 **모양만** 본다 (`\d{4}-\d{2}-\d{2}`). 그래서
    `2026-13-99` 같은 값이 그대로 통과하고, 아래 기간 비교는 그것을 **문자열끼리**
    댄다(`newest > cur_end`). 사전식으로는 `2026-13-99` 가 어떤 정상 날짜보다도 크므로
    끝날짜가 그 값으로 덮어써지고, 한 번 박히면 다음 회차부터는 그것이 「지금 끝」이라
    **더 기형적인 값만 이길 수 있다** — 되돌아오지 않는다. 에러는 한 번도 안 난다.

    그래서 비교 전에 여기서 거르고, 걸리면 **아무것도 안 쓰고** 사람에게 넘긴다
    (`bad_date`). 고치는 것은 판단이다 — 어느 쪽이 맞는 날인지 기계가 모른다.
    """
    if not isinstance(s, str):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    # `date.fromisoformat` 은 파이썬 3.11+ 에서 `20260101`·`2026-W01-1` 도 받는다.
    # 여기서 다루는 계약은 `YYYY-MM-DD` 하나뿐이라 모양까지 못 박는다.
    return len(s) == 10 and s[4] == "-" and s[7] == "-"


def _placeholder_headers(lines):
    """봇 자리표시 블록의 **헤더 줄 인덱스** 집합.

    자리표시는 `**날짜 시각 · 봇**` 헤더 바로 아래(빈 줄은 건너뛰고) 본문 자리에
    `(봇 발신 — 본문 미수록)` 한 줄이 오는 모양이다.
    """
    out = set()
    for i, line in enumerate(lines):
        if not HEADER_RE.match(line):
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            s = lines[j].strip()
            if not s:
                continue
            if s.startswith(BOT_BLOCK_BODY):
                out.add(i)
            break
    return out


def count_messages(text: str) -> int:
    """`**실제 메시지**: N건` 에 적는 값 — 슬랙에 **사람이** 올린 메시지 수.

    봇 자리표시 블록은 세지 않는다. 봇 글 자리를 세면 그만큼 부풀려 말하게 된다
    (`slack-archive.js` 의 `ingestChannel` 이 `insert_messages.py` 를 부르는 자리와 같은 정의). 스레드 답글도 메시지가 아니라 안 센다.
    """
    return len(HEADER_RE.findall(text)) - sum(
        1 for l in text.split("\n") if l.strip().startswith(BOT_BLOCK_BODY)
    )


def newest_date(text: str):
    """사람이 올린 메시지 중 가장 최근 날짜 (`YYYY-MM-DD`). 없으면 None.

    자리표시 블록의 날짜는 빼야 한다 — 안 그러면 봇이 요약을 올린 날이 그 채널의
    「기간」 끝으로 적혀 사람 대화가 그날까지 있었던 것처럼 보인다.
    """
    lines = text.split("\n")
    skip = _placeholder_headers(lines)
    dates = [
        HEADER_RE.match(l).group(1)
        for i, l in enumerate(lines)
        if i not in skip and HEADER_RE.match(l)
    ]
    return max(dates) if dates else None


def oldest_date(text: str):
    """사람이 올린 메시지 중 가장 오래된 날짜 (`YYYY-MM-DD`). 없으면 None.

    `newest_date` 와 쌍이다. 백필이 md 에 더 오래된 메시지를 채우면 이 값이 내려가고,
    `sync_channel_headers` 가 그 값으로 `**기간**` **시작**날짜를 맞춘다 (Task 7,
    2026-09-02) — 안 그러면 백필이 채운 과거를 헤더가 여전히 「없다」고 말해서, 봇이
    옛 대화를 물으면 근거가 있는데도 자료가 없다고 오답한다.
    """
    lines = text.split("\n")
    skip = _placeholder_headers(lines)
    dates = [
        HEADER_RE.match(l).group(1)
        for i, l in enumerate(lines)
        if i not in skip and HEADER_RE.match(l)
    ]
    return min(dates) if dates else None


# ══ ① 채널 md 헤더 ═══════════════════════════════════════════════════════════

def sync_channel_headers(dry: bool):
    changed, unresolved, counts = [], [], {}

    for path in sorted(CHANNELS.glob("*.md")):
        name = path.stem
        lines, newline = read_lines(path)
        text = "\n".join(lines)
        counts[name] = count_messages(text)

        m = PERIOD_RE.search(text)
        newest = newest_date(text)
        oldest = oldest_date(text)

        # **달력에 없는 날짜가 하나라도 끼면 기간에 손대지 않는다** (`is_date` 주석 참조).
        # 아래는 전부 문자열 비교라, 거르지 않으면 기형적인 값이 이겨서 그대로 써진다.
        bad_dates = sorted({
            d for d in (newest, oldest, m.group(1) if m else None, m.group(2) if m else None)
            if d and not is_date(d)
        })
        if bad_dates:
            unresolved.append({
                "where": f"channels/{name}.md",
                "kind": "bad_date",
                "detail": f"달력에 없는 날짜 {', '.join(bad_dates)} — 기간을 손대지 않았습니다 "
                          "(문자열로 대면 더 기형적인 값으로 덮어씁니다). 어느 쪽이 맞는지는 사람이 봅니다",
            })

        # 자리표시 기간(`(백필 중)`·`(대화 없음)`)이 굳어 있고 **메시지가 실제로 있으면**
        # 실제 범위로 낫게 한다. 날짜 모양이 아니라 아래 시작·끝 비교로는 영영 안 걸린다.
        # 메시지가 0건이면 손대지 않는다 — 그때 `(대화 없음)` 은 사실이다.
        if not m and oldest and newest and not bad_dates:
            ph = PERIOD_PLACEHOLDER_RE.search(text)
            if ph:
                was = text[ph.start():ph.end()]
                new_text = text[:ph.start()] + f"**기간**: {oldest} ~ {newest}" + text[ph.end():]
                if not dry:
                    write_lines(path, new_text.split("\n"), newline)
                changed.append({
                    "where": f"channels/{name}.md",
                    "what": f"자리표시 기간 {was.split(':', 1)[1].strip()} → {oldest} ~ {newest}",
                })
                text = new_text
                m = PERIOD_RE.search(text)

        period_start = period_end = None
        if m and not bad_dates:
            cur_start = m.group(1)
            # 끝날짜가 없는 단일 날짜 채널은 그 하나의 날짜가 시작이자 끝이다.
            # 아래 두 비교의 「지금 끝」은 이 값을 쓴다 — `m.group(2)` 만 보면(예전
            # 버그, Task 7 리뷰 2026-09-02 재현) 단일 날짜 채널에서 최신 메시지가
            # 늘어난 것을 절대 못 잡는다: 헤더가 `2026-07-01` 하나뿐인데 본문에
            # 6/30 과 8/20 메시지가 있으면, 시작만 6/30 으로 당기고 끝은 그대로
            # 7/1 에 남아 `2026-06-30 ~ 2026-07-01` 이 써진다 — 8/20 은 있는데
            # 봇은 그 뒤를 「없다」고 답하고, 다음 회차(07:00/17:00, 최대 10시간
            # 뒤)가 와야 8/20 으로 다시 고쳐진다. 그 사이 그 값이 봇 시스템
            # 프롬프트에 그대로 실린다 — 이 파일이 막으려는 바로 그 실패다.
            cur_end = m.group(2) or cur_start

            # 끝날짜는 **뒤로만 민다**(최신 메시지가 늘어날 뿐 줄어들 일이 없어서다).
            if newest and newest > cur_end:
                period_end = newest

            # 시작날짜는 **앞으로만 당긴다**(내려간다) — 백필이 더 오래된 메시지를
            # 채우면 md 안의 가장 오래된 메시지가 기록된 시작날짜보다 앞서게 되고,
            # 그때만 고친다. 반대 방향(시작을 지금보다 새것으로 올림)은 하지 않는다
            # — 그건 메시지가 사라졌다는 뜻이라 조용히 고칠 일이 아니라 사람이 볼
            # 일이다. 이 비대칭이 예전 주석의 걱정("시작일이 첫 메시지가 아니라
            # 채널 개설일인 채널이 여럿")도 함께 지킨다 — 개설일은 대개 첫
            # 메시지보다 이르므로 `오래된 메시지 < 기록된 시작` 이 성립하지 않아
            # 그런 채널은 애초에 안 건드려진다. (Task 7, 2026-09-02)
            if oldest and oldest < cur_start:
                period_start = oldest

        if m and not bad_dates and (period_start or period_end):
            # 기간 줄만은 update_header 를 거치지 않고 여기서 직접 쓴다 —
            # update_header 의 기간 치환(insert_messages.py)은 시작날짜를 늘 보존하도록
            # 짜여 있어(끝날짜만 바꾸는 용도였다), 시작날짜까지 바꾸려면 그 정규식도
            # 다시 타야 해서 같은 로직이 두 파일에 생긴다. index.md 의 표·소계·총계도
            # 같은 이유로 이 파일이 read_lines/write_lines 로 직접 쓴다(모듈 docstring).
            #
            # `start`·`end` 는 항상 `cur_start`/`cur_end` 로 떨어진다(바뀐 값이 없으면
            # 그 자리를 그대로 채운다) — 그래서 둘 중 하나만 바뀌어도 나머지 한쪽이
            # `None` 이 되어 사라지는 일이 없다. `period_start < cur_start ≤ cur_end`
            # 이고 `period_end > cur_end ≥ cur_start` 라서, 무엇이 바뀌었든 최종
            # `start < end` 가 항상 성립한다 — 「끝날짜가 없는 단일 날짜 채널에
            # `A ~ A` 를 써 버린다」는 걱정은 애초에 무언가 바뀌었을 때만 이 블록에
            # 들어오므로 구조적으로 안 생긴다.
            start = period_start or cur_start
            end = period_end or cur_end
            new_period = f"**기간**: {start} ~ {end}"
            new_text = text[:m.start()] + new_period + text[m.end():]
            if new_text != text:
                bits = []
                if period_start:
                    bits.append(f"기간 시작 {cur_start} → {period_start}")
                if period_end:
                    bits.append(f"기간 끝 {cur_end if m.group(2) else '(없음)'} → {period_end}")
                if not dry:
                    write_lines(path, new_text.split("\n"), newline)
                changed.append({"where": f"channels/{name}.md", "what": ", ".join(bits)})
                text = new_text

        try:
            note = update_header(path, None, counts[name], dry=dry)
        except ValueError as e:
            unresolved.append({
                "where": f"channels/{name}.md",
                "kind": "header_not_found",
                "detail": str(e),
            })
            continue
        if note != "이미 같음":
            changed.append({"where": f"channels/{name}.md", "what": note})

    return counts, changed, unresolved


# ══ ② ③ index.md ════════════════════════════════════════════════════════════

def _section_bounds(lines):
    """채널 목록 절의 [시작, 끝) 줄 범위. 못 찾으면 None.

    범위를 먼저 좁히는 것이 index.md 손질의 절반이다 — 파일 전체에 행 정규식을 걸면
    「읽는 법」의 `channels/{채널명}.md` 리터럴 자리표시와 팀 구성 표가 함께 걸린다.
    """
    start = None
    for i, l in enumerate(lines):
        if l.startswith(CHANNEL_SECTION):
            start = i
        elif start is not None and l.startswith("## "):
            return start, i
    return (start, len(lines)) if start is not None else None


def _table_rows(lines, lo, hi):
    """표의 **데이터 행** 인덱스 목록.

    헤더 행은 「구분선 바로 앞줄」로 판정한다 — 마크다운 표는 헤더 다음 줄이 반드시
    구분선이라 이 규칙은 열 이름에 안 기댄다. 열 이름으로 거르면 규칙이 사람마다
    달라져 같은 표를 47행으로도 48행으로도 세게 된다(2026-08-10 에 실제로 갈렸다).
    """
    seps = {i for i in range(lo, hi) if SEP_RE.match(lines[i])}
    heads = {i - 1 for i in seps}
    return [
        i for i in range(lo, hi)
        if lines[i].startswith("|") and i not in seps and i not in heads
    ]


def _parse_rows(lines, lo, hi):
    """(데이터 행 인덱스, 매치) 목록. 매치가 None 이면 정규식이 못 읽은 행이다."""
    return [(i, ROW_RE.match(lines[i])) for i in _table_rows(lines, lo, hi)]


def _assert_partition(lines, lo, hi):
    """절 안의 `|` 줄이 **빠짐없이** 「데이터 행 · 구분선 · 헤더 행」 중 하나로 분류되는지.

    아래 불변식들은 쓰기 전후를 **같은 파서**로 견주므로 파서가 행을 놓치면 양쪽 다 놓쳐
    아무것도 안 걸린다. 그런데 행을 놓치면 소계·총계가 조용히 작아진 채 **쓰인다** — 봇
    프롬프트에 실리는 숫자다. 그래서 파서와 독립인 확인이 하나 필요하다: 분류가 절 안의
    `|` 줄을 전부 덮는가. (2026-08-10 시험에서 파서에 고의로 버그를 넣었더니 나머지
    불변식이 전부 통과하고 파일이 쓰였다.)
    """
    bars = {i for i in range(lo, hi) if lines[i].startswith("|")}
    seps = {i for i in range(lo, hi) if SEP_RE.match(lines[i])}
    heads = {i - 1 for i in seps} & bars
    covered = set(_table_rows(lines, lo, hi)) | seps | heads
    if covered != bars:
        lost = sorted(bars - covered)
        raise Invariant(
            "표 분류가 `|` 줄을 다 덮지 못했습니다 — "
            + ", ".join(f"index.md:{i + 1}" for i in lost[:5])
        )


def _row_key(m, line):
    """불변식 대조용 행 식별자 — 이름(없으면 원문 첫 40자)."""
    if m is None:
        return ("?", line.strip()[:40])
    return ("n", (m.group("file") or m.group("plain") or "").strip())


def _check_invariants(old, new, lo, hi):
    """쓰기 직전 확인. 하나라도 깨지면 Invariant 를 던지고 **아무것도 쓰지 않는다**.

    고쳐도 되는 것은 숫자 칸뿐이다. 그래서 「줄 수 · 행 이름과 순서 · 🔒 가 붙은 행」 셋이
    쓰기 전후로 같아야 한다. 이 셋이 같으면 표의 뼈대는 그대로이고 숫자만 바뀐 것이다.
    """
    if len(new) != len(old):
        raise Invariant(f"줄 수가 달라졌습니다 ({len(old)} → {len(new)})")

    _assert_partition(new, lo, hi)

    def snap(lines):
        rows = _parse_rows(lines, lo, hi)
        keys = [_row_key(m, lines[i]) for i, m in rows]
        locks = {k for k, (_, m) in zip(keys, rows) if m and m.group("lock")}
        return keys, locks

    ok_keys, ok_locks = snap(old)
    new_keys, new_locks = snap(new)
    if new_keys != ok_keys:
        diff = next((f"{a} → {b}" for a, b in zip(ok_keys, new_keys) if a != b), "행 수가 다릅니다")
        raise Invariant(f"표의 행 이름·순서가 달라졌습니다 ({diff})")
    if new_locks != ok_locks:
        raise Invariant(f"🔒 가 붙은 행이 달라졌습니다 ({ok_locks ^ new_locks})")


def sync_index(counts: dict, dry: bool):
    changed, unresolved = [], []
    if not INDEX.exists():
        return changed, [{"where": "index.md", "kind": "missing", "detail": "index.md 가 없습니다"}]

    lines, newline = read_lines(INDEX)
    bounds = _section_bounds(lines)
    if not bounds:
        return changed, [{
            "where": "index.md", "kind": "section_not_found",
            "detail": f"'{CHANNEL_SECTION}' 절을 찾지 못했습니다",
        }]
    lo, hi = bounds
    _assert_partition(lines, lo, hi)

    before_rows = _parse_rows(lines, lo, hi)
    new = list(lines)
    seen = set()

    # ── 행의 숫자 칸 ──
    for i, m in before_rows:
        if m is None:
            unresolved.append({
                "where": f"index.md:{i + 1}", "kind": "unmatched",
                "detail": lines[i].strip()[:60],
            })
            continue

        name = m.group("file") or m.group("plain")
        name = name.strip()
        cur = int(m.group("count"))

        if m.group("file") is None:
            # 링크 없는 행 — md 가 생겼으면 링크를 달아야 하고, 없는데 건수가 0이 아니면 낡았다
            if name in counts:
                unresolved.append({
                    "where": f"index.md:{i + 1}", "kind": "should_link",
                    "detail": f"{name} — 채널 md 가 생겼는데 행에 링크가 없습니다",
                })
                seen.add(name)
            elif cur != 0:
                unresolved.append({
                    "where": f"index.md:{i + 1}", "kind": "stale_nonzero",
                    "detail": f"{name} — {cur}건으로 적혀 있는데 채널 md 가 없습니다",
                })
            continue

        seen.add(name)
        if name not in counts:
            unresolved.append({
                "where": f"index.md:{i + 1}", "kind": "broken_link",
                "detail": f"{name} — 링크가 가리키는 channels/{name}.md 가 없습니다",
            })
            continue

        want = counts[name]
        if cur != want:
            new[i] = lines[i][:m.start("count")] + str(want) + lines[i][m.end("count"):]
            changed.append({"where": f"index.md:{i + 1}", "what": f"{name} {cur} → {want}"})

    for name in sorted(set(counts) - seen):
        unresolved.append({
            "where": "index.md", "kind": "missing_row",
            "detail": f"{name} — 채널 md 는 있는데 표에 행이 없습니다 (성격 칸은 사람이 씁니다)",
        })

    # ── 섹션 소계 · 총계 ──
    n_rows = len(before_rows)
    n_lock = sum(1 for _, m in before_rows if m and m.group("lock"))
    # 정규식이 못 읽은 행. `n_rows` 에는 세어지는데 `n_lock` 에는 안 세어져서, 그냥 두면
    # **그 행이 공개로 잡힌다.** 공개/비공개를 모르는 채로 총계를 쓰면 틀린 값이 박히므로
    # 하나라도 있으면 총계 줄을 아예 안 고친다 (소계·헤더는 그대로 맞춘다 — 거기엔
    # 공개/비공개 갈림이 없다). 그 행은 아래 `unmatched` 로 이미 DM 에 뜬다.
    n_unknown = sum(1 for _, m in before_rows if m is None)

    row_idx = {j for j, _ in before_rows}
    cur_sub, tally = None, {}
    for i in range(lo, hi):
        if SUBHEAD_RE.match(new[i]):
            cur_sub = i
            tally[i] = 0
        elif cur_sub is not None and i in row_idx:
            tally[cur_sub] += 1
    for i, actual in tally.items():
        sm = SUBHEAD_RE.match(new[i])
        if int(sm.group(2)) != actual:
            label = sm.group(1).lstrip("# ").rstrip("( ")
            changed.append({"where": f"index.md:{i + 1}",
                            "what": f"{label} 소계 {sm.group(2)} → {actual}"})
            new[i] = f"{sm.group(1)}{actual}{sm.group(3)}"

    total_at = next((i for i in range(0, lo) if TOTAL_RE.match(new[i])), None)
    if total_at is None:
        unresolved.append({
            "where": "index.md", "kind": "total_not_found",
            "detail": "'> **채널**: 총 N개 (공개 A + 비공개 B)' 줄을 못 찾았습니다",
        })
    elif n_unknown:
        unresolved.append({
            "where": f"index.md:{total_at + 1}", "kind": "total_held",
            "detail": f"못 읽는 행 {n_unknown}건이 있어 총계 줄을 손대지 않았습니다 — "
                      "그 행의 공개/비공개를 모르는 채로 쓰면 틀린 값이 박힙니다",
        })
    else:
        tm = TOTAL_RE.match(new[total_at])
        if (int(tm.group(2)), int(tm.group(4)), int(tm.group(6))) != (n_rows, n_rows - n_lock, n_lock):
            changed.append({
                "where": f"index.md:{total_at + 1}",
                "what": f"총계 {tm.group(2)}({tm.group(4)}+{tm.group(6)}) → "
                        f"{n_rows}({n_rows - n_lock}+{n_lock})",
            })
            new[total_at] = (f"{tm.group(1)}{n_rows}{tm.group(3)}{n_rows - n_lock}"
                             f"{tm.group(5)}{n_lock}{tm.group(7)}")

    # ── 꼬리말의 최종 동기화 시각 ──
    # 「지금」이 아니라 `.sync-state.json` 의 last_sync 다. 이 파일이 말하는 것은
    # 스크립트를 돌린 시각이 아니라 아카이브가 언제까지 슬랙을 따라잡았느냐다.
    stamp = _last_sync_stamp()
    if stamp:
        for i, l in enumerate(new):
            fm = FOOTER_RE.search(l)
            if fm and fm.group(2) != stamp:
                new[i] = l[:fm.start(2)] + stamp + l[fm.end(2):]
                changed.append({"where": f"index.md:{i + 1}",
                                "what": f"최종 동기화 {fm.group(2)} → {stamp}"})
                break

    # ── 쓰기 불변식 ──
    _check_invariants(lines, new, lo, hi)

    if changed and not dry:
        write_lines(INDEX, new, newline)
    return changed, unresolved


class Invariant(Exception):
    """쓰기 직전 확인에 걸렸다 — 아무것도 쓰지 않는다."""


def _last_sync_stamp():
    try:
        raw = json.loads(STATE.read_bytes().decode("utf-8")).get("last_sync")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


# ══ main ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="아카이브 파생값 재계산")
    ap.add_argument("--all", action="store_true", help="채널 헤더 + index.md 전부")
    ap.add_argument("--dry-run", action="store_true", help="계산만 하고 쓰지 않는다")
    ap.add_argument("--check", action="store_true", help="--dry-run 과 같다")
    ap.add_argument("--json", action="store_true", help="마지막 줄에 결과 JSON 한 줄")
    args = ap.parse_args()

    if not args.all:
        ap.error("--all 이 필요합니다")
    dry = args.dry_run or args.check

    if not CHANNELS.is_dir():
        print(f"✗ 채널 디렉터리가 없습니다: {CHANNELS}", file=sys.stderr)
        return 1

    counts, changed, unresolved = sync_channel_headers(dry)
    try:
        ic, iu = sync_index(counts, dry)
    except Invariant as e:
        print(f"✗ 쓰기 불변식 위반 — index.md 를 손대지 않았습니다: {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "invariant": str(e),
                              "changed": changed, "unresolved": unresolved}, ensure_ascii=False))
        return 1
    changed += ic
    unresolved += iu

    if not args.json:
        tag = "[dry] " if dry else ""
        if changed:
            print(f"{tag}맞춘 곳 {len(changed)}건")
            for c in changed:
                print(f"  · {c['where']} — {c['what']}")
        else:
            print(f"{tag}맞출 것 없음 (채널 {len(counts)}개)")
        if unresolved:
            print(f"\n기계가 못 고친 것 {len(unresolved)}건 — 사람이 봐야 합니다")
            for u in unresolved:
                print(f"  ⚠ {u['where']} [{u['kind']}] {u['detail']}")
    else:
        print(json.dumps({"ok": True, "dry": dry, "channels": len(counts),
                          "changed": changed, "unresolved": unresolved}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
