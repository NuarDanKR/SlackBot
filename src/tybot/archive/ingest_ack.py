"""수집 ACK — 「방금 올린 것이 검색되나」 에 답하는 **유일한 근거.**

결정: 2026-09-25 오너 §6, 2026-09-25 후속 지시 1~8.
스키마: `archive_ingest_state` (`deploy/sql/archiving_schema.sql`).

## 왜 필요한가

본문은 즉시 쓰이고 첨부 변환은 큐를 지난다. 그래서 「올렸습니다」 와 「검색됩니다」
사이에 **시간이 있다.** 그 사이를 모른 채 답하면 두 방향으로 틀린다 — 본문은 이미
있는데 「아직」 이라고 하거나, 첨부가 아직인데 「됐다」 고 한다.

뒤쪽이 더 나쁘다. 사람이 찾으러 갔다가 못 찾으면 **봇이 거짓말한 것**이 되고,
그 다음부터는 맞는 답도 확인하러 간다.

## 앞으로만 간다

같은 Slack 이벤트가 두 번 와도 상태가 뒤로 가지 않는다. Slack 은 재전달을 한다 —
`ready` 인 메시지에 `received` 가 다시 오면 그것은 **새 사실이 아니라 같은 사실의
재방송**이다. 뒤로 보내면 그 순간 「검색된다」 가 「아직」 으로 바뀌고, 사람은
방금 본 것이 사라졌다고 읽는다.

판정은 `archiving_state.plan_ingest_change` 가 한다. 여기서 다시 규칙을 쓰지
않는다 — 두 곳에 있으면 한 곳만 고치는 날이 온다.

## 모르면 「된다」 고 하지 않는다

행이 없거나 DB 를 못 보면 **검색 가능하다고 말하지 않는다.** 없는 것을 「아직
안 됐다」 로도 말하지 않는다 — 그것도 모르면서 아는 척이다.
"""

from __future__ import annotations

import logging
import os

from .archiving_state import (
    IngestProgress,
    IngestState,
    TransitionRefused,
    searchable_claim,
)

log = logging.getLogger("tybot.archive.ingest_ack")

#: 상태를 못 봤을 때 하는 말. **검색 가능 여부를 말하지 않는다.**
UNKNOWN_CLAIM = "수집 상태를 확인하지 못했습니다. 검색 가능 여부는 말씀드릴 수 없습니다."


def enabled() -> bool:
    """DB 가 없으면 ACK 를 기록하지 않는다.

    개발 PC 와 시험에서 수집 자체는 돌아야 한다. 기록을 못 한다고 수집을 멈추면
    **놓친 원본은 되돌릴 수 없다** — Slack 백필은 분당 1요청이라 사실상 복구가
    안 된다. 반대로 ACK 는 나중에 다시 만들 수 있다.
    """
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    from ..console.workspace_store import _connect as connect

    return connect()


