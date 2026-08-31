"""답변 품질 피드백 기록.

피드백은 감사 기록의 답변 ID만 참조한다. 아카이브 원문에 넣지 않으며 검색 근거로도 쓰지 않는다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("tybot.feedback")
KST = timezone(timedelta(hours=9))
MAX_CORRECTION = 2000
REACTIONS = {"+1": "positive", "thumbsup": "positive", "-1": "negative", "thumbsdown": "negative"}

# 신고 종류. `missing`(근거를 못 찾음)을 `negative`(틀린 답)와 나누는 이유:
# 근거를 못 찾은 것은 수집·검색 문제이고, 틀린 답은 생성 문제다. 조치하는 사람이 다르다.
KINDS = frozenset({"positive", "negative", "correction", "missing"})

# 모달의 선택지. value 가 그대로 kind 가 된다.
KIND_CHOICES = (
    ("positive", "🟢 정확했다"),
    ("negative", "🔴 틀렸다"),
    ("missing", "🟡 근거를 못 찾았다"),
)


@dataclass(frozen=True)
class FeedbackEvent:
    at: str
    workspace: str
    channel_id: str
    qa_record_id: str
    answer_ts: str
    actor: str
    kind: str  # positive | negative | correction | missing
    action: str  # added | removed | submitted
    text: str = ""


class FeedbackLog:
    """월별 append-only JSONL. 답변 내용은 QA 로그에만 두고 여기서는 ID로 연결한다."""

    def __init__(self, qa_root: Path | str) -> None:
        self.root = Path(qa_root)

    def _path(self, at: str) -> Path:
        return self.root / f"feedback-{at[:7]}.jsonl"

    def write(
        self,
        *,
        workspace: str,
        channel_id: str,
        qa_record_id: str,
        answer_ts: str,
        actor: str,
        kind: str,
        action: str,
        text: str = "",
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"지원하지 않는 피드백 종류: {kind}")
        if action not in {"added", "removed", "submitted"}:
            raise ValueError(f"지원하지 않는 피드백 동작: {action}")
        at = datetime.now(KST).isoformat(timespec="seconds")
        event = FeedbackEvent(
            at=at,
            workspace=workspace,
            channel_id=channel_id,
            qa_record_id=qa_record_id,
            answer_ts=answer_ts,
            actor=actor,
            kind=kind,
            action=action,
            text=text.strip()[:MAX_CORRECTION],
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._path(at).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("답변 피드백 기록 실패: %s", e)


def reaction_kind(name: str) -> str | None:
    return REACTIONS.get((name or "").strip().lower())


def correction_text(text: str) -> str | None:
    """`정정: 실제 내용`에서 사람이 쓴 정정 내용만 꺼낸다."""
    value = (text or "").strip()
    if not value.startswith("정정"):
        return None
    value = value[2:].lstrip(" \t:：-—")
    return value or ""


# `/피드백` 안내. 무인수로 실행하면 모달이 열리므로, 이 문구는 '한 줄로 바로 보내는'
# 빠른 경로를 알려 주는 역할이다.
SLASH_HELP = (
    "`/피드백` 만 입력하면 선택 화면이 열립니다(가장 빠릅니다).\n"
    "바로 적어 보내려면: `/피드백 <무엇이 잘못됐는지>`\n"
    "예) `/피드백 김해외동 기성금을 물었는데 다른 현장 내용이 나왔어요`\n"
    "\n"
    "답변에 직접 표시하려면 그 답변에 :+1: / :-1: 를 누르거나, "
    "답변 스레드에서 `@{bot} 정정: 올바른 내용` 이라고 적어도 됩니다.\n"
    "신고 내용은 아카이브에 저장되지 않고 답변 근거로도 쓰이지 않습니다."
)

def feedback_modal(private_metadata: str, *, target: str = "") -> dict:
    """`/피드백` 무인수 실행 시 여는 모달.

    이것이 주 입구다. 리액션(:+1:/:-1:)은 남겨 두지만, 답변 메시지에 마우스를 올려
    이모지를 찾는 동작이 **모바일에서 특히 번거롭다**. 명령 한 번에 선택지가 뜨는 쪽이
    실제로 더 많이 쓰인다.

    `target` 은 어떤 답변에 붙는지 사람이 확인할 수 있게 보여주는 문구다.
    """
    blocks: list[dict] = []
    if target:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"대상: {target}"}],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "이 채널에서 회원님이 받은 답변 기록을 찾지 못했습니다. "
                        "특정 답변에 연결하지 않고 접수합니다.",
            }],
        })
    blocks.append({
        "type": "input",
        "block_id": "kind",
        "label": {"type": "plain_text", "text": "답변이 어땠습니까?"},
        "element": {
            "type": "radio_buttons",
            "action_id": "kind",
            "options": [
                {"text": {"type": "plain_text", "text": label}, "value": value}
                for value, label in KIND_CHOICES
            ],
        },
    })
    blocks.append({
        "type": "input",
        "block_id": "detail",
        "optional": True,
        "label": {"type": "plain_text", "text": "무엇이 어땠는지 (선택)"},
        "element": {
            "type": "plain_text_input",
            "action_id": "detail",
            "multiline": True,
            "max_length": MAX_CORRECTION,
            "placeholder": {
                "type": "plain_text",
                "text": "예) 김해외동을 물었는데 다른 현장 내용이 나왔습니다",
            },
        },
    })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "신고 내용은 아카이브에 저장되지 않고 답변 근거로도 쓰이지 않습니다.",
        }],
    })
    return {
        "type": "modal",
        "callback_id": "tybot_feedback",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "답변 피드백"},
        "submit": {"type": "plain_text", "text": "보내기"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }


def from_view(view: dict) -> tuple[str, str]:
    """모달 제출에서 (kind, text) 를 꺼낸다. 알 수 없는 값은 negative 로 둔다.

    모르는 값을 조용히 positive 로 두면 문제 신고가 칭찬으로 집계된다.
    """
    state = (view.get("state") or {}).get("values") or {}
    picked = (
        state.get("kind", {}).get("kind", {}).get("selected_option") or {}
    ).get("value")
    kind = picked if picked in KINDS else "negative"
    text = (state.get("detail", {}).get("detail", {}).get("value") or "").strip()
    return kind, text[:MAX_CORRECTION]


def thanks(kind: str, *, linked: str = "") -> str:
    """접수 확인 문구. 무엇으로 접수됐는지 사람이 알 수 있어야 한다."""
    # 조사를 포함해 둔다 - "사례으로" 같은 어색한 문장이 사용자에게 그대로 나간다.
    label = {
        "positive": "정확했다는 의견으로",
        "negative": "틀린 답변 신고로",
        "missing": "근거를 못 찾은 사례로",
        "correction": "정정 의견으로",
    }.get(kind, "의견으로")
    lines = [f"{label} 접수했습니다. 답변 품질 검토에 반영하겠습니다."]
    lines.append(
        linked
        or "이 채널에서 회원님이 받은 답변 기록을 찾지 못해 특정 답변에 연결하지는 못했습니다."
    )
    lines.append("신고 내용은 아카이브에 저장되지 않으며 답변 근거로도 쓰이지 않습니다.")
    return "\n".join(lines)
