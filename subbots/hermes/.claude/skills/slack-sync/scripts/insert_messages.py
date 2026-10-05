#!/usr/bin/env python3
"""
채널 md 파일의 올바른 위치에 새 메시지 블록을 삽입한다.

아카이브 md는 최신이 위이므로, 새 메시지는 해당 월(`## YYYY-MM`) 헤딩 바로 아래에 들어간다.
해당 월 헤딩이 없으면 메타데이터 다음 첫 `---` 바로 아래에 헤딩과 함께 만든다.

사용:
  python insert_messages.py --file <채널md> --month 2026-08 --content-file <새블록md>
  python insert_messages.py --file <채널md> --month 2026-08 --content "본문..."
  python insert_messages.py --file <채널md> --month 2026-08 --in-order --content-file <새블록md>
  python insert_messages.py --file <채널md> --update-header --period-end 2026-08-05 --count 34
  python insert_messages.py --file <채널md> --append-thread '**2026-08-04 13:08 · 홍길동**' --content-file <답글줄들>

`--append-thread` 는 나중에 달린 스레드 답글(정정 등)을 **이미 저장된 부모 블록 안에** 넣는다.
같은 답글이 이미 있으면 건너뛰므로 매일 돌려도 쌓이지 않는다.
부모도 중복 판정도 **작성자 이름을 안 보고 시각으로** 맞춘다 — 슬랙 표시 이름은 바뀌는데
md 에는 기록 당시 이름이 박혀 있어서, 이름으로 맞추면 정정이 안 붙거나 답글이 날마다 쌓인다.

── `--report` : **무엇을 실제로 써 넣었나** (`--append-thread` · 월 삽입 전용) ──

평소 출력(`OK 스레드 덧붙임 (…)`)은 **어디에** 넣었는지만 말한다. 넣으려던 것 다섯 중
하나만 새것이어도 그 문구는 똑같아서, 부르는 쪽이 **제안한 개수를 그대로** 세게 된다.
`--report` 를 주면 맨 끝에 한 줄이 더 나온다:

  REPORT {"mode": "append-thread", "written": ["> **\\u2514 …** — …", …], "botAdded": 0}

`written` 은 **이번에 파일에 들어간 것만** 이고, 답글은 여러 줄이어도 **한 덩이**로 들어
있다(`group_replies` 가 묶는 단위). 기존 출력은 그대로라 이 옵션을 안 쓰는 곳은 영향이 없다.
`src/ingest/slack-archive.js` 의 `writtenFrom` 이 이 줄을 읽는다.

── 슬랙에서 고쳐지거나 지워진 것을 따라가는 모드 (사람이 지시할 때만) ──

  --replace-body   --header '**…**'  --expect-file <현재본문> --content-file <새본문>
  --delete-message --header '**…**'  --expect-file <현재본문>
  --replace-reply  --header '**…**'  --reply-key '2026-08-04 13:10 · 박민수'
                                     --expect-file <현재줄>   --content-file <새줄>
  --delete-reply   --header '**…**'  --reply-key '…'          --expect-file <현재줄>

위 네 모드는 **이미 저장된 내용을 고치거나 지운다.** 삽입보다 파괴적이라 안전장치가 붙는다.

  · `--expect-file` 로 **지금 md 에 있는 내용과 대조**한 뒤에만 손댄다. 다르면 아무것도
    고치지 않고 멈춘다. 헤더만으로 블록을 고르면 안 되기 때문이다 — 같은 분·같은 사람
    헤더가 아카이브에 118건(57곳·파일 20개, 2026-09-22 실측) 있다. 후보가 2개 이상이어도 멈춘다.
  · `--dry-run` 으로 바뀔 자리를 먼저 볼 수 있다.
  · tmp 에 쓰고 바꿔 끼운다. 중간에 죽어도 반쪽짜리 파일이 안 남는다.
  · **원본 개행(CRLF/LF)을 그대로 유지한다.** 커밋에는 영향이 없다 — 이 저장소는
    `core.autocrlf` 가 켜져 있어 git 이 저장할 때 LF 로 맞춘다(실측 확인). 그래도 유지하는
    이유는 파일을 통째로 다시 쓰지 않기 위해서다. 줄 끝이 섞여 있으면 줄끝 앵커가 있는
    정규식이 **에러 없이** 안 맞는 일이 생긴다 (2026-08-05 감지 코드에서 실제로 한 번 났다).

이 모드는 **슬랙 원문 자체가 바뀐 것을 따라갈 때만** 쓴다. 사람 판단으로 내용을 고치는 것은
여전히 슬랙 스레드에 `[정정]` 댓글이고, 아카이브 원문은 건드리지 않는다.

종료코드: 0 성공 / 1 실패
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "_shared"))
import argparse
import json
import re
import sys
from pathlib import Path

# Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지지 않도록
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

MONTH_RE = re.compile(r"^##\s+(\d{4}-\d{2})\s*$")
# 하단 섹션 — 이 앞까지가 메시지 본문 영역
TAIL_RE = re.compile(r"^##\s+참여 기록")

# archive.js 의 splitMessages 가 블록을 자르는 자리와 **같은 모양**이다.
# --append-thread 가 답글을 넣는 자리는 이 경계보다 **늦으면 안 된다** — 늦으면 봇이 보는
# 블록이 갈려 답글이 남의 발언에 붙는다. 이르면 괜찮다(어차피 같은 블록 안이다).
MSG_HEADER_RE = re.compile(r"^\*\*\d{4}-\d{2}-\d{2}[^*]*\*\*\s*$")

# 봇 답변 자리에 남기는 표식. **정본은 `src/slack-live.js` 의 `BOT_ANSWER_MARK` 이고**
# 여기 있는 것은 그 값을 파이썬에서 쓰기 위한 짝이다. 두 언어가 같은 문자열을 쓰는지는
# `scripts/check-archive-contract.js` [6/8] 이 대조한다 — 값을 한쪽만 고치면 거기서 빨개진다.
#
# **베낀 자리를 하나로 줄인 것이 요점이다.** 2026-09-03 까지 이 표식은 네 군데에 각각
# 적혀 있었고(여기 두 정규식 · `_bump_bot_tail` 의 두 문자열), 게다가 JS 쪽
# `slack-archive.js` 의 `BODY_TAIL` 은 `(Hermes` 라고 적혀 있어 **한 번도 안 맞았다** —
# 그래서 표식 줄이 본문에 빨려 들어간 블록이 실물에 7건 있었다(2026-09-03 실측).
# 어긋나도 에러가 안 나고, 그 블록이 슬랙에서 수정되면 `apply_edits.py --apply` 가
# 표식 줄을 본문의 일부로 보고 함께 지운다.
BOT_ANSWER_MARK = "(봇 답변 — 미수록)"
_MARK = re.escape(BOT_ANSWER_MARK)
BOT_TAIL_RE = re.compile(r"^>\s*💬\s*스레드\s*\d+건\s*" + _MARK)
REPLY_HEAD_RE = re.compile(r"^>\s*\*\*└\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^*]+)\*\*")
# 답글의 **시작 줄**을 날짜 없이도 잡는다. 경계를 볼 때만 쓴다 — 초기 수기 추출분에
# 날짜 없는 답글(`> **└ WHK** — …`)이 있어서, 날짜를 요구하면 그 줄이 앞 답글의
# 이어지는 줄로 빨려 들어가 함께 지워진다.
REPLY_HEAD_LOOSE_RE = re.compile(r"^>\s*\*\*└")
# 답글 줄을 '시각' 과 '본문' 으로 나눠 잡는다 — 이름이 바뀌어도 같은 답글임을 알아보려고.
REPLY_LINE_RE = re.compile(
    r"^>\s*\*\*└\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})[^*]*\*\*\s*—?\s*(.*)$"
)

# 교체·삭제 모드용 — 블록 경계는 MSG_HEADER_RE 보다 **느슨하게** 본다.
# 아카이브에는 `**2026-07-30 07:30 · 최지훈** — 7/29 일일업무일지 · 💬 스레드 1건(...)` 처럼
# 한 줄로 줄여 적은 메시지가 38건 있다. 줄끝 앵커가 있는 MSG_HEADER_RE 는 이걸 헤더로 못 봐서
# 앞 블록에 흡수시키고, 그 상태로 블록을 지우면 **이웃 메시지까지 함께 지워진다.**
BLOCK_START_RE = re.compile(r"^\*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2} · ")
# 본문이 끝나는 자리. renderMessage 가 쌓는 순서(본문 → 첨부 → 스레드)와 짝이다.
# `>` 로 시작하는 줄을 전부 경계로 보면 안 된다 — 슬랙 본문에도 인용문이 있다.
# 표식은 위 `BOT_ANSWER_MARK` 에서 만든다 (JS 쪽 `slack-archive.js` 의 `BODY_TAIL` 도 같다).
BODY_TAIL_RE = re.compile(r"^(?:`[^`]*`\s*$|📎 첨부: |> 💬 |> \*\*└|💬 스레드 \d+건 " + _MARK + ")")
# 스레드 머리줄. 꼬리말이 붙는 자리가 **둘**이다 — 볼드 안(`(3) — 제3자 사업성평가**`, 15건)과
# 볼드 밖(`(4)** — 답글이 …`, 1건). 줄끝 앵커나 `)**` 붙임을 요구하면 132건 중 16건을 못 본다.
# 꼬리말은 그대로 두고 숫자만 고칠 수 있게 나눠 잡는다.
THREAD_HEAD_ANY_RE = re.compile(r"^(>\s*💬\s*\*\*스레드\s*\()(\d+)(\)[^*]*\*\*)(.*)$")
# 블록 헤더의 'YYYY-MM-DD HH:MM' — --in-order 가 시간을 견줄 때 쓴다
BLOCK_KEY_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")


def read_lines(path: Path):
    """줄 목록과 **원본 개행**을 함께 돌려준다.

    `read_text` 는 개행을 `\\n` 으로 바꿔 읽고 `write_text` 는 `os.linesep` 으로 바꿔 쓴다.
    그래서 윈도우에서 돌리면 LF 파일이 통째로 CRLF 로 바뀐다. 커밋에는 안 드러나지만
    (`core.autocrlf` 가 git 쪽에서 맞춰 준다) 파일을 전부 다시 쓰는 것은 사실이고,
    줄 끝이 섞이면 줄끝 앵커가 있는 정규식이 **에러 없이** 안 맞는다. 그래서 그대로 둔다.
    """
    raw = path.read_bytes().decode("utf-8")
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    newline = "\r\n" if crlf >= lf else "\n"
    return raw.replace("\r\n", "\n").split("\n"), newline


def write_lines(path: Path, lines, newline: str):
    """tmp 에 쓰고 바꿔 끼운다 — 교체·삭제는 중간에 죽으면 반쪽짜리 파일이 남는다."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(newline.join(lines).encode("utf-8"))
    tmp.replace(path)


