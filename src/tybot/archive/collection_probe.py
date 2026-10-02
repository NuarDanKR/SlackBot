"""Read-only channel collection facts for TYBot's status commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db import connect

log = logging.getLogger("tybot.archive.collection_probe")


@dataclass(frozen=True)
class ArchiverChannel:
    mode: str
    membership: str
    writer_owner: str
    operator_hold: bool
    live_enabled: bool
    last_ack_state: str = ""
    last_ack_target: str = ""
    last_ack_at: str = ""
    attachment_issues: int = 0


@dataclass(frozen=True)
class Probe:
    checked: bool
    channel: ArchiverChannel | None = None


def load(workspace: str, channel_id: str) -> Probe:
    """A missing row means legacy Master ownership; a failed lookup does not."""
    try:
        with connect() as conn:
            if conn is None:
                return Probe(checked=False)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT mode, membership, writer_owner, operator_hold
                      FROM archive_channel_mode
                     WHERE workspace = %s AND channel_id = %s
                    """,
                    (workspace, channel_id),
                )
                row = cur.fetchone()
                if row is None:
                    return Probe(checked=True)
                cur.execute(
                    """
                    SELECT enabled FROM archive_feature_flag
                     WHERE scope = 'global' AND name = 'archiver_writes_live'
                    """
                )
                flag = cur.fetchone()
                cur.execute(
                    """
                    SELECT state, written_to, updated_at
                      FROM archive_ingest_state
                     WHERE workspace = %s AND channel_id = %s
                     ORDER BY updated_at DESC LIMIT 1
                    """,
                    (workspace, channel_id),
                )
                ack = cur.fetchone()
                cur.execute(
                    """
                    SELECT count(*) AS total FROM archive_ingest_state
                     WHERE workspace = %s AND channel_id = %s
                       AND state IN ('partial', 'failed')
                       AND attachment_ready < attachment_total
                    """,
                    (workspace, channel_id),
                )
                issues = cur.fetchone()
        return Probe(
            checked=True,
            channel=ArchiverChannel(
                mode=str(row["mode"]),
                membership=str(row["membership"]),
                writer_owner=str(row["writer_owner"]),
                operator_hold=bool(row["operator_hold"]),
                live_enabled=bool(flag and flag["enabled"]),
                last_ack_state=str(ack["state"] or "") if ack else "",
                last_ack_target=str(ack["written_to"] or "") if ack else "",
                last_ack_at=str(ack["updated_at"] or "") if ack else "",
                attachment_issues=int(issues["total"] or 0) if issues else 0,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - DB 장애를 정상 수집 상태로 표시하지 않는다
        log.warning("Archiver 수집 상태 조회 실패 ws=%s ch=%s: %s", workspace, channel_id, exc)
        return Probe(checked=False)
