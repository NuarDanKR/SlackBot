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
from datetime import UTC, date, datetime
from pathlib import Path

from ..answer_status import answer_failed, failure_code
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


#: 어느 봇의 감사기록에서 읽었는지 붙여 두는 키. 파일에는 없다 — 폴더가 곧 봇이다.
BOT_KEY = "_answer_bot"


def _record_dirs() -> list[tuple[str, Path]]:
    """(봇, 폴더). VIPBot 폴더가 TYBot 폴더와 같게 잘못 설정돼도 두 번 읽지 않는다."""
    ty = reader.qa_log_dir()
    vip = reader.vip_qa_log_dir()
    dirs = [("tybot", ty)]
    try:
        same = vip.resolve() == ty.resolve()
    except OSError:
        same = False
    if not same:
        dirs.append(("vipbot", vip))
    return dirs


def _all_records() -> list[dict]:
    """TYBot·VIPBot 감사기록 전부. 각 줄에 답변한 봇(`BOT_KEY`)을 붙인다."""
    rows: list[dict] = []
    for bot, directory in _record_dirs():
        for path in sorted(directory.glob("qa-*.jsonl")):
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            item[BOT_KEY] = bot
                            rows.append(item)
            except OSError as exc:
                logger.warning("질문·답변 기록을 읽지 못했습니다 (%s): %s", path, exc)
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    return rows


def answer_bot(row: dict) -> str:
    return str(row.get(BOT_KEY) or "tybot")


#: 본문을 가린 VIP 기록에서도 보여 주는 작업 기록 키 — 처리 경로만, 내용은 없다.
_TRACE_KEYS = (
    "task_index", "task_kind", "required_capability", "routing_confidence",
    "final_responder", "attempted_specialists", "result", "error_code",
)


def body_visible(row: dict, user: ConsoleUser) -> bool:
    """질문·답변 본문을 보여 줘도 되는가.

    VIPBot(경영진) 질문 본문은 **콘솔 관리자와 질문한 본인**만 본다. 검토자 등 그 밖의
    계정은 시각·모델·결과·근거 건수 같은 처리 기록만 본다. 관리자도 담당 워크스페이스
    밖의 기록은 애초에 열 수 없다(`find_visible`). 열람은 감사기록에 남는다.
    (2026-11-18 「본인만」 → 2026-11-19 「관리자 + 본인」, 오너 결정)

    본인 확인은 회사 이메일 → 사번 → Slack ID 매핑으로만 한다 — 확인이 안 되면 가린다.
    """
    if answer_bot(row) != "vipbot" or user.is_admin:
        return True
    own = review_scope(user.email).own_users
    return (str(row.get("workspace") or ""), str(row.get("user") or "")) in own


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
    bot: str = "",
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
    if bot:
        rows = [row for row in rows if answer_bot(row) == bot]
    if result == "slow":
        rows = [row for row in rows if int(row.get("elapsed_ms") or 0) > health.SLOW_MS]
    elif result == "error":
        rows = [row for row in rows if answer_result(row) == "failed"]
    elif result == "answered":
        rows = [row for row in rows if answer_result(row) == "answered"]
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


# 근거가 0건이지만 **자료 문제가 아닌** 사유. 운영자가 할 일이 전혀 다르다 —
# 자료를 찾으러 가는 것이 아니라 연결·권한을 본다. `no_hits` 로 접으면 그 구분이
# 화면에서 사라지고, 연결 장애가 「아카이브에 자료가 없다」 로 몇 주씩 남는다.
NOT_A_DATA_PROBLEM = {"permission_unverified"}


def answer_result(row: dict) -> str:
    """Separate successful Slack delivery from successful answer generation."""
    if answer_failed(row):
        return "failed"
    return "needs_review" if quality_reasons(row, include_failure=False) else "answered"


def quality_reasons(row: dict, *, include_failure: bool = True) -> list[str]:
    reasons: list[str] = []
    if include_failure and answer_result(row) == "failed":
        reasons.append("answer_failed")
    if str(row.get("error") or "").strip():
        reasons.append("error")
    reason = str(row.get("reason") or "")
    if reason in NOT_A_DATA_PROBLEM:
        reasons.append(reason)
    elif int(row.get("hits") or 0) == 0:
        reasons.append("no_hits")
    specialist_error = str(row.get("specialist_error_code") or "").strip()
    responder = str(row.get("final_responder") or "").strip()
    if specialist_error and responder not in {"", "none"}:
        reasons.append("specialist_fallback")
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
            "bot": answer_bot(row),
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
            "resultStatus": answer_result(row),
            "failureCode": failure_code(row),
            "hasFeedback": str(row.get("record_id") or "") in feedback_ids,
        }
        for row in rows
    ]