def find_body_start(lines):
    """메시지 본문이 시작되기 직전 '---' 의 인덱스를 돌려준다.

    채널에 따라 메타 블록과 첫 월 헤딩 사이에 **상단 요약 섹션**이 있다
    (`## 자금 구조 요약 …` 처럼 월 헤딩이 아닌 '## '). 그 경우 본문 시작은
    요약 섹션을 닫는 '---' 뒤다. 여기를 잘못 잡으면 새 월 헤딩이 요약 위에
    삽입되고, 요약이 첫 메시지 헤더 **아래**로 밀려 archive.js 의 splitMessages
    에 사람 발언 본문으로 딸려 들어간다 (2026-08-02 #사업장나·#사업장거 실제 사고).
    """
    first = None
    seen_meta = False
    for i, line in enumerate(lines):
        if line.startswith(">"):
            seen_meta = True
        if seen_meta and line.strip() == "---":
            first = i
            break
    if first is None:
        # 메타 블록이 없으면 첫 '---'
        for i, line in enumerate(lines):
            if line.strip() == "---":
                first = i
                break
    if first is None:
        raise ValueError("본문 시작 구분선('---')을 찾지 못했습니다")

    # 첫 '---' 다음에 나오는 첫 '## ' 헤딩이 월 헤딩이면 요약 섹션이 없는 것.
    # `## 참여 기록` 도 마찬가지다 — 그건 **하단** 섹션이라 요약이 아니다. 이걸 빼면
    # 월 헤딩이 없는 채널에서 참여 기록을 상단 요약으로 잘못 읽어 본문 영역이 통째로
    # 사라진다. **지금은 밑줄로 시작하는 채널 하나뿐이다** (2026-08-31 확인) — 전에
    # 함께 적어 두었던 `z_비공개마_옛이름` 는 채널이 아카이브 범위 밖이 되어 md 를 지웠고(2026-08-31),
    # `사업장머` 는 그 뒤 월 헤딩이 생겼다. **개수가 줄었다고 이 처리를 걷어내지 말 것** —
    # 월 헤딩 없는 채널은 새로 만들어질 때마다 다시 생긴다.
    summary_at = None
    for j in range(first + 1, len(lines)):
        if lines[j].startswith("## "):
            if MONTH_RE.match(lines[j]) or TAIL_RE.match(lines[j]):
                return first
            summary_at = j
            break
    if summary_at is None:
        return first

    # 요약 섹션을 닫는 '---' 를 찾는다. 없으면 위치를 추측하지 않고 멈춘다
    for k in range(summary_at + 1, len(lines)):
        if lines[k].strip() == "---":
            return k
    raise ValueError(
        f"상단 요약 섹션('{lines[summary_at].strip()}')을 닫는 '---' 이 없습니다. "
        "요약 섹션 끝에 '---' 을 넣은 뒤 다시 실행하세요."
    )


def find_body_end(lines, body_start):
    """참여 기록 섹션(또는 그 앞 '---')의 인덱스. 없으면 파일 끝."""
    for i in range(body_start + 1, len(lines)):
        if TAIL_RE.match(lines[i]):
            # 바로 앞의 '---'까지 되돌아간다
            j = i - 1
            while j > body_start and lines[j].strip() == "":
                j -= 1
            if lines[j].strip() == "---":
                return j
            return i
    return len(lines)