def read(workspace: str, channel_id: str, message_ts: str) -> IngestProgress | None:
    """지금 상태. 행이 없으면 `None` — **「아직」 과 구분한다.**"""
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT state, attachment_total, attachment_ready
                  FROM archive_ingest_state
                 WHERE workspace = %s AND channel_id = %s AND message_ts = %s
                """,
                (workspace, channel_id, message_ts),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - 못 보는 것과 없는 것을 같은 값으로 돌려준다
        log.warning("수집 상태를 읽지 못했다 ws=%s ch=%s", workspace, channel_id)
        return None
    if row is None:
        return None
    return IngestProgress(
        IngestState(str(row["state"])),
        attachment_total=int(row["attachment_total"] or 0),
        attachment_ready=int(row["attachment_ready"] or 0),
    )


def advance(
    *,
    workspace: str,
    channel_id: str,
    message_ts: str,
    target: IngestState,
    attachment_total: int | None = None,
    attachment_ready: int | None = None,
    written_to: str = "",
    doc_path: str = "",
    error_code: str = "",
) -> IngestState | None:
    """상태를 앞으로 민다. 못 가면 **지금 상태를 그대로** 돌려준다.

    예외를 던지지 않는 이유: 재전달은 오류가 아니라 정상이다. 같은 이벤트가 두
    번 와서 `ready → raw_written` 이 시도되면, 그건 버그가 아니라 Slack 이 한 번
    더 보낸 것이다. 던지면 수집 루프가 그때마다 예외를 먹는다.

    `None` 은 **기록하지 못했다** 다(DB 없음·오류). 「상태가 없다」 와 다르다 —
    호출부가 「기록됐다」 고 착각하지 않게 구분한다.
    """
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            # 같은 메시지에 두 경로가 동시에 닿을 수 있다(실시간·백필·재전달).
            # 좌표별로 잠가야 「읽고 판정하고 쓰는」 사이가 벌어지지 않는다.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (f"{workspace}\x1f{channel_id}", message_ts),
            )
            cur.execute(
                """
                SELECT state, attachment_total, attachment_ready
                  FROM archive_ingest_state
                 WHERE workspace = %s AND channel_id = %s AND message_ts = %s
                 FOR UPDATE
                """,
                (workspace, channel_id, message_ts),
            )
            row = cur.fetchone()
            current = (
                IngestProgress(
                    IngestState(str(row["state"])),
                    int(row["attachment_total"] or 0),
                    int(row["attachment_ready"] or 0),
                )
                if row
                else None
            )
            plan = _next(current, target, attachment_total, attachment_ready)
            if plan is None:
                return current.state if current else None

            cur.execute(
                """
                INSERT INTO archive_ingest_state
                    (workspace, channel_id, message_ts, state,
                     attachment_total, attachment_ready, written_to, doc_path, error_code)
                VALUES (%(workspace)s, %(channel_id)s, %(message_ts)s, %(state)s,
                        %(total)s, %(ready)s, %(written_to)s, %(doc_path)s, %(error_code)s)
                ON CONFLICT (workspace, channel_id, message_ts) DO UPDATE SET
                    state = excluded.state,
                    attachment_total = excluded.attachment_total,
                    attachment_ready = excluded.attachment_ready,
                    written_to = CASE WHEN excluded.written_to <> '' THEN excluded.written_to
                                      ELSE archive_ingest_state.written_to END,
                    doc_path = CASE WHEN excluded.doc_path <> '' THEN excluded.doc_path
                                    ELSE archive_ingest_state.doc_path END,
                    error_code = excluded.error_code,
                    updated_at = now()
                """,
                {
                    "workspace": workspace,
                    "channel_id": channel_id,
                    "message_ts": message_ts,
                    "state": str(plan.state),
                    "total": plan.attachment_total,
                    "ready": plan.attachment_ready,
                    "written_to": written_to,
                    "doc_path": doc_path,
                    "error_code": error_code,
                },
            )
            return plan.state
    except Exception:
        log.exception("수집 상태를 기록하지 못했다 ws=%s ch=%s", workspace, channel_id)
        return None


def _next(
    current: IngestProgress | None,
    target: IngestState,
    attachment_total: int | None,
    attachment_ready: int | None,
) -> IngestProgress | None:
    """다음 상태. **갈 수 없으면 `None`** (재전달이므로 조용히 둔다).

    첨부 수는 「준 것만」 바꾼다. 안 주면 지금 값을 유지한다 — 본문 경로가 첨부
    수를 0 으로 덮으면, 첨부가 아직인데 `ready` 로 갈 수 있게 된다.
    """
    base = current or IngestProgress(IngestState.RECEIVED)
    total = base.attachment_total if attachment_total is None else attachment_total
    ready = base.attachment_ready if attachment_ready is None else attachment_ready
    counted = IngestProgress(base.state, total, max(0, min(ready, total)))

    if current is None:
        # 첫 기록. `received` 가 아니면 그 앞을 지나왔다는 뜻이므로 그대로 둔다 —
        # 지어낸 중간 단계를 쌓지 않는다.
        return IngestProgress(target, counted.attachment_total, counted.attachment_ready)
    if target == counted.state:
        # 같은 상태로 다시 왔다. 첨부 수만 늘 수 있다(변환이 하나 더 끝난 경우).
        return counted if counted != current else None
    try:
        return plan_ingest(counted, target)
    except TransitionRefused:
        return None


def plan_ingest(current: IngestProgress, target: IngestState) -> IngestProgress:
    """전이 판정은 `archiving_state` 가 한다. 여기서 규칙을 다시 쓰지 않는다."""
    from .archiving_state import plan_ingest_change

    return plan_ingest_change(current, target)


# ---------------------------------------------------------------------------
# Master 가 답하기 전에 부르는 자리
# ---------------------------------------------------------------------------


def claim(
    workspace: str, channel_id: str, message_ts: str, *, require_ack: bool = True
) -> str:
    """사람에게 할 말. **여기를 거치지 않고 「검색 가능」 을 말하지 않는다.**

    문구를 한 자리에서 만드는 이유: 호출부마다 쓰면 한 군데가 「올렸습니다」 를
    「검색됩니다」 로 적고, 그게 제일 안 들킨다.

    상태를 못 보면 `UNKNOWN_CLAIM` 이다. 「아직 안 됐다」 로 말하지 않는다 —
    그것도 모르면서 아는 척이고, 사람이 기다리지 않아도 될 것을 기다리게 된다.
    """
    progress = read(workspace, channel_id, message_ts)
    if progress is None:
        return UNKNOWN_CLAIM
    return searchable_claim(progress, require_ack=require_ack)


def is_searchable(workspace: str, channel_id: str, message_ts: str) -> bool:
    """검색된다고 단언해도 되는가. **모르면 `False`.**"""
    progress = read(workspace, channel_id, message_ts)
    return progress is not None and progress.state == IngestState.READY