def summary(rows: list[dict]) -> dict:
    count = len(rows)
    grounded = sum(
        1
        for row in rows
        if int(row.get("hits") or 0) > 0 and not answer_failed(row)
    )
    return {
        "questions": count,
        "grounded": grounded,
        "noHits": sum(1 for row in rows if int(row.get("hits") or 0) == 0),
        "groundedRate": grounded / count if count else None,
        "errors": sum(1 for row in rows if answer_failed(row)),
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


def detail(row: dict, feedback: list[dict], *, show_body: bool = True) -> dict:
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
    out = {
        "recordKey": record_key(row),
        "bot": answer_bot(row),
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
        "decisionId": str(row.get("decision_id") or ""),
        "plannerModel": str(row.get("planner_model") or ""),
        "taskTraces": list(row.get("task_traces") or []),
        "qualityReasons": quality_reasons(row),
        "resultStatus": answer_result(row),
        "failureCode": failure_code(row),
        "finalResponder": str(row.get("final_responder") or ""),
        "specialistErrorCode": str(row.get("specialist_error_code") or ""),
        "feedback": visible_feedback,
    }
    out["bodyHidden"] = not show_body
    if not show_body:
        out.update(
            question="", answer="", citations=[],
            taskTraces=[{key: trace.get(key) for key in _TRACE_KEYS if key in trace}
                        for trace in out["taskTraces"] if isinstance(trace, dict)],
            feedback=[{**item, "text": "", "handledNote": ""} for item in out["feedback"]],
        )
    return out


#: 봇 분류 상태에서 전문 봇 없이 답한 질문의 결과 값.
DIRECT_RESULT = "direct"


def _parse_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def direct_answer_rows(
    *,
    exclude_keys: set[str],
    allowed: frozenset[str] | set[str] | None,
    bot: str = "",
    limit: int = 200,
) -> list[dict]:
    """전문 봇(Hermes)을 부르지 않고 봇이 **직접** 답한 질문 — 봇 분류 상태용.

    봇 분류 상태는 원래 전문 봇 호출(`specialist_call`)만 보였다. 그러면 VIPBot·TYBot 이
    스스로 답한 질문(짧은 질문, 이어 묻기 재검색, 조언 답변, 자료 없음 등)은 어느 봇이
    받았는지 그 화면에 남지 않는다. 감사기록(TYBot·VIPBot 폴더)에서 읽어, 이미 호출로
    잡힌 질문(`exclude_keys`)을 빼고 같은 모양으로 돌려준다. 본문은 담지 않는다.
    """
    out: list[dict] = []
    for row in reversed(_all_records()):
        key = record_key(row)
        if key in exclude_keys:
            continue
        workspace = str(row.get("workspace") or "")
        if allowed is not None and workspace not in allowed:
            continue
        answered_by = answer_bot(row)
        if bot and answered_by != bot:
            continue
        at = _parse_at(row.get("ts"))
        if at is None:
            continue
        intent = str(row.get("intent_kind") or "")
        source = str(row.get("intent_source") or "")
        model = str(row.get("model") or "-")
        reason = str(row.get("reason") or "")
        out.append({
            "id": f"qa:{key}",
            "at": at,
            "workspace": workspace,
            "specialist": "",
            "routingReason": " ".join(
                part for part in (
                    "direct",
                    f"intent={intent}" if intent else "",
                    f"source={source}" if source else "",
                    f"model={model}",
                    f"reason={reason}" if reason else "",
                ) if part
            ),
            "confidence": None,
            "result": DIRECT_RESULT,
            "answerResult": answer_result(row),
            "elapsedMs": int(row.get("elapsed_ms") or 0),
            "costUsd": float(row.get("cost_usd") or 0),
            "errorCode": failure_code(row),
            "qaRecordKey": key,
            "decisionId": "",
            "taskIndex": 0,
            "taskKind": intent,
            "requiredCapability": "",
            "bot": answered_by,
        })
        if len(out) >= limit:
            break
    return out