def insert_block(path: Path, month: str, block: str, at_end: bool = False,
                 in_order: bool = False, dry: bool = False, report: dict | None = None) -> str:
    """새 블록을 해당 월 절에 넣는다.

    기본은 **맨 위**다 — 평상시 넣는 것은 새 메시지이고 아카이브는 최신이 위다.

    `at_end=True` 는 **초기 구간 채우기**(SKILL.md 3.5) 전용이다. 그때 넣는 것은
    이미 있는 것보다 **오래된** 메시지라 위에 붙이면 시간 순서가 뒤집힌다.
    같은 이유로 월 헤딩이 없을 때도 본문 **맨 뒤**에 만든다 — 없던 달을 채운다는 것은
    그 달이 기존 어느 달보다 오래됐다는 뜻이다.

    `in_order=True` 는 **맨 위도 맨 뒤도 아닌** 경우다. 넣는 것이 지금 이 채널의 가장 새
    메시지라는 보장이 없을 때 쓴다 — 예를 들어 자동 반영이 며칠 전 봇 글에 달린 정정을
    담을 자리표시 블록을 만들 때. 헤더의 시각을 읽어 기존 블록들 사이 제자리에 넣는다.
    """
    lines, newline = read_lines(path)

    body_start = find_body_start(lines)
    body_end = find_body_end(lines, body_start)

    # **이미 들어 있는 블록은 건너뛴다.**
    #
    # 자동 반영은 ④ 삽입 → ④-b 답글 → ④-c 정정 순으로 가는데, ④ 가 md 를 쓴 뒤 ④-b 에서
    # 실패하면 `last_ts` 를 못 밀고 돌아간다(부모를 못 찾는 경우가 대표적이다). 그런데
    # index.js 는 그래도 커밋·push 하므로, 다음 회차가 **같은 메시지를 다시 가져와 또 넣는다.**
    # 답글 쪽(`--append-thread`)에는 이 방어가 있었는데 이쪽에는 없었다 (2026-08-10).
    #
    # 판정은 **줄 단위 접두 일치**다. 헤더만 보면 같은 분·같은 사람의 다른 메시지를 잃고
    # (아카이브에 그런 헤더가 118건(57곳·파일 20개, 2026-09-22 실측) 있다), 텍스트 부분일치로 보면 새 본문이 기존 본문의
    # 앞부분과 같을 때 통째로 사라진다("네 알겠습니다" vs "네 알겠습니다 확인 후 회신드리겠습니다").
    # 접두로 보는 것은 md 의 블록이 나중에 **자라기** 때문이다 — ④-b 가 스레드 답글을,
    # ④-c 가 정정을 같은 블록 안에 덧붙이므로 기존 블록이 넣으려는 블록보다 길다.
    def split_blocks(src):
        out = []
        for raw in src:
            # **경계는 좁게 본다** — 월 헤딩(`## YYYY-MM`)과 최상위 메시지 헤더만.
            # 전에는 `MSG_HEADER_RE`(줄 전체가 `**날짜…**`)와 `## ` 로 시작하는 아무 줄이나
            # 경계로 봤는데, 그러면 본문 안의 헤더 흉내 줄(`**2026-08-15 회의록**`)이나
            # 사람이 보라고 넣은 중간 절(`## 호텔 운영실적 추이`)에서 묶음이 갈린다.
            # 갈린 묶음을 다시 이어 붙일 때 사이에 **없던 빈 줄이 낀다.**
            # 좁혀도 잃는 것이 없는 것은 실측으로 확인했다 (2026-08-29 전 채널):
            # 정상 헤더 1,604건은 전부 `BLOCK_START_RE` 에도 걸리고, `MSG_HEADER_RE` 에만
            # 걸리는 줄은 0건이다. 한 줄로 줄여 적은 38건은 `BLOCK_START_RE` 가 잡는다.
            if BLOCK_START_RE.match(raw) or MONTH_RE.match(raw) or not out:
                out.append([raw])
            else:
                out[-1].append(raw)
        blocks = []
        for g in out:
            while g and not g[-1].strip():
                g.pop()
            if g:
                blocks.append(g)
        return blocks

    existing_blocks = split_blocks(lines[body_start:body_end])
    incoming_blocks = split_blocks(block.rstrip("\n").split("\n"))
    fresh_blocks = [
        g for g in incoming_blocks
        if not any(e[:len(g)] == g for e in existing_blocks)
    ]
    # **무엇을 썼는지**를 부르는 쪽에 돌려준다 (`--report`). 아래 `where` 문구는 「어디에」만
    # 말하고 「무엇을」은 말하지 않아서, 부르는 쪽이 제안한 개수를 그대로 세게 된다.
    if report is not None:
        report["mode"] = "insert"
        report["written"] = ["\n".join(g) for g in fresh_blocks]
    if not fresh_blocks:
        return "이미 반영됨"

    # 하나도 안 걸렀으면 **원문 그대로** 쓴다 — 다시 이어 붙이면 본문에 헤더를 흉내 낸
    # 인용 줄이 있을 때 없던 빈 줄이 끼어든다.
    block_lines = (
        block.rstrip("\n").split("\n")
        if len(fresh_blocks) == len(incoming_blocks)
        else "\n\n".join("\n".join(g) for g in fresh_blocks).split("\n")
    )

    def back_over_blanks(i):
        while i > body_start + 1 and lines[i - 1].strip() == "":
            i -= 1
        return i

    def section_end(after):
        """`after` 줄이 여는 절이 끝나는 자리.

        다음 '## ' 헤딩이거나 '---' 이다. `find_body_end` 로는 부족하다 — 그건
        '## 참여 기록' 을 찾아 되짚는데, 하단 '---' 과 참여 기록 사이에 메모
        (「미수집 구간」 줄 등)가 있으면 그 메모 뒤를 가리켜 블록이 구분선 아래로
        떨어진다. 이 아카이브에서 '---' 은 늘 하단 구분선이고 메시지 본문 안에는
        없다(2026-08-07 전 채널 확인).
        """
        for i in range(after + 1, body_end):
            if lines[i].startswith("## ") or lines[i].strip() == "---":
                return i
        return body_end

    def body_content_end():
        """본문 전체가 끝나는 자리 — 마지막 달의 뒤. 월 헤딩은 건너뛴다."""
        for i in range(body_start + 1, body_end):
            if lines[i].strip() == "---":
                return i
        return body_end

    def new_key():
        """넣을 블록의 'YYYY-MM-DD HH:MM'. --in-order 는 이게 있어야 자리를 정한다."""
        m = BLOCK_KEY_RE.match(block_lines[0])
        if not m:
            raise ValueError(f"--in-order 는 첫 줄이 메시지 헤더여야 합니다: {block_lines[0][:60]}")
        return m.group(1)

    def slot_in_month(after):
        """그 달 안에서 **처음으로 나보다 오래된** 블록 헤더의 인덱스. 없으면 None.

        아카이브는 최신이 위라 위에서부터 훑다가 나보다 오래된 것을 만나는 자리가 내 자리다.
        """
        key = new_key()
        for i in range(after + 1, section_end(after)):
            m = BLOCK_KEY_RE.match(lines[i])
            if m and m.group(1) < key:
                return i
        return None

    def month_headings():
        """본문 영역의 (인덱스, 'YYYY-MM') 목록 — 위에서부터, 즉 최신부터."""
        out = []
        for i in range(body_start + 1, body_end):
            m = MONTH_RE.match(lines[i])
            if m:
                out.append((i, m.group(1)))
        return out

    # 본문 영역에서 해당 월 헤딩 찾기
    target = None
    for i in range(body_start + 1, body_end):
        m = MONTH_RE.match(lines[i])
        if m and m.group(1) == month:
            target = i
            break

    if target is not None and in_order:
        at = slot_in_month(target)
        if at is None:
            # 그 달에서 가장 오래된 메시지 — 절의 맨 뒤
            insert_at = back_over_blanks(section_end(target))
            new_lines = lines[:insert_at] + [""] + block_lines + lines[insert_at:]
            where = f"'## {month}' 끝 (시간 순서)"
        else:
            new_lines = lines[:at] + block_lines + [""] + lines[at:]
            where = f"'## {month}' 안 {lines[at][:20]}… 앞 (시간 순서)"
    elif target is None and in_order:
        # 없던 달을 만든다. 월 헤딩도 최신이 위이므로 나보다 오래된 첫 달 앞에 낀다.
        older = next((i for i, mo in month_headings() if mo < month), None)
        if older is None:
            insert_at = back_over_blanks(body_content_end())
            new_lines = lines[:insert_at] + ["", f"## {month}", ""] + block_lines + lines[insert_at:]
            where = f"새 '## {month}' 생성 (본문 맨 뒤)"
        else:
            new_lines = lines[:older] + [f"## {month}", ""] + block_lines + [""] + lines[older:]
            where = f"새 '## {month}' 생성 (기존 달 사이)"
    elif target is not None and at_end:
        # 그 달의 마지막 메시지 뒤
        insert_at = back_over_blanks(section_end(target))
        new_lines = (
            lines[:insert_at] + [""] + block_lines + lines[insert_at:]
        )
        where = f"기존 '## {month}' 끝"
    elif target is not None:
        # 헤딩 바로 아래에 삽입
        insert_at = target + 1
        while insert_at < body_end and lines[insert_at].strip() == "":
            insert_at += 1
        new_lines = (
            lines[:insert_at] + block_lines + [""] + lines[insert_at:]
        )
        where = f"기존 '## {month}' 아래"
    elif at_end:
        # 없던 달을 본문 맨 뒤에 만든다
        insert_at = back_over_blanks(body_content_end())
        new_lines = (
            lines[:insert_at]
            + ["", f"## {month}", ""]
            + block_lines
            + lines[insert_at:]
        )
        where = f"새 '## {month}' 생성 (본문 맨 뒤)"
    else:
        # 새 월 헤딩을 본문 맨 앞에 만든다
        insert_at = body_start + 1
        while insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        new_lines = (
            lines[:insert_at]
            + [f"## {month}", ""]
            + block_lines
            + [""]
            + lines[insert_at:]
        )
        where = f"새 '## {month}' 생성"

    if dry:
        return f"[dry] {where}"
    write_lines(path, new_lines, newline)
    return where


class AmbiguousParent(ValueError):
    """같은 분에 부모 후보가 여럿이라 하나를 못 고른다.

    `ValueError` 를 물려받으므로 부르는 쪽의 `except Exception` 은 그대로 돈다 —
    바깥에서 보이는 것은 종전과 같은 `ERROR: …` 한 줄이다.

    후보 줄 번호를 들고 있는 이유는 `append_thread` 가 **고르지 않고도 답할 수 있는
    질문** 하나를 더 묻기 위해서다 — 「붙이려는 답글이 후보 전부를 통틀어 이미 다
    들어 있나」. 그 답이 예면 할 일이 없으므로 멈출 이유도 없다.
    """

    def __init__(self, message, candidates, minute):
        super().__init__(message)
        self.candidates = candidates
        self.minute = minute


