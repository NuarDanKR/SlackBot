"""`근거 보기` — 답변이 실제로 무엇을 읽고 만들어졌는지 사람이 직접 본다.

## 왜
사내 피드백: "봇이 어떤 과정을 거쳐 그런 답변을 하는지 알 수 없다."

출처 줄(`#채널, 📄문서`)은 *어디서* 왔는지만 말한다. *무엇이 적혀 있었는지*는 여전히
안 보인다. 원문 줄을 그대로 보여주면 질문이 바뀐다 — "봇을 믿을 수 있나" 에서
"이 답이 맞나" 로. 뒤쪽은 사람이 검증할 수 있는 질문이다.

## 저장하지 않는다. 다시 찾는다.
근거 줄을 감사기록에 복사해 두는 방법도 있지만 그러면 **원문이 아카이브 밖에 한 벌 더
생긴다** — ACL 이 다른 두 번째 사본이고, 시간이 지나면 아카이브와 어긋난다.

대신 누를 때 **같은 검색어로 다시 찾는다.** 그때 권한도 다시 판정되므로, 답변을 받은 뒤
채널에서 나간 사람에게는 근거가 보이지 않는다. 저장 방식이었다면 그대로 보였을 것이다.

대가: 그사이 아카이브가 자라면 줄이 조금 달라질 수 있다. 그래서 "지금 다시 찾은
결과" 라고 밝힌다 — 그럴듯하게 같은 척하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ACTION_SHOW = "tybot_show_evidence"

# 한 화면에 보여줄 줄 수. 넘치면 더 보여주는 대신 검색을 좁히라고 안내한다.
MAX_LINES = 12
# Slack 버튼 value 상한은 2000자다. 검색어만 담으므로 여유가 크지만 잘라 둔다.
MAX_VALUE = 1900


@dataclass(frozen=True)
class EvidenceLine:
    channel: str
    ts: str
    speaker: str
    text: str
    workspace: str = ""


def button(terms: list[str], *, workspace: str = "") -> dict | None:
    """근거 보기 버튼. 검색어가 없으면(기간 요약 등) 만들지 않는다.

    버튼이 있는데 눌러도 아무것도 안 나오는 것보다, 없는 편이 낫다.
    """
    query = " ".join(t for t in (terms or []) if t).strip()
    if not query:
        return None
    return {
        "type": "actions",
        "elements": [{
            "type": "button",
            "action_id": ACTION_SHOW,
            "text": {"type": "plain_text", "text": "근거 보기"},
            "value": query[:MAX_VALUE],
        }],
    }


def blocks(body: str, terms: list[str], *, workspace: str = "") -> list[dict]:
    """답변 본문 + 근거 보기 버튼."""
    out: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}}]
    btn = button(terms, workspace=workspace)
    if btn:
        out.append(btn)
    return out


def _stamp(ts: str) -> str:
    v = (ts or "").strip()
    if len(v) >= 16 and v[10] in ("T", " "):
        return f"{v[5:10]} {v[11:16]}"
    return v or "-"


NO_EVIDENCE = (
    "지금 다시 찾아보니 이 검색어로 나오는 원문이 없습니다.\n"
    "답변 이후 채널 권한이 바뀌었거나, 아카이브가 정리됐을 수 있습니다."
)


def report(lines: list[EvidenceLine], *, query: str, own_workspace: str = "") -> str:
    """원문 줄 목록. 손대지 않고 그대로 보여준다 — 요약하면 그게 또 하나의 답변이 된다."""
    if not lines:
        return NO_EVIDENCE

    shown = lines[:MAX_LINES]
    head = (
        f"*「{query}」로 지금 다시 찾은 원문* — {len(shown)}줄"
        + (f" (전체 {len(lines)}줄 중)" if len(lines) > len(shown) else "")
    )
    out = [head, ""]
    last_channel = ""
    for line in shown:
        label = line.channel
        if line.workspace and line.workspace != own_workspace:
            label = f"[{line.workspace}] {line.channel}"
        if label != last_channel:
            out.append(f"*{label}*")
            last_channel = label
        out.append(f"    `{_stamp(line.ts)}` {line.speaker}: {line.text}")

    out += [
        "",
        "_답변은 이 줄들만 근거로 만들어집니다. 회원님이 볼 수 있는 채널만 검색합니다._",
    ]
    if len(lines) > len(shown):
        out.append(f"_{MAX_LINES}줄까지만 보여드립니다. 검색어를 좁히면 더 정확해집니다._")
    return "\n".join(out)


# --- 표 렌더 ------------------------------------------------------------------
#
# Slack `mrkdwn` 에는 표 문법이 없고 글꼴 크기 제어도 없다. 우리 코드 문제가 아니라
# Slack 메시지 서식의 한계다. 유일하게 **줄이 맞는** 방법은 고정폭 코드 블록이다.
#
# 모델이 마크다운 표(`| a | b |`)를 뱉으면 Slack 에서 파이프가 그대로 보이고 열이
# 어긋난다. 그걸 잡아 코드 블록으로 다시 그린다 — 프롬프트로 금지하는 것만으로는
# 새는 경우가 남는다.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 이보다 넓으면 모바일에서 줄바꿈이 생겨 오히려 더 읽기 어렵다.
MAX_TABLE_WIDTH = 68


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _width(text: str) -> int:
    """한글은 두 칸을 차지한다. 이걸 세지 않으면 열이 어긋난다."""
    return sum(2 if ord(ch) > 0x1100 and not ch.isascii() else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _width(text), 0)


def render_table(rows: list[list[str]]) -> str:
    """줄이 맞는 고정폭 표. 코드 블록으로 감싼다.

    넓으면 표를 포기하고 항목 나열로 되돌린다 — 깨진 표보다 낫다.
    """
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    grid = [[*r, *([""] * (cols - len(r)))] for r in rows]
    widths = [max(_width(r[i]) for r in grid) for i in range(cols)]

    if sum(widths) + 3 * (cols - 1) > MAX_TABLE_WIDTH:
        head, *body = grid
        out = []
        for row in body:
            out.append(
                "• " + " · ".join(
                    f"{h}: {v}" for h, v in zip(head, row, strict=False) if v
                )
            )
        return "\n".join(out)

    head, *body = grid
    lines = ["  ".join(_pad(c, w) for c, w in zip(head, widths, strict=False)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        lines.append(
            "  ".join(_pad(c, w) for c, w in zip(row, widths, strict=False)).rstrip()
        )
    return "```\n" + "\n".join(lines) + "\n```"


def fix_markdown_tables(text: str) -> str:
    """답변에 섞인 마크다운 표를 Slack 에서 읽히는 형태로 바꾼다.

    프롬프트로 금지해도 새는 경우가 있고, 새면 사용자에게는 그냥 깨진 표로 보인다.
    """
    out: list[str] = []
    buffer: list[list[str]] = []

    def flush() -> None:
        if buffer:
            out.append(render_table(buffer))
            buffer.clear()

    for line in (text or "").splitlines():
        if _TABLE_ROW.match(line):
            if not _TABLE_SEP.match(line):  # `|---|---|` 구분선은 버린다
                buffer.append(_cells(line))
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)
