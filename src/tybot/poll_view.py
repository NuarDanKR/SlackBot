"""투표 화면 조립 — 모달과 메시지 블록.

`polls.py` 가 규칙이고 여기는 보여 주는 방식이다. Slack SDK 를 쓰지 않고 사전(dict)만
만들어 돌려주므로, 블록 구조를 Slack 없이 테스트할 수 있다.

## 왜 버튼인가
선택지를 버튼으로 두면 한 번 누르는 것으로 투표가 끝난다. 드롭다운(static_select)은
누르고·고르고·닫는 세 동작이 필요하고, 모바일에서 특히 번거롭다.

Slack 은 한 `actions` 블록에 요소를 25개까지 허용하지만 화면이 좁아지므로 선택지는
5개씩 줄을 나눈다.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .polls import (
    MAX_OPTIONS,
    SHOW_AFTER_CLOSE,
    SHOW_AFTER_VOTE,
    SHOW_ALWAYS,
    Poll,
)

# 결과 막대 길이. 폭이 좁은 모바일에서도 한 줄에 들어오는 길이로 잡는다.
BAR_WIDTH = 12
BUTTONS_PER_ROW = 5

MODAL_CALLBACK = "tybot_create_poll"
ACTION_VOTE = "tybot_poll_vote"
ACTION_CLOSE = "tybot_poll_close"
ACTION_RESULTS = "tybot_poll_results"

DEADLINE_CHOICES = [
    ("없음", "none"),
    ("30분 후", "30m"),
    ("1시간 후", "1h"),
    ("3시간 후", "3h"),
    ("6시간 후", "6h"),
    ("내일 이 시간", "1d"),
    ("3일 후", "3d"),
    ("1주 후", "7d"),
]

SHOW_LABELS = {
    SHOW_ALWAYS: "항상 공개 — 진행 상황을 바로 봅니다",
    SHOW_AFTER_VOTE: "투표한 사람에게만 — 내가 고른 뒤 결과가 보입니다",
    SHOW_AFTER_CLOSE: "마감 후에만 — 앞선 표가 뒤 표에 영향을 주지 않습니다",
}


def _opt(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text, "emoji": True}, "value": value}


# ---------------------------------------------------------------------------
# 만들기 모달
# ---------------------------------------------------------------------------

def create_modal(*, channel_id: str, prefill_question: str = "") -> dict:
    """`/투표` 를 입력했을 때 열리는 화면."""
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "투표 만들기"},
        "submit": {"type": "plain_text", "text": "올리기"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "input",
                "block_id": "question",
                "label": {"type": "plain_text", "text": "무엇을 물어볼까요?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "initial_value": prefill_question,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "예: 다음 주 정기회의 시간을 언제로 할까요?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "options",
                "label": {"type": "plain_text", "text": "선택지 (한 줄에 하나씩)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "월요일 10시\n화요일 14시\n수요일 16시",
                    },
                },
                "hint": {
                    "type": "plain_text",
                    "text": f"2개 이상 {MAX_OPTIONS}개까지. 번호는 자동으로 붙습니다.",
                },
            },
            {
                "type": "input",
                "block_id": "settings",
                "optional": True,
                "label": {"type": "plain_text", "text": "투표 방식"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "value",
                    "options": [
                        {
                            **_opt("여러 개 고를 수 있게 하기", "multi"),
                            "description": {
                                "type": "plain_text",
                                "text": "끄면 하나만 고를 수 있습니다.",
                            },
                        },
                        {
                            **_opt("익명 투표", "anonymous"),
                            "description": {
                                "type": "plain_text",
                                "text": "누가 무엇을 골랐는지 아무도 볼 수 없습니다.",
                            },
                        },
                        {
                            **_opt("투표 후 변경 못 하게 하기", "lock"),
                            "description": {
                                "type": "plain_text",
                                "text": "끄면 마감 전까지 선택을 바꿀 수 있습니다.",
                            },
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "show_results",
                "optional": True,
                "label": {"type": "plain_text", "text": "결과를 언제 보여줄까요?"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "initial_option": _opt(SHOW_LABELS[SHOW_ALWAYS], SHOW_ALWAYS),
                    "options": [_opt(SHOW_LABELS[k], k) for k in SHOW_LABELS],
                },
            },
            {
                "type": "input",
                "block_id": "deadline",
                "optional": True,
                "label": {"type": "plain_text", "text": "마감"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "initial_option": _opt("없음", "none"),
                    "options": [_opt(label, value) for label, value in DEADLINE_CHOICES],
                },
                "hint": {
                    "type": "plain_text",
                    "text": "마감을 정하지 않으면 만든 사람이 직접 마감할 때까지 열려 있습니다.",
                },
            },
        ],
    }


def read_modal(view: dict) -> dict:
    """모달 제출 값을 `create_poll()` 인자 모양으로 꺼낸다."""
    values = (view.get("state") or {}).get("values") or {}

    def text(block: str) -> str:
        return ((values.get(block) or {}).get("value") or {}).get("value") or ""

    def selected(block: str) -> str | None:
        chosen = ((values.get(block) or {}).get("value") or {}).get("selected_option")
        return (chosen or {}).get("value")

    picked = {
        o.get("value")
        for o in (((values.get("settings") or {}).get("value") or {}).get("selected_options") or [])
    }

    return {
        "channel_id": view.get("private_metadata") or "",
        "question": text("question"),
        "options_text": text("options"),
        "multi": "multi" in picked,
        "anonymous": "anonymous" in picked,
        # 화면에서는 "변경 못 하게 하기"로 묻고, 내부에서는 허용 여부로 뒤집어 쓴다.
        "allow_change": "lock" not in picked,
        "show_results": selected("show_results") or SHOW_ALWAYS,
        "deadline": selected("deadline") or "none",
    }


# ---------------------------------------------------------------------------
# 투표 메시지
# ---------------------------------------------------------------------------

def _bar(count: int, total: int) -> str:
    if total <= 0:
        return "░" * BAR_WIDTH
    filled = round(BAR_WIDTH * count / total)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _deadline_text(poll: Poll, *, now: datetime | None = None) -> str:
    if not poll.closes_at:
        return ""
    try:
        deadline = datetime.fromisoformat(poll.closes_at)
    except ValueError:
        return ""
    current = now or datetime.now(UTC)
    if current >= deadline:
        return "마감됨"
    remain = deadline - current
    hours, seconds = divmod(int(remain.total_seconds()), 3600)
    if hours >= 24:
        return f"{hours // 24}일 {hours % 24}시간 남음"
    if hours:
        return f"{hours}시간 {seconds // 60}분 남음"
    return f"{max(1, seconds // 60)}분 남음"


def _badges(poll: Poll, *, now: datetime | None = None) -> str:
    marks = ["여러 개 선택 가능" if poll.multi else "하나만 선택"]
    if poll.anonymous:
        marks.append("익명")
    if not poll.allow_change:
        marks.append("변경 불가")
    if poll.show_results == SHOW_AFTER_CLOSE:
        marks.append("결과는 마감 후 공개")
    elif poll.show_results == SHOW_AFTER_VOTE:
        marks.append("투표하면 결과 공개")
    when = _deadline_text(poll, now=now)
    if when:
        marks.append(when)
    if poll.closed:
        marks.append("마감")
    return " · ".join(marks)


def results_lines(poll: Poll, *, now: datetime | None = None) -> list[str]:
    counts = poll.counts()
    total = max(1, poll.voter_count) if not poll.multi else max(1, sum(counts))
    voters = poll.voters_by_option()
    lines: list[str] = []
    for i, option in enumerate(poll.options):
        share = round(100 * counts[i] / total) if total else 0
        line = f"`{_bar(counts[i], total)}` *{option}* — {counts[i]}표 ({share}%)"
        if not poll.anonymous and voters[i]:
            shown = " ".join(f"<@{u}>" for u in voters[i][:12])
            more = f" 외 {len(voters[i]) - 12}명" if len(voters[i]) > 12 else ""
            line += f"\n　　{shown}{more}"
        lines.append(line)
    void = _deadline_text(poll, now=now)
    return lines or [f"선택지가 없습니다. {void}"]


def message_blocks(poll: Poll, *, viewer: str = "", now: datetime | None = None) -> list[dict]:
    """채널에 올라가는 투표 메시지.

    `viewer` 가 비어 있으면 **아무도 결과를 보지 못하는 상태**로 그린다. 채널 메시지는
    한 장을 모두가 보므로, '투표한 사람에게만 공개'는 메시지에서 결과를 감추고
    각자 `결과 보기` 버튼으로 확인하게 한다.
    """
    open_now = poll.is_open(now=now)
    header = f"*{poll.question}*"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<@{poll.creator}> 님의 투표 · {_badges(poll, now=now)}"}
            ],
        },
    ]

    show_here = poll.show_results == SHOW_ALWAYS or not open_now
    if show_here:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(results_lines(poll, now=now))},
            }
        )
    else:
        hidden = (
            "결과는 마감 후에 공개됩니다."
            if poll.show_results == SHOW_AFTER_CLOSE
            else "투표하면 결과가 보입니다."
        )
        numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(poll.options))
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{numbered}\n\n_{hidden}_"}}
        )

    if open_now:
        for start in range(0, len(poll.options), BUTTONS_PER_ROW):
            chunk = list(enumerate(poll.options))[start : start + BUTTONS_PER_ROW]
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"{ACTION_VOTE}_{start}",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": f"{ACTION_VOTE}:{i}",
                            "text": {"type": "plain_text", "text": f"{i + 1}. {label}"[:75]},
                            "value": f"{poll.id}:{i}",
                        }
                        for i, label in chunk
                    ],
                }
            )

    tools: list[dict] = []
    if poll.show_results != SHOW_ALWAYS and open_now:
        tools.append(
            {
                "type": "button",
                "action_id": ACTION_RESULTS,
                "text": {"type": "plain_text", "text": "결과 보기"},
                "value": poll.id,
            }
        )
    if open_now:
        tools.append(
            {
                "type": "button",
                "action_id": ACTION_CLOSE,
                "style": "danger",
                "text": {"type": "plain_text", "text": "마감하기"},
                "value": poll.id,
            }
        )
    if tools:
        blocks.append({"type": "actions", "block_id": "tybot_poll_tools", "elements": tools})

    footer = (
        f"참여 {poll.voter_count}명"
        if poll.anonymous
        else f"참여 {poll.voter_count}명 · 선택을 다시 누르면 취소됩니다"
    )
    if not open_now:
        footer = f"참여 {poll.voter_count}명 · 마감된 투표입니다"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def fallback_text(poll: Poll) -> str:
    """알림·검색 결과에 보이는 한 줄. Slack 은 blocks 만 있으면 빈 알림을 보낸다."""
    return f"[투표] {poll.question}"


def private_results(poll: Poll, user_id: str, *, now: datetime | None = None) -> str:
    """`결과 보기` 를 누른 사람에게만 보내는 문구."""
    if not poll.may_see_results(user_id, now=now):
        if poll.show_results == SHOW_AFTER_VOTE:
            return "먼저 투표하면 결과를 볼 수 있습니다."
        return "결과는 마감 후에 공개됩니다."
    mine = poll.selection(user_id)
    picked = (
        "내가 고른 항목: " + ", ".join(poll.options[i] for i in mine if i < len(poll.options))
        if mine
        else "아직 투표하지 않았습니다."
    )
    return f"*{poll.question}*\n" + "\n".join(results_lines(poll, now=now)) + f"\n\n_{picked}_"


def help_text() -> str:
    return (
        "*투표 만들기*\n"
        "• `/투표` — 투표 만들기 화면이 열립니다\n"
        "• `/투표 회식 언제 할까요?` — 질문을 미리 채워서 화면을 엽니다\n"
        "• `/투표 도움말` — 이 안내\n\n"
        "*고를 수 있는 옵션*\n"
        "• *여러 개 선택* — 하나만 고르게 할지, 여러 개 고르게 할지\n"
        "• *익명 투표* — 누가 무엇을 골랐는지 아무도 볼 수 없습니다\n"
        "• *변경 불가* — 한 번 고르면 바꿀 수 없게 합니다\n"
        "• *결과 공개 시점* — 항상 / 투표한 사람에게만 / 마감 후에만\n"
        "• *마감* — 30분부터 1주까지, 또는 직접 마감\n\n"
        "투표는 만든 사람이 마감할 수 있습니다. 마감된 투표는 결과만 남습니다."
    )