def find_parent(lines, header: str, body_start: int, body_end: int) -> int:
    """부모 메시지 헤더 줄의 인덱스. **이름이 아니라 시각으로 찾는다.**

    예전에는 헤더 줄 전체가 문자열로 같은지 봤다. 그런데 헤더의 이름은 md 에 **기록 당시**
    이름이 박혀 있고, 부르는 쪽은 **지금 슬랙** 이름으로 헤더를 만든다. 그래서 누가 표시
    이름을 바꾸면 그 사람의 옛 메시지에 달린 정정이 영영 안 붙었다.

    또 하나: 예전에는 같은 헤더가 여럿일 때 **첫 번째**를 골랐다. 아카이브에 같은 분 헤더가
    104곳(223건·파일 27개), 그중 **헤더 줄이 글자까지 같은 것**이 57곳(118건·파일 20개)
    있으므로(2026-09-22 실측), 정정이 엉뚱한 메시지에 조용히 붙을 수 있었다. 이제는 못
    고르면 `AmbiguousParent` 로 멈춘다.

    **멈춘 뒤에 한 번 더 묻는 자리가 있다** — `append_thread` 는 이 예외를 받아 「붙일 것이
    후보 전부를 통틀어 이미 다 있나」를 본다. 고르지 않고도 답할 수 있는 질문이라, 예면
    할 일이 없으므로 멈출 이유도 없다. 여기서는 고르는 일만 한다.
    """
    m = re.match(r"^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", header.strip())
    if not m:
        raise ValueError(f"부모 헤더 모양이 아닙니다: {header}")
    minute = m.group(1)

    hits = [
        i for i in range(body_start + 1, body_end)
        if lines[i].startswith(f"**{minute}")
    ]
    if not hits:
        raise ValueError(f"부모 메시지를 찾지 못했습니다: {header}")
    if len(hits) == 1:
        return hits[0]

    # 같은 분에 여러 건 — 헤더 줄이 통째로 같은 것으로 좁혀 본다
    exact = [i for i in hits if lines[i].strip() == header.strip()]
    if len(exact) == 1:
        return exact[0]
    raise AmbiguousParent(
        f"같은 시각({minute})에 메시지가 {len(hits)}건이라 어디에 붙일지 고를 수 없습니다: {header}\n"
        "  추측해서 붙이면 정정이 엉뚱한 발언에 달립니다. 손으로 확인해 주세요.",
        hits, minute,
    )


def _bump_bot_tail(lines, target, block_end, insert_at, n):
    """`> 💬 스레드 N건 (봇 답변 — 미수록)` 의 건수를 n 만큼 올린다. 없으면 만든다.

    봇 답변 **본문**은 넣지 않는다 — Hermes 답변은 아카이브를 읽어 만든 2차 가공물이라
    넣으면 다음번에 봇이 자기 답을 원본 근거로 삼고, 다른 봇의 글도 같은 이유로 뺀다
    (WHK 결정 2026-08-12). 몇 건이 있었다는 사실만 남긴다.
    """
    for i in range(target + 1, block_end):
        m = BOT_TAIL_RE.match(lines[i])
        if m:
            prev = int(re.search(r"(\d+)건", lines[i]).group(1))
            lines[i] = f"> 💬 스레드 {prev + n}건 {BOT_ANSWER_MARK}"
            return None
    return f"> 💬 스레드 {n}건 {BOT_ANSWER_MARK}"


def group_replies(block: str):
    """덧붙일 줄들을 **답글 단위**로 묶는다 → `[[줄, 줄, …], …]`

    슬랙 답글은 여러 줄일 수 있다 — `renderReply` 가 본문을 그대로 담으므로 줄바꿈이 있는
    `[정정]` 은 여러 줄로 온다. 줄 단위로 세면 두 가지가 한꺼번에 깨진다.
      · 한 답글이 N건으로 잡혀 스레드 건수가 부푼다
      · 이어지는 줄은 아래 중복 판정 정규식에 안 걸려 **자동 반영이 돌 때마다 다시 쌓인다**
    그래서 머리줄(`> **└ …**`)이 나올 때마다 새 묶음을 열고 나머지는 직전 묶음에 붙인다.
    """
    groups = []
    for raw in block.rstrip("\n").split("\n"):
        if not raw.strip():
            continue
        if REPLY_HEAD_RE.match(raw) or not groups:
            groups.append([raw])
        else:
            # 이어지는 줄도 인용 안에 둔다 — `>` 가 없으면 md 에서 인용 블록이 거기서 끊긴다
            groups[-1].append(raw if raw.lstrip().startswith(">") else f"> {raw}")
    return groups


def _reply_scan_end(lines, target: int, body_end: int) -> int:
    """`target` 블록의 답글을 훑을 끝 경계. 최상위 헤더나 월 헤딩까지."""
    for i in range(target + 1, body_end):
        if MSG_HEADER_RE.match(lines[i]) or lines[i].startswith("## "):
            return i
    return body_end


def _collect_existing(lines, target: int, scan_end: int):
    """그 구간에 이미 있는 답글 줄들. `append_thread` 의 짝짓기가 쓰는 모양."""
    out = []
    for i in range(target, scan_end):
        p = REPLY_LINE_RE.match(lines[i])
        if not p:
            continue
        h = REPLY_HEAD_RE.match(lines[i])
        out.append({
            "idx": i,
            "time": p.group(1),
            "head": h.group(1).strip() if h else None,   # 'YYYY-MM-DD HH:MM · 이름'
            "body": p.group(2).strip(),
            "used": False,
        })
    return out


def _all_replies_present(lines, candidates, body_end: int, groups) -> bool:
    """부모 후보 **전부**를 통틀어, 붙이려는 답글이 이미 다 들어 있나.

    **판정은 완전일치(시각+본문)만 쓴다.** `append_thread` 의 ② 「같은 `시각·이름`
    자리가 남아 있으면 그 줄이 이 답글이다」 구제를 여기로 가져오면 안 된다 — 한 블록
    안에서는 「같은 시각·같은 이름 = 같은 답글의 표기 흔들림」이 서지만, 블록 여럿에
    걸치면 **같은 사람이 같은 분에 다른 스레드에 단 무관한 답글**이 그 조건을 만족해
    진짜 새 답글을 「이미 있음」으로 삼킨다. 그 유실은 조용하다.

    ①만 쓰면 표기가 흔들린 옛 답글은 「없음」으로 남아 종전처럼 멈추는데, 그쪽은
    **시끄럽게** 실패하는 방향이라 안전하다.
    """
    pool = []
    seen = set()
    for t in candidates:
        for e in _collect_existing(lines, t, _reply_scan_end(lines, t, body_end)):
            if e["idx"] in seen:
                continue
            seen.add(e["idx"])
            pool.append(e)

    for g in groups:
        p = REPLY_LINE_RE.match(g[0])
        if not p:
            return False
        time, body = p.group(1), p.group(2).strip()
        for e in pool:
            if not e["used"] and e["time"] == time and e["body"] == body:
                e["used"] = True
                break
        else:
            return False
    return True


