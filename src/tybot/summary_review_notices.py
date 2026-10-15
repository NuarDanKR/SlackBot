"""Review status DMs are delivery records, never approval authority."""
from __future__ import annotations

import logging
from datetime import datetime

from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

MESSAGES = {
    "no-new-source": "지난 처리 이후 새로 수집된 원문이 없습니다. Slack 전체 내역을 확인했다는 뜻은 아닙니다.",
    "model-empty": "확인한 원문에서 검토할 요약 후보가 없습니다.",
    "no-accepted-candidate": "요약 후보가 원문 검증을 통과하지 못했습니다. 정상적인 요약 0건이 아닙니다.",
    "generate-failed": "요약 후보 생성에 실패했습니다. 정상적인 요약 0건이 아니며 재시도가 필요합니다.",
    "generation-retry-deferred": "이전 요약 생성 실패 후 재시도를 기다리고 있습니다.",
}
ERROR_REASONS = frozenset({
    "no-accepted-candidate", "generate-failed", "generation-retry-deferred",
})


class NoticeStore:
    def __init__(self, conn):
        self.conn = conn

    def claim(self, workspace, channel_id, recipient, on, category, reason) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_status_delivery
                   (workspace,channel_id,recipient,review_date,category,reason,state)
                VALUES (%s,%s,%s,%s,%s,%s,'sending')
                ON CONFLICT (workspace,channel_id,recipient,review_date,category)
                DO UPDATE SET state='sending', reason=excluded.reason, updated_at=now()
                WHERE summary_review_status_delivery.state='failed'
                  AND summary_review_status_delivery.updated_at < now()-interval '1 hour'
                RETURNING workspace""",
                (workspace, channel_id, recipient, on, category, reason),
            )
            claimed = cur.fetchone() is not None
        self.conn.commit()
        return claimed

    def finish(self, workspace, channel_id, recipient, on, category, state) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_status_delivery SET state=%s,updated_at=now()
                WHERE workspace=%s AND channel_id=%s AND recipient=%s
                  AND review_date=%s AND category=%s AND state='sending'""",
                (state, workspace, channel_id, recipient, on, category),
            )
        self.conn.commit()


def send_status(conn, client, *, workspace: str, channel_id: str, label: str,
                recipients: list[str], reason: str, now: datetime,
                store=None) -> tuple[int, int]:
    """At most one empty and one error notice per recipient/channel/KST day."""
    if reason not in MESSAGES:
        return 0, 0
    if client is None:
        log.warning("검토 상태 DM sender 없음 ws=%s ch=%s", workspace, channel_id)
        return 0, len(set(recipients))
    from .summary_review import KST

    on = now.astimezone(KST).date()
    category = "error" if reason in ERROR_REASONS else "empty"
    ledger = store if store is not None else NoticeStore(conn)
    sent = failed = 0
    for recipient in sorted(set(recipients)):
        posting = False
        claimed = False
        try:
            claimed = ledger.claim(workspace, channel_id, recipient, on, category, reason)
            if not claimed:
                continue
            opened = client.conversations_open(users=recipient)
            dm = (opened.get("channel") or {}).get("id")
            if not dm:
                raise ValueError("missing DM channel")
            text = f"{label}\n{on.isoformat()} 요약 검토 상태\n{MESSAGES[reason]}"
            posting = True
            reply = client.chat_postMessage(
                channel=dm, text="요약 검토 상태",
                blocks=[{"type": "section", "text": {"type": "plain_text", "text": text}}],
            )
            if not reply or not reply.get("ts"):
                raise ValueError("missing message timestamp")
            ledger.finish(workspace, channel_id, recipient, on, category, "sent")
            sent += 1
        except Exception as exc:  # noqa: BLE001 - An uncertain send must not be retried automatically.
            conn.rollback()
            if claimed:
                state = "ambiguous" if posting and not isinstance(exc, SlackApiError) else "failed"
                try:
                    ledger.finish(workspace, channel_id, recipient, on, category, state)
                except Exception:  # noqa: BLE001 - Keep the durable sending record on DB failure.
                    conn.rollback()
                    # A durable 'sending' record also prevents an unsafe retry.
            failed += 1
            log.warning(
                "검토 상태 DM 실패 ws=%s ch=%s reason=%s code=%s",
                workspace, channel_id, reason, type(exc).__name__,
            )
    return sent, failed
