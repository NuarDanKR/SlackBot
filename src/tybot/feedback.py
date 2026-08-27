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


@dataclass(frozen=True)
class FeedbackEvent:
    at: str
    workspace: str
    channel_id: str
    qa_record_id: str
    answer_ts: str
    actor: str
    kind: str  # positive | negative | correction
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
        if kind not in {"positive", "negative", "correction"}:
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