def append_thread(path: Path, header: str, block: str, bot_added: int = 0,
                  dry: bool = False, report: dict | None = None) -> str:
    """이미 저장된 메시지 블록 **안에** 새 스레드 답글 줄을 덧붙인다.

    나중에 달린 정정 답글(`[정정] …`)이 원문과 **같은 블록**에 있어야 한다. archive.js 의
    searchArchive 는 매칭된 메시지 블록을 통째로 돌려주므로, 원문에 걸린 검색이 정정까지
    함께 보여줘야 봇이 어느 쪽이 최신인지 안다. 별도 블록으로 떼면 원문만 인용된다.

    block 은 `> **└ YYYY-MM-DD HH:MM · 이름** — …` 줄들이다.
    """
    lines, newline = read_lines(path)

    body_start = find_body_start(lines)
    body_end = find_body_end(lines, body_start)

    try:
        target = find_parent(lines, header, body_start, body_end)
    except AmbiguousParent as amb:
        # **할 일이 없으면 멈추지 않는다.** 부모를 못 고르는 것은 「어디에 넣을까」를
        # 못 정한다는 뜻인데, 넣을 것이 하나도 없으면 그 질문 자체가 안 선다.
        #
        # 실제로 이 자리가 스스로 못 푸는 고리를 만들고 있었다 — 붙이려는 답글이 이미
        # md 에 들어 있어도 `find_parent` 가 **중복 검사보다 먼저** 터지므로 「이미
        # 반영됨」 판정에 닿지 못했다. 부르는 쪽(`slack-archive.js` ④-b)은 그 실패로
        # 채널 회차를 중단하고 `last_ts` 를 저장하지 않으므로, 되돌아보기 창이 그
        # 자리에 얼어붙어 **같은 실패를 매일 다시 낸다**. 한 채널이 2026-09-17~09-22
        # 엿새를 그렇게 돌았다 (2026-09-22 확인 — 방아쇠는 09-16 08:53 에 달린 답글
        # 하나였고, 그 채널의 `last_ts` 는 08-25 에 멈춰 있었다. 그 둘은 다른 이야기다:
        # 08-26~09-15 는 실패가 아니라 새 최상위 메시지가 없어 안 움직인 것이다).
        #
        # 그래서 여기서 고르지 않고도 답할 수 있는 질문 하나만 더 묻는다. 새것이
        # 하나라도 있으면 **종전대로 멈춘다** — 추측해서 붙이지 않는다는 규칙은 그대로다.
        #
        # **봇 답변 건수(`--bot-added`)는 올리지 않는다.** 그 값은 어느 블록의 꼬리말을
        # 고칠지 정해야 쓸 수 있는데, 그 자리가 바로 못 고르는 자리다. 그렇다고 이것 때문에
        # 멈추면 위의 고리가 **봇이 답한 스레드에서만 그대로 살아난다** — 그래서 멈추는
        # 대신 **못 올렸다고 말한다**(`NOTE:` 줄). 잃는 것은 「봇 답변이 몇 건 있었다」는
        # 사실 하나이고, 답글 본문은 어차피 안 담는다(WHK 결정 2026-08-12).
        amb_groups = group_replies(block)
        if amb_groups:
            if not _all_replies_present(lines, amb.candidates, body_end, amb_groups):
                raise
        elif not bot_added:
            # 붙일 것도 올릴 것도 없다 — 부르는 쪽이 막지만 여기서도 종전대로 멈춘다
            raise
        if bot_added:
            # **「사람 답글 0건 · 봇 답변만」이 실제로 오는 입력이다** — Hermes 만 답한
            # 스레드가 그렇다(`slack-archive.js` 가 `human.length === 0 && botAdded > 0`
            # 을 그대로 넘긴다). 그때 `block` 이 비어 위 `amb_groups` 가 `[]` 가 되는데,
            # 그것을 실패로 읽으면 **이 고침이 없애려던 고리가 바로 그 경우에만 그대로
            # 살아난다.** 물을 것이 없으면 물을 것이 없는 것이다.
            print(f"NOTE: 같은 시각({amb.minute})에 메시지가 {len(amb.candidates)}건이라 "
                  f"봇 답변 {bot_added}건을 어느 블록의 스레드 건수에 더할지 못 골랐습니다. "
                  "**이 건수는 못 되찾습니다** — 상태가 전진해 다시 제안되지 않습니다. "
                  "그 시각의 md 블록을 슬랙 원문과 대보고 헤더가 갈리게 고치면 앞으로의 "
                  "봇 답변부터 올라갑니다. 그 블록에 `(봇 답변 — 미수록)` 표식 줄이 아직 "
                  "없으면 이번에 안 생깁니다.")
        if report is not None:
            report["mode"] = "append-thread"
            report["written"] = []
            # 실제로 올린 것이 없으므로 0 이다. **지금은 아무도 안 밟는 줄이다** —
            # `--bot-added` 는 ④-b 만, `--report` 는 ④-c 만 주므로 둘이 겹치는 호출자가
            # 없다. 둘이 만나는 날 넘겨받은 값을 그대로 돌려주면 「올렸다」로 세어지므로
            # 미리 막아 둔다.
            report["botAdded"] = 0
        return (f"이미 반영됨 — 같은 시각 {len(amb.candidates)}건이라 부모는 못 골랐지만 "
                "붙일 답글이 전부 이미 있습니다"
                + (f" (봇 답변 {bot_added}건은 셈에 못 올렸습니다)" if bot_added else ""))

    # 경계를 **둘** 잡는다. 하나로 쓰면 어느 쪽으로 잡아도 한쪽이 깨진다.
    #   · 넣는 자리(block_end) 는 **좁게** — `BLOCK_START_RE` 까지 경계로 본다. 아카이브에는
    #     한 줄로 줄여 적은 메시지가 38건 있고 줄끝 앵커가 있는 MSG_HEADER_RE 는 그걸 헤더로
    #     못 본다. 넓게 잡으면 정정이 그 메시지들을 건너뛰어 **남의 발언 아래에 적힌다.**
    #   · 중복 검사(scan_end) 는 **넓게** — 좁게 잡기 전에 들어간 답글이 그 뒤에 있을 수 있고,
    #     좁게 보면 그게 안 보여 같은 정정이 한 번 더 붙는다.
    block_end = body_end
    for i in range(target + 1, body_end):
        if (BLOCK_START_RE.match(lines[i]) or MSG_HEADER_RE.match(lines[i])
                or lines[i].startswith("## ")):
            block_end = i
            break
    scan_end = _reply_scan_end(lines, target, body_end)
    # 블록 끝의 빈 줄은 되돌린다
    while block_end > target + 1 and lines[block_end - 1].strip() == "":
        block_end -= 1

    groups = group_replies(block)

    # 이미 들어 있는 답글은 건너뛴다. 자동 반영이 매일 같은 구간을 다시 훑으므로
    # 이게 없으면 같은 정정이 날마다 한 줄씩 쌓인다. 판정은 **묶음의 첫 줄**로 하고,
    # 거르는 기준이 둘이다.
    #   · `시각 · 이름` 이 이미 있으면 — 예전부터 쓰던 규칙
    #   · **`시각` 과 본문이 같으면** — 이름이 바뀌었을 때를 위한 것. 이것이 없으면 누가 표시
    #     이름을 바꾼 순간 그 사람의 옛 답글이 "새 답글" 로 보여 **날마다 한 줄씩 쌓인다.**
    #     조용히 늘어나는 종류라 눈치채기 어렵다.
    # **개수로 맞춘다** — 완전일치로 먼저 짝을 짓고, 남은 것은 같은 `시각 · 이름` 의 짝 안 지어진
    # 기존 줄이 있으면 이미 있는 것으로 본다. 답글의 진짜 열쇠는 슬랙 ts 인데 md 에는 분까지만
    # 적혀 있어서, 어느 한 값만으로 거르면 반드시 한쪽이 깨진다.
    #
    #   · **헤더(`시각 · 이름`)만 보고 거르면** 같은 1분 안의 두 번째 답글이 사라진다.
    #     `[정정]` 이 바쁜 1분에 들어가면 흔적 없이 없어졌다 — 버리고도 "이미 반영됨" 으로
    #     끝나 부르는 쪽이 성공으로 세고(slack-archive.js) DM 에도 관문에도 안 뜬다.
    #   · **본문까지 요구하면** 표기가 흔들린 옛 답글이 회차마다 새 줄로 쌓인다. 자동 반영은
    #     `.sync-state.json` 에 기록이 없는 스레드의 답글을 **전부** 다시 제안하고(knownCount 0),
    #     초기 수기 추출분은 `-`↔`•`·띄어쓰기·첨부 표기가 실제로 다르다. 게다가 renderText 가
    #     `<#C…>` 를 채널 이름으로 풀게 되면서 흔들릴 자리가 하나 더 늘었다.
    #
    # 둘 다 조용해서 어느 쪽도 눈에 안 띈다. 그래서 「무엇이 같나」가 아니라 「몇 개가 이미
    # 있나」로 판정한다. (2026-08-10)
    existing = _collect_existing(lines, target, scan_end)

    def take(pred):
        """조건에 맞는 **짝 안 지어진** 기존 줄 하나를 소비한다. 있었으면 True."""
        for e in existing:
            if not e["used"] and pred(e):
                e["used"] = True
                return True
        return False

    incoming = []
    for g in groups:
        p = REPLY_LINE_RE.match(g[0])
        h = REPLY_HEAD_RE.match(g[0])
        incoming.append({
            "group": g,
            "time": p.group(1) if p else None,
            "head": h.group(1).strip() if h else None,
            "body": p.group(2).strip() if p else None,
        })

    # ① 완전일치. 이름을 안 보므로 표시 이름이 바뀐 답글도 여기서 짝지어진다.
    left = []
    for it in incoming:
        if it["time"] is not None and take(
            lambda e, it=it: e["time"] == it["time"] and e["body"] == it["body"]
        ):
            continue
        left.append(it)

    # ② 표기 흔들림. 같은 `시각 · 이름` 자리가 아직 남아 있으면 그 줄이 이 답글이다.
    fresh_groups = []
    for it in left:
        if it["head"] is not None and take(lambda e, it=it: e["head"] == it["head"]):
            continue
        fresh_groups.append(it["group"])
    fresh = [l for g in fresh_groups for l in g]
    n_fresh = len(fresh_groups)
    # **무엇을 썼는지**를 부르는 쪽에 돌려준다 (`--report`).
    #
    # 아래 「이미 반영됨」은 **하나도 안 남았을 때만** 나온다. 다섯 중 하나만 새것이면 그
    # 하나를 쓰고 평범한 문구를 내므로, 문구 유무로 이진 판정하면 그 부분 성공이 「다섯 건」이
    # 된다 — DM 이 부풀고, 봇 글에 달린 정정 갈래는 **어느 문장이 쓰였는지 목록까지** 함께
    # 부푼다(쓰이지도 않은 정정 문장이 DM 에 실린다). 개수만으로는 그 목록을 못 만든다.
    # (WHK 결정 2026-09-03 — ㉰안. 일반 답글 쪽은 커밋 `3d61ef0` 이 md 를 전후로 세는
    #  방식으로 이미 막았고, 세는 규칙은 그쪽 하나로 둔 채 여기서는 **신원**만 돌려준다.)
    #
    # 담는 것은 `group_replies` 가 묶은 **답글 단위**다 — 줄 단위로 돌려주면 여러 줄 답글이
    # N건으로 세어져 부풀리는 문제가 자리만 옮겨 다시 생긴다.
    if report is not None:
        report["mode"] = "append-thread"
        report["written"] = ["\n".join(g) for g in fresh_groups]
        report["botAdded"] = bot_added
    if not fresh and not bot_added:
        return "이미 반영됨"

    # `> 💬 스레드 N건 (봇 답변 — 미수록)` 은 항상 맨 끝이다 — 그 앞에 넣는다.
    insert_at = block_end
    for i in range(target + 1, block_end):
        if BOT_TAIL_RE.match(lines[i]):
            insert_at = i
            break

    # 봇 답변 건수는 본문 없이 숫자만 갱신한다
    new_tail = None
    if bot_added:
        new_tail = _bump_bot_tail(lines, target, block_end, insert_at, bot_added)
    if not fresh:
        if new_tail:
            lines = lines[:block_end] + [new_tail] + lines[block_end:]
        note = f"봇 답변 {bot_added}건 (건수만)"
        if dry:
            return f"[dry] {note}"
        write_lines(path, lines, newline)
        return note

    # 스레드 머리줄이 없으면 만들고, 있으면 건수를 갱신한다
    count_at = None
    for i in range(target + 1, block_end):
        if THREAD_HEAD_ANY_RE.match(lines[i]):
            count_at = i
            break

    if count_at is None:
        head = [""] if lines[insert_at - 1].strip() else []
        head.append(f"> 💬 **스레드 ({n_fresh})**")
        new_block = lines[:insert_at] + head + fresh + lines[insert_at:]
        note = f"스레드 머리줄 신설, 답글 {n_fresh}건"
    else:
        m = THREAD_HEAD_ANY_RE.match(lines[count_at])
        prev = int(m.group(2))
        # 꼬리말(`— 제3자 사업성평가`)은 그대로 두고 숫자만 고친다
        lines[count_at] = f"{m.group(1)}{prev + n_fresh}{m.group(3)}{m.group(4)}"
        new_block = lines[:insert_at] + fresh + lines[insert_at:]
        note = f"답글 {n_fresh}건 (스레드 {prev} → {prev + n_fresh})"

    if new_tail:
        # 꼬리줄은 블록 맨 끝. 위에서 줄을 넣은 만큼 블록 끝도 뒤로 밀린다.
        tail_at = block_end + (len(new_block) - len(lines))
        new_block = new_block[:tail_at] + [new_tail] + new_block[tail_at:]
    if bot_added:
        note += f", 봇 답변 {bot_added}건"

    if dry:
        return f"[dry] {note}"
    write_lines(path, new_block, newline)
    return note


