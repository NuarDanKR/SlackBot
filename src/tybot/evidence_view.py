"""`근거 보기` — 답변이 실제로 무엇을 읽고 만들어졌는지 사람이 직접 본다.

## 왜
사내 피드백: "봇이 어떤 과정을 거쳐 그런 답변을 하는지 알 수 없다."

출처 줄(`#채널, 📄문서`)은 *어디서* 왔는지만 말한다. *무엇이 적혀 있었는지*는 여전히
안 보인다. 원문 줄을 그대로 보여주면 질문이 바뀐다 — "봇을 믿을 수 있나" 에서
"이 답이 맞나" 로. 뒤쪽은 사람이 검증할 수 있는 질문이다.

## 원문은 복제하지 않고 좌표만 저장한다.
근거 줄을 감사기록에 복사하면 ACL이 다른 원문 사본이 하나 더 생긴다. 대신 답변 당시
읽은 `EvidenceRef` 좌표만 저장하고, 버튼을 누를 때 현재 권한으로 원문을 다시 연다.
검색을 다시 하지 않으므로 답변과 다른 문서가 나타나지 않고, 권한이 사라진 줄은 보이지
않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .channels import source_label

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


def button(record_id: str) -> dict | None:
    """근거 보기 버튼. **답변 기록 ID 하나만** 싣는다(인계 §6.2).

    예전에는 검색어를 실어 클릭 시 다시 검색했다. 두 가지가 잘못됐다.

    1. 보여 주는 것이 **답변이 읽은 원문이 아니라** 지금 그 낱말로 나오는 것이었다.
    2. 질문에서 뽑은 낱말이 버튼 값으로 Slack 에 남았다.

    실패 답변에는 붙이지 않는다 — 버튼이 있는데 눌러도 아무것도 안 나오면
    없는 것만 못하다. 그 판정은 `blocks()` 가 한다.
    """
    key = (record_id or "").strip()
    if not key:
        return None
    return {
        "type": "actions",
        "elements": [{
            "type": "button",
            "action_id": ACTION_SHOW,
            "text": {"type": "plain_text", "text": "근거 보기"},
            "value": key[:MAX_VALUE],
        }],
    }


def blocks(
    body: str,
    *,
    record_id: str = "",
    has_evidence: bool = False,
    answered: bool = False,
) -> list[dict]:
    """답변 본문 + 근거 보기 버튼.

    버튼은 **셋이 모두 참일 때만** 붙는다(인계 §6.1).

    - 답변이 성공했다(`answered`)
    - 다시 열 좌표가 있다(`has_evidence`)
    - 그 좌표를 찾을 기록 ID 가 있다(`record_id`)

    `timeout`·`specialist_unavailable`·근거 없음 답변에 버튼이 붙으면, 사람은
    눌러 보고 빈 화면을 받는다. 검색어가 있다는 이유만으로 만들지 않는다.
    """
    out: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": part}}
        for part in _split_sections(body)
    ]
    if answered and has_evidence:
        btn = button(record_id)
        if btn:
            out.append(btn)
    return out


def _split_sections(body: str, *, limit: int = 2900, max_sections: int = 45) -> list[str]:
    """Slack section 한도를 지키면서 출처를 포함한 답변 전체를 보존한다."""
    remaining = body.strip() or "답변 내용이 없습니다."
    parts: list[str] = []
    while remaining and len(parts) < max_sections:
        if len(remaining) <= limit:
            parts.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append("_답변이 Slack 표시 한도를 초과했습니다. `근거 보기`에서 원문을 확인하세요._")
    return parts


def _stamp(ts: str) -> str:
    v = (ts or "").strip()
    if len(v) >= 16 and v[10] in ("T", " "):
        return f"{v[5:10]} {v[11:16]}"
    return v or "-"


ACTION_DISMISS = "tybot_dismiss_evidence"

NO_RECORD = (
    "이 답변의 기록을 찾지 못했습니다.\n"
    "다른 채널 또는 다른 사용자의 DM 답변일 수 있습니다."
)

# 좌표를 다시 열지 못한 사유. **업무 내용을 담지 않는 고정 낱말**이라 그대로 보인다.
DROP_LABELS = {
    "acl": "권한이 바뀌어 볼 수 없습니다",
    "missing": "문서를 찾지 못했습니다",
    "changed": "원문이 바뀌어 같은 줄을 찾지 못했습니다",
    "live_not_archived": "실시간으로 읽은 대화라 아카이브에 없습니다",
}


def modal(text: str) -> dict:
    """근거 모달. 닫으면 **원래 스레드가 그대로** 남는다(인계 §6.3).

    ephemeral 은 별도 메시지가 쌓이고 「이전」 으로 돌아갈 곳이 없었다.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "답변 근거"},
        # 닫기는 Slack 기본 버튼을 쓴다. 우리가 만들면 동작이 두 가지가 된다.
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": part}}
            for part in _split_sections(text, limit=2900, max_sections=40)
        ],
    }


def fallback_blocks(text: str) -> list[dict]:
    """모달을 못 열었을 때의 ephemeral. **지울 수 있게** 버튼을 준다."""
    return [
        *(
            {"type": "section", "text": {"type": "mrkdwn", "text": part}}
            for part in _split_sections(text)
        ),
        {"type": "actions", "elements": [{
            "type": "button",
            "action_id": ACTION_DISMISS,
            "text": {"type": "plain_text", "text": "닫기"},
            "value": "dismiss",
        }]},
    ]


def stored_report(
    lines: list[EvidenceLine], *, dropped: list[str], own_workspace: str = ""
) -> str:
    """답변 당시 좌표를 지금 권한으로 다시 연 결과.

    **다시 검색한 것이 아니다.** 그 말을 머리글에 적는다 — 사람이 "왜 검색
    결과와 다르지" 를 묻지 않게.
    """
    from .channel_lifecycle import mark_for
    from .channels import source_label

    if not lines and not dropped:
        return NO_EVIDENCE
    head = f"*답변이 읽은 원문* — {len(lines)}줄"
    out = [head, ""]
    last = ""
    for line in lines:
        label = source_label(line.channel)
        if line.workspace and line.workspace != own_workspace:
            label = f"{label} ({line.workspace})"
        label += mark_for(line.workspace, None, line.channel)
        if label != last:
            out.append(f"*{label}*")
            last = label
        out.append(f"    `{_stamp(line.ts)}` {line.speaker}: {line.text}")
    if dropped:
        reasons = ", ".join(
            DROP_LABELS.get(code, code) for code in dict.fromkeys(dropped)
        )
        out += ["", f"_일부 근거는 지금 열 수 없습니다: {reasons}_"]
    out += ["", "_지금 권한으로 다시 연 것입니다. 새로 검색하지 않았습니다._"]
    return "\n".join(out)


NO_EVIDENCE = (
    "답변 당시 근거 좌표를 현재 권한으로 열 수 없습니다.\n"
    "답변 이후 채널 권한이 바뀌었거나, 원문이 정리됐을 수 있습니다."
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
        # 출처와 **같은 이름**으로 보여야 한다. 여기만 채널명이면 사람이
        # 「출처에 적힌 그 자료」 를 이 패널에서 못 찾는다.
        label = source_label(line.channel)
        if line.workspace and line.workspace != own_workspace:
            label = f"{label} ({line.workspace})"
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
