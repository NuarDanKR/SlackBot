"""콘솔 질문·답변 원본 조회.

QA 기록은 감사 자료이며 아카이브가 아니다. 이 모듈은 읽기만 하고, 여기서 읽은 봇 답변을
`ArchiveStore`나 다음 답변의 근거로 넘기는 API를 만들지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date

from ..feedback import event_id
from . import health, reader
from .auth import ConsoleUser

logger = logging.getLogger("tybot.console.answer_records")


@dataclass(frozen=True)
class ReviewScope:
    own_users: frozenset[tuple[str, str]] = frozenset()
    review_channels: frozenset[tuple[str, str]] = frozenset()


def review_scope(email: str) -> ReviewScope:
    """콘솔 이메일을 Slack 본인 ID와 담당 검토 채널로 바꾼다.

    매핑을 확인할 수 없으면 빈 범위를 돌려준다. 이름이나 워크스페이스 담당 권한만 보고
    질문 본문을 열어 주지 않는다.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url or not email:
        return ReviewScope()
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                select distinct i.workspace, i.slack_user
                  from user_identity i
                  join employee e on e.emp_no = i.emp_no and e.active
                 where lower(btrim(e.email)) = lower(btrim(%s))
                """,
                (email,),
            )
            own = frozenset((str(row[0]), str(row[1])) for row in cur.fetchall())
            if not own:
                return ReviewScope()
            cur.execute(
                """
                select distinct cr.workspace, cr.channel_id
                  from channel_reviewer cr
                  join user_identity i
                    on i.workspace = cr.workspace
                   and i.slack_user = cr.reviewer_user
                  join employee e on e.emp_no = i.emp_no and e.active
                 where cr.enabled
                   and lower(btrim(e.email)) = lower(btrim(%s))
                """,
                (email,),
            )
            channels = frozenset((str(row[0]), str(row[1])) for row in cur.fetchall())
            return ReviewScope(own_users=own, review_channels=channels)
    except Exception as exc:  # noqa: BLE001 - 조회 실패 시 권한을 넓히면 안 된다
        logger.warning("검토자 질문 범위를 확인하지 못했습니다: %s", exc)
        return ReviewScope()


def record_key(row: dict) -> str:
    existing = str(row.get("record_id") or "").strip().lower()
    if existing and all(char in "0123456789abcdef" for char in existing):
        return existing
    identity = "\0".join(
        str(row.get(key) or "")
        for key in ("ts", "workspace", "channel_id", "user", "request_ts")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _all_records() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(reader.qa_log_dir().glob("qa-*.jsonl")):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        except OSError as exc:
            logger.warning("질문·답변 기록을 읽지 못했습니다 (%s): %s", path, exc)
    return rows


def _visible(row: dict, user: ConsoleUser, scope: ReviewScope) -> bool:
    workspace = str(row.get("workspace") or "")
    if user.is_admin:
        return user.may_see(workspace)
    slack_user = str(row.get("user") or "")
    channel_id = str(row.get("channel_id") or "")
    return (workspace, slack_user) in scope.own_users or (
        bool(channel_id) and (workspace, channel_id) in scope.review_channels
    )


def visible_records(
    user: ConsoleUser,
    *,
    start: date,
    end: date,
    workspace: str = "",
    result: str = "",
    asker: str = "",
) -> tuple[list[dict], ReviewScope]:
    scope = ReviewScope() if user.is_admin else review_scope(user.email)
    rows = [
        row
        for row in _all_records()
        if start.isoformat() <= str(row.get("ts") or "")[:10] <= end.isoformat()
        and _visible(row, user, scope)
    ]
    if workspace:
        rows = [row for row in rows if str(row.get("workspace") or "") == workspace]
    if result == "slow":
        rows = [row for row in rows if int(row.get("elapsed_ms") or 0) > health.SLOW_MS]
    elif result and result != "feedback":
        rows = [
            row
            for row in rows
            if str(row.get("reason") or "") == result
            or str(row.get("intent_kind") or "") == result
        ]
    if asker:
        needle = asker.casefold()
        rows = [
            row
            for row in rows
            if needle in str(row.get("user_name") or row.get("user") or "").casefold()
        ]
    rows.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return rows, scope


def quality_reasons(row: dict) -> list[str]:
    reasons: list[str] = []
    if str(row.get("error") or "").strip():
        reasons.append("error")
    if int(row.get("hits") or 0) == 0:
        reasons.append("no_hits")
    if int(row.get("elapsed_ms") or 0) > health.SLOW_MS:
        reasons.append("slow")
    return reasons


def feedback_rows(days: int = 366) -> list[dict]:
    return health._read_feedback(days, None)


def summaries(rows: list[dict], feedback: list[dict]) -> list[dict]:
    feedback_ids = {
        str(item.get("qa_record_id") or "")
        for item in feedback
        if item.get("action") in {"added", "submitted"}
    }
    return [
        {
            "recordKey": record_key(row),
            "at": str(row.get("ts") or ""),
            "workspace": str(row.get("workspace") or ""),
            "channel": str(row.get("channel") or row.get("channel_id") or ""),
            "asker": str(row.get("user_name") or row.get("user") or "unknown"),
            "intent": str(row.get("intent_kind") or ""),
            "source": str(row.get("intent_source") or ""),
            "reason": str(row.get("reason") or ""),
            "hits": int(row.get("hits") or 0),
            "model": str(row.get("model") or "-"),
            "costUsd": float(row.get("cost_usd") or 0),
            "ms": int(row.get("elapsed_ms") or 0),
            "qualityReasons": quality_reasons(row),
            "hasFeedback": str(row.get("record_id") or "") in feedback_ids,
        }
        for row in rows
    ]


def summary(rows: list[dict]) -> dict:
    count = len(rows)
    grounded = sum(
        1
        for row in rows
        if int(row.get("hits") or 0) > 0 and not str(row.get("error") or "").strip()
    )
    return {
        "questions": count,
        "grounded": grounded,
        "noHits": sum(1 for row in rows if int(row.get("hits") or 0) == 0),
        "groundedRate": grounded / count if count else None,
        "errors": sum(1 for row in rows if str(row.get("error") or "").strip()),
        "slowAnswers": sum(
            1 for row in rows if int(row.get("elapsed_ms") or 0) > health.SLOW_MS
        ),
        "spentUsd": round(sum(float(row.get("cost_usd") or 0) for row in rows), 6),
    }


def find_visible(user: ConsoleUser, key: str) -> tuple[dict | None, ReviewScope]:
    scope = ReviewScope() if user.is_admin else review_scope(user.email)
    for row in _all_records():
        if record_key(row) == key and _visible(row, user, scope):
            return row, scope
    return None, scope


def detail(row: dict, feedback: list[dict]) -> dict:
    raw_id = str(row.get("record_id") or "")
    events = [item for item in feedback if str(item.get("qa_record_id") or "") == raw_id]
    resolved = {
        str(item.get("target") or ""): item
        for item in feedback
        if item.get("action") == "resolved" and item.get("target")
    }
    visible_feedback = []
    for item in events:
        if item.get("action") not in {"added", "submitted"}:
            continue
        eid = event_id(item)
        done = resolved.get(eid)
        visible_feedback.append(
            {
                "id": eid,
                "at": str(item.get("at") or ""),
                "actor": str(item.get("actor") or ""),
                "kind": str(item.get("kind") or ""),
                "text": str(item.get("text") or ""),
                "handled": done is not None,
                "handledBy": str((done or {}).get("actor") or ""),
                "handledNote": str((done or {}).get("text") or ""),
            }
        )
    return {
        "recordKey": record_key(row),
        "at": str(row.get("ts") or ""),
        "workspace": str(row.get("workspace") or ""),
        "channel": str(row.get("channel") or ""),
        "channelId": str(row.get("channel_id") or ""),
        "userId": str(row.get("user") or ""),
        "asker": str(row.get("user_name") or row.get("user") or "unknown"),
        "question": str(row.get("question") or ""),
        "answer": str(row.get("answer") or ""),
        "intent": str(row.get("intent_kind") or ""),
        "source": str(row.get("intent_source") or ""),
        "reason": str(row.get("reason") or ""),
        "scope": str(row.get("scope") or ""),
        "hits": int(row.get("hits") or 0),
        "citations": list(row.get("citations") or []),
        "model": str(row.get("model") or "-"),
        "costUsd": float(row.get("cost_usd") or 0),
        "ms": int(row.get("elapsed_ms") or 0),
        "error": str(row.get("error") or ""),
        "requestTs": str(row.get("request_ts") or ""),
        "responseTs": str(row.get("response_ts") or ""),
        "qualityReasons": quality_reasons(row),
        "feedback": visible_feedback,
    }