def block_candidates(lines, header: str):
    """헤더가 같은 블록을 전부 찾는다 → [(헤더줄 index, 블록 끝, 본문 끝, 현재 본문)]"""
    body_start = find_body_start(lines)
    body_end = find_body_end(lines, body_start)

    hits = []
    for i in range(body_start + 1, body_end):
        if lines[i].strip() != header.strip():
            continue
        end = body_end
        for j in range(i + 1, body_end):
            if BLOCK_START_RE.match(lines[j]) or lines[j].startswith("## "):
                end = j
                break
        while end > i + 1 and lines[end - 1].strip() == "":
            end -= 1
        b_end = i + 1
        while b_end < end and not BODY_TAIL_RE.match(lines[b_end]):
            b_end += 1
        hits.append((i, end, b_end, "\n".join(lines[i + 1:b_end]).strip()))
    return hits


def _one(hits, header, what):
    """후보가 정확히 하나일 때만 돌려준다.

    헤더만으로 고르면 안 된다 — 같은 분·같은 사람 헤더가 아카이브에 118건(57곳·파일 20개, 2026-09-22 실측) 있다
    (예: `비공개가.md` 의 `**2026-07-23 10:41 · WHK**` ×2). 삽입은 어디에 붙어도 크게
    안 틀리지만, **교체·삭제는 엉뚱한 메시지를 조용히 날린다.** 그래서 추측하지 않고 멈춘다.
    """
    if not hits:
        raise ValueError(
            f"헤더는 맞지만 지금 md {what}이(가) 달라 손대지 않았습니다: {header}\n"
            "  아카이브가 이미 바뀌었거나 다른 곳을 가리키고 있습니다. "
            "자동 반영을 한 번 더 돌려 `.pending-edits.json` 을 새로 만든 뒤 시도하세요."
        )
    if len(hits) > 1:
        raise ValueError(f"같은 헤더·같은 {what}인 블록이 {len(hits)}개라 어느 것인지 알 수 없습니다: {header}")
    return hits[0]


def find_block(lines, header: str, expect: str, already: str | None = None):
    """본문까지 맞는 블록 하나. 한 줄로 줄여 적은 메시지는 헤더 줄이 곧 본문이다.

    `already` 에 **이미 반영된 모습**을 주면 그것도 후보로 받는다. 없으면 한 번 반영한 뒤
    같은 지시가 또 오면 "본문이 달라 손대지 않았습니다" 로 떨어져, **반영이 안 된 것처럼
    보인다** — 부르는 쪽의 `이미 같음` 안내는 before 와 after 가 같을 때만 닿아 사실상
    죽은 길이었다. `.pending-edits.json` 의 두 건이 실제로 그 상태였다 (2026-08-10).
    """
    exp = expect.strip()
    alts = {exp}
    if already is not None:
        alts.add(already.strip())
    hits = [h for h in block_candidates(lines, header) if h[3] in alts or lines[h[0]].strip() in alts]
    return _one(hits, header, "본문")


def header_exists(lines, header: str) -> bool:
    """그 헤더의 블록이 **하나라도** 있나. 삭제가 이미 끝났는지 가르는 데 쓴다."""
    return bool(block_candidates(lines, header))


def reply_span(lines, k: int) -> int:
    """답글 한 건이 차지하는 줄 범위의 **끝**(반열림 `[k, end)`).

    **답글은 한 줄이 아니다.** 본문에 줄바꿈이 있으면 둘째 줄부터도 `> ` 안에 들어간다
    (`src/ingest/slack-archive.js` 의 `renderReply` — `>` 가 없으면 인용 블록이
    거기서 끊겨 그 줄이 사람 발언 본문처럼 떠 버리기 때문이다). 아카이브에 36건 있다.

    첫 줄만 보고 교체·삭제하면 **에러 없이** 옛 줄이 남는다 — 교체는 새 첫 줄 밑에 옛
    둘째 줄이 붙고, 삭제는 머리줄만 사라져 남은 줄이 앞 메시지 작성자의 말로 읽힌다
    (2026-08-10 코드 점검에서 실제로 재현했다). 그래서 경계를 여기 한 곳에서 정한다.
    """
    end = k + 1
    while end < len(lines):
        s = lines[end]
        if not s.startswith(">"):
            break
        if REPLY_HEAD_LOOSE_RE.match(s) or THREAD_HEAD_ANY_RE.match(s) or BOT_TAIL_RE.match(s):
            break
        end += 1
    return end


def find_block_by_reply(lines, header: str, reply_key: str, expect_line: str,
                        already: str | None = None):
    """**그 답글 줄을 품고 있는** 블록 하나.

    답글을 고칠 때는 부모 본문을 대조 재료로 쓸 수 없다(감지가 답글 줄만 들고 있다).
    대신 답글 줄 자체가 열쇠 노릇을 한다 — 같은 헤더의 블록 여럿 중에서 그 답글이
    들어 있는 것은 보통 하나뿐이다.

    대조는 **답글 전체**(여러 줄이면 여러 줄 다)와 맞춰 본다. 첫 줄만 맞춰 보면 둘째 줄이
    이미 달라져 있어도 통과해 버린다.

    `already` 를 주면 **이미 반영된 모습**의 답글도 후보로 받는다 (find_block 과 같은 이유).
    """
    alts = {expect_line.strip()}
    if already is not None:
        alts.add(already.strip())
    hits = []
    for h in block_candidates(lines, header):
        i, end = h[0], h[1]
        for k in range(i + 1, end):
            m = REPLY_HEAD_RE.match(lines[k])
            if not m or m.group(1).strip() != reply_key.strip():
                continue
            span = "\n".join(lines[k:reply_span(lines, k)]).strip()
            if span in alts:
                hits.append((h, k))
                break
    h, k = _one(hits, header, f"답글({reply_key})")
    return h[0], h[1], k


def reply_key_exists(lines, header: str, reply_key: str) -> bool:
    """그 헤더 블록 안에 그 답글이 **하나라도** 남아 있나. 삭제가 끝났는지 가르는 데 쓴다."""
    for h in block_candidates(lines, header):
        for k in range(h[0] + 1, h[1]):
            m = REPLY_HEAD_RE.match(lines[k])
            if m and m.group(1).strip() == reply_key.strip():
                return True
    return False


def replace_body(path: Path, header: str, expect: str, new_body: str, dry: bool) -> str:
    """메시지 본문만 갈아 끼운다. 리액션·첨부·스레드 블록은 그대로 둔다."""
    lines, newline = read_lines(path)
    i, _end, b_end, body = find_block(lines, header, expect, already=new_body)

    # 한 줄로 줄여 적은 메시지(`**…** — 7/29 일일업무일지 …`, 38건)는 본문이 헤더 줄에 있다.
    # 이 모드는 헤더 **아래**만 건드리므로, 그대로 두고 아래에 본문을 새로 넣어 내용이 둘이 된다.
    # 문서로만 막으면 실수했을 때 조용히 깨지므로 여기서 멈춘다.
    if re.match(r"^\*\*[^*]+\*\*\s*\S", lines[i]):
        raise ValueError(
            "한 줄로 줄여 적은 메시지라 본문만 갈아 끼울 수 없습니다 (본문이 헤더 줄에 있습니다).\n"
            f"  {lines[i].strip()}\n"
            "  Edit 으로 '헤더 줄 + 본문 줄' 의 정상 형태로 풀어 쓴 뒤 다시 시도하세요."
        )

    new = new_body.rstrip("\n").split("\n")
    if body == "\n".join(new).strip():
        return "이미 같음"

    out = lines[:i + 1] + new + lines[b_end:]
    if dry:
        return f"[dry] {len(lines[i + 1:b_end])}줄 → {len(new)}줄"
    write_lines(path, out, newline)
    return f"{len(lines[i + 1:b_end])}줄 → {len(new)}줄"


def delete_message(path: Path, header: str, expect: str, dry: bool) -> str:
    """메시지 블록을 통째로 지운다 (리액션·첨부·스레드 포함)."""
    lines, newline = read_lines(path)
    # 그 헤더가 아예 없으면 이미 지워진 것이다. 본문이 **다를** 때는 그대로 멈춘다 —
    # 그건 "지울 것이 아직 있는데 내용이 바뀐" 경우라 추측해 지우면 안 된다.
    if not header_exists(lines, header):
        return "이미 지워짐"
    i, end, _b_end, _body = find_block(lines, header, expect)

    # 블록 뒤의 빈 줄까지 걷어낸 뒤, 앞뒤가 붙지 않게 한 줄만 되돌린다
    j = end
    while j < len(lines) and lines[j].strip() == "":
        j += 1
    out = lines[:i] + lines[j:]
    if 0 < i < len(out) and out[i - 1].strip() and out[i].strip():
        out.insert(i, "")

    if dry:
        return f"[dry] {end - i}줄 삭제"
    write_lines(path, out, newline)
    return f"{end - i}줄 삭제"


def replace_reply(path: Path, header: str, reply_key: str, expect: str, new_line: str, dry: bool) -> str:
    """답글 **한 건 전체**를 갈아 끼운다 (여러 줄이면 여러 줄 다)."""
    lines, newline = read_lines(path)
    _i, _end, k = find_block_by_reply(lines, header, reply_key, expect, already=new_line)
    k_end = reply_span(lines, k)

    new = new_line.rstrip("\n").split("\n")
    # 넣는 모양을 여기서 막는다. 인용(`>`)이 빠진 줄은 md 에서 인용 블록을 끊어 그 줄이
    # 사람 발언 본문처럼 뜨는데, 그건 **에러 없이** 일어나 아무도 모른다.
    if not REPLY_HEAD_LOOSE_RE.match(new[0]):
        raise ValueError(f"답글 교체 내용의 첫 줄은 '> **└ …**' 모양이어야 합니다: {new[0][:60]}")
    loose = [n for n in new[1:] if not n.startswith(">")]
    if loose:
        raise ValueError(
            "답글은 둘째 줄부터도 '> ' 안에 있어야 합니다 (인용이 끊기면 그 줄이 "
            f"사람 발언 본문으로 읽힙니다): {loose[0][:60]}"
        )

    if lines[k:k_end] == new:
        return "이미 같음"
    note = f"{reply_key} 교체" + (f" ({k_end - k}줄 → {len(new)}줄)" if k_end - k > 1 or len(new) > 1 else "")
    if dry:
        return f"[dry] {note}"
    write_lines(path, lines[:k] + new + lines[k_end:], newline)
    return note


def delete_reply(path: Path, header: str, reply_key: str, expect: str, dry: bool) -> str:
    """답글 **한 건 전체**를 지우고 스레드 머리줄의 건수를 내린다."""
    lines, newline = read_lines(path)
    # 그 답글이 아예 없으면 이미 지워진 것 (delete_message 와 같은 판정).
    if not reply_key_exists(lines, header, reply_key):
        return "이미 지워짐"
    i, end, k = find_block_by_reply(lines, header, reply_key, expect)
    k_end = reply_span(lines, k)

    out = lines[:k] + lines[k_end:]
    note = f"{reply_key} 삭제" + (f" ({k_end - k}줄)" if k_end - k > 1 else "")

    # 스레드 머리줄 건수. 꼬리말(`— 제3자 사업성평가`)이 붙은 것이 있어 그대로 보존한다.
    for h in range(i + 1, end):
        m = THREAD_HEAD_ANY_RE.match(out[h] if h < len(out) else "")
        if not m:
            continue
        prev = int(m.group(2))
        if prev <= 1:
            out = out[:h] + out[h + 1:]          # 남는 답글이 없으면 머리줄도 지운다
            note += " (스레드 머리줄 제거)"
        else:
            out[h] = f"{m.group(1)}{prev - 1}{m.group(3)}{m.group(4)}"
            note += f" (스레드 {prev} → {prev - 1})"
        break

    if dry:
        return f"[dry] {note}"
    write_lines(path, out, newline)
    return note


def update_header(path: Path, period_end: str | None, count: int | None, dry: bool = False) -> str:
    """헤더의 '**기간**: A ~ B · **실제 메시지**: N건' 줄을 갱신.

    **요청한 항목을 못 찾으면 멈춘다.** 예전에는 못 찾아도 `변경 없음` + 종료코드 0 이라,
    부르는 쪽(자동 반영)이 성공으로 읽고 넘어갔다. 이 줄은 봇 프롬프트에 실려 규모감이
    되는 값이라, 조용히 낡으면 봇이 낡은 건수로 답한다.
    """
    if not period_end and count is None:
        raise ValueError("--update-header 에는 --period-end 또는 --count 가 필요합니다")

    lines, newline = read_lines(path)
    text = "\n".join(lines)
    changed = []

    if period_end:
        def _period(m):
            new = f"{m.group(1)} ~ {period_end}"
            if m.group(0) != new:
                changed.append("기간")
            return new
        # 기간이 `A ~ B` 인 채널이 대부분이지만 **단일 날짜**인 채널이 있을 수 있다.
        # `~ B` 를 필수로 보면 그 채널은 영영 안 맞는데 예전에는 그것을 성공으로 보고했다.
        # **지금 아카이브에는 그런 채널이 없다** — 근거였던 `z_비공개마_옛이름.md`
        # (`**기간**: 2026-06-11`) 는 채널이 범위 밖이 되어 2026-08-31 에 지웠다.
        # **없다고 이 선택 그룹을 필수로 바꾸지 말 것** — 메시지가 하루치뿐인 채널이
        # 새로 들어오면 그날로 다시 생긴다.
        text, n = re.subn(
            r"(\*\*기간\*\*:\s*\d{4}-\d{2}-\d{2})(?:\s*~\s*\d{4}-\d{2}-\d{2})?",
            _period, text, count=1,
        )
        if not n:
            raise ValueError("헤더에서 '**기간**: YYYY-MM-DD' 를 찾지 못했습니다")

    if count is not None:
        def _count(m):
            new = f"**실제 메시지**: {count}건"
            if m.group(0) != new:
                changed.append("메시지수")
            return new
        text, n = re.subn(
            r"\*\*실제 메시지\*\*:\s*(?:약\s*)?[\d~]+건",
            _count, text, count=1,
        )
        if not n:
            raise ValueError("헤더에서 '**실제 메시지**: N건' 을 찾지 못했습니다")

    if not changed:
        return "이미 같음"
    if dry:
        return f"[dry] {', '.join(changed)}"
    write_lines(path, text.split("\n"), newline)
    return ", ".join(changed)


def main():
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다 —
    # 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 밖으로 나간다.
    from mode import exit_if_blocked
    exit_if_blocked("원문 메시지 삽입(insert_messages)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="채널 md 경로")
    ap.add_argument("--month", help="삽입할 월 (YYYY-MM)")
    ap.add_argument(
        "--at-month-end",
        action="store_true",
        help="그 달의 맨 뒤에 붙인다. 초기 구간 채우기(3.5단계) 전용 — "
        "넣는 것이 기존보다 오래된 메시지일 때. 기본은 맨 위",
    )
    ap.add_argument(
        "--in-order",
        action="store_true",
        help="헤더의 시각을 읽어 기존 블록들 사이 제자리에 넣는다. "
        "넣는 것이 가장 새 메시지라는 보장이 없을 때 (며칠 전 글의 자리표시 블록 등)",
    )
    ap.add_argument("--content", help="삽입할 본문")
    ap.add_argument("--content-file", help="삽입할 본문이 든 파일")
    ap.add_argument("--update-header", action="store_true")
    ap.add_argument("--period-end", help="헤더 기간 종료일 (YYYY-MM-DD)")
    ap.add_argument("--count", type=int, help="헤더 메시지 수")
    ap.add_argument(
        "--append-thread",
        help="이미 저장된 메시지 블록에 스레드 답글을 덧붙인다. "
             "값은 부모 메시지 헤더 줄 (예: '**2026-08-04 13:08 · 홍길동**')",
    )
    ap.add_argument(
        "--bot-added", type=int, default=0,
        help="그 스레드에 새로 달린 봇 답변 수. 본문 없이 '(봇 답변 — 미수록)' 건수만 올린다",
    )
    # 슬랙 원문이 바뀐 것을 따라가는 모드 (사람이 지시할 때만)
    ap.add_argument("--replace-body", action="store_true", help="메시지 본문만 교체")
    ap.add_argument("--delete-message", action="store_true", help="메시지 블록을 통째로 삭제")
    ap.add_argument("--replace-reply", action="store_true", help="스레드 답글 한 줄 교체")
    ap.add_argument("--delete-reply", action="store_true", help="스레드 답글 한 줄 삭제")
    ap.add_argument("--header", help="대상 메시지의 헤더 줄 (예: '**2026-08-04 13:08 · 홍길동**')")
    ap.add_argument("--reply-key", help="대상 답글의 'YYYY-MM-DD HH:MM · 이름'")
    ap.add_argument("--expect-file", help="지금 md 에 있어야 하는 내용. 다르면 손대지 않고 멈춘다")
    ap.add_argument("--dry-run", action="store_true", help="바뀔 자리만 찍고 파일은 안 건드린다")
    ap.add_argument(
        "--report", action="store_true",
        help="맨 끝에 'REPORT {json}' 한 줄을 더 찍는다 — **실제로 써 넣은 것**의 목록. "
             "--append-thread 와 월 삽입에서만 쓴다 (다른 모드는 에러로 멈춘다)",
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: 파일 없음 — {path}", file=sys.stderr)
        return 1

    # `--report` 는 「무엇을 실제로 써 넣었나」를 돌려주는 자리라, 그것을 셀 수 있는 두
    # 모드에만 있다. 다른 모드에 붙여 주면 **조용히 아무것도 안 나오고**, 받는 쪽은 그것을
    # 「하나도 안 썼다」로 읽는다 — 지금 막으려는 것과 같은 종류의 조용한 어긋남이다.
    report = {} if args.report else None
    if args.report and not (args.append_thread or args.month):
        print("ERROR: --report 는 --append-thread 또는 --month 삽입에서만 씁니다", file=sys.stderr)
        return 1

    def emit_report():
        if report is None:
            return
        # 한글이 오가므로 아스키로만 내보낸다 — 받는 쪽 stdout 인코딩에 기대지 않는다
        # (`scripts/check-shared-rules.js` 가 같은 이유로 같은 방식을 쓴다).
        print("REPORT " + json.dumps(report, ensure_ascii=True))

    try:
        # ── 슬랙 원문이 바뀐 것을 따라가는 모드 ──
        edit_modes = [args.replace_body, args.delete_message, args.replace_reply, args.delete_reply]
        if any(edit_modes):
            if sum(bool(x) for x in edit_modes) > 1:
                print("ERROR: 교체·삭제 모드는 한 번에 하나만", file=sys.stderr)
                return 1
            if not args.header:
                print("ERROR: --header 필요", file=sys.stderr)
                return 1
            if not args.expect_file:
                # 대조 없이 고치면 헤더가 겹치는 118건(57곳·파일 20개, 2026-09-22 실측) 에서 엉뚱한 메시지를 조용히 날린다
                print("ERROR: --expect-file 필요 (지금 md 내용과 대조한 뒤에만 손댑니다)", file=sys.stderr)
                return 1
            expect = Path(args.expect_file).read_text(encoding="utf-8").strip()

            if args.replace_body or args.replace_reply:
                if args.content_file:
                    new = Path(args.content_file).read_text(encoding="utf-8")
                elif args.content is not None:
                    new = args.content
                else:
                    print("ERROR: --content 또는 --content-file 필요", file=sys.stderr)
                    return 1

            if args.replace_body:
                result = replace_body(path, args.header, expect, new, args.dry_run)
                label = "본문 교체"
            elif args.delete_message:
                result = delete_message(path, args.header, expect, args.dry_run)
                label = "메시지 삭제"
            else:
                if not args.reply_key:
                    print("ERROR: --reply-key 필요", file=sys.stderr)
                    return 1
                if args.replace_reply:
                    result = replace_reply(path, args.header, args.reply_key, expect, new, args.dry_run)
                    label = "답글 교체"
                else:
                    result = delete_reply(path, args.header, args.reply_key, expect, args.dry_run)
                    label = "답글 삭제"

            if result == "이미 같음":
                print(f"SKIP 이미 같음 — {path.name}")
            else:
                print(f"OK {label} ({result}) — {path.name}")
            return 0

        if args.append_thread:
            if args.content_file:
                block = Path(args.content_file).read_text(encoding="utf-8")
            elif args.content:
                block = args.content
            else:
                block = ""
            if not block.strip() and not args.bot_added:
                print(f"SKIP 빈 본문 — {path.name}")
                if report is not None:
                    report.update({"mode": "append-thread", "written": [], "botAdded": 0})
                emit_report()
                return 0
            result = append_thread(path, args.append_thread, block, args.bot_added,
                                   args.dry_run, report)
            print(f"OK 스레드 덧붙임 ({result}) — {path.name}")
            emit_report()
            return 0

        if args.update_header:
            result = update_header(path, args.period_end, args.count, args.dry_run)
            if result == "이미 같음":
                print(f"SKIP 헤더 이미 같음 — {path.name}")
            else:
                print(f"OK 헤더 갱신 ({result}) — {path.name}")
            return 0

        if not args.month:
            print("ERROR: --month 필요", file=sys.stderr)
            return 1

        if args.content_file:
            block = Path(args.content_file).read_text(encoding="utf-8")
        elif args.content:
            block = args.content
        else:
            print("ERROR: --content 또는 --content-file 필요", file=sys.stderr)
            return 1

        if not block.strip():
            print(f"SKIP 빈 본문 — {path.name}")
            if report is not None:
                report.update({"mode": "insert", "written": []})
            emit_report()
            return 0

        if args.at_month_end and args.in_order:
            print("ERROR: --at-month-end 와 --in-order 는 함께 못 씁니다", file=sys.stderr)
            return 1

        where = insert_block(path, args.month, block, at_end=args.at_month_end,
                             in_order=args.in_order, dry=args.dry_run, report=report)
        print(f"OK 삽입 ({where}) — {path.name}")
        emit_report()
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
