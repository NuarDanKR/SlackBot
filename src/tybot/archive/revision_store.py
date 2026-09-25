"""Append message revision coordinates without storing message bodies in PostgreSQL."""
from __future__ import annotations

import hashlib

from ..console.workspace_store import _connect


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if body else ""


def record_revision(
    *,
    workspace: str,
    channel_id: str,
    message_ts: str,
    kind: str,
    body: str,
    edited_ts: str = "",
    author_id: str = "",
    doc_path: str = "",
    previous_body: str = "",
) -> int:
    """Append one idempotent revision and return its number."""
    digest = body_digest(body)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (f"{workspace}\x1f{channel_id}", message_ts),
        )
        cur.execute(
            """
            SELECT revision_no, kind, edited_ts, body_sha256
              FROM archive_message_revision
             WHERE workspace = %s AND channel_id = %s AND message_ts = %s
             ORDER BY revision_no DESC LIMIT 1
            """,
            (workspace, channel_id, message_ts),
        )
        latest = cur.fetchone()
        if latest and (
            str(latest["kind"]) == kind
            and str(latest["edited_ts"] or "") == edited_ts
            and str(latest["body_sha256"] or "") == digest
        ):
            return int(latest["revision_no"])

        next_no = int(latest["revision_no"]) + 1 if latest else 1
        if latest is None and kind != "create":
            cur.execute(
                """
                INSERT INTO archive_message_revision
                    (workspace, channel_id, message_ts, revision_no, kind,
                     author_id, body_sha256, doc_path)
                VALUES (%s, %s, %s, 1, 'create', %s, %s, %s)
                """,
                (
                    workspace,
                    channel_id,
                    message_ts,
                    author_id,
                    body_digest(previous_body),
                    doc_path,
                ),
            )
            next_no = 2

        cur.execute(
            """
            INSERT INTO archive_message_revision
                (workspace, channel_id, message_ts, revision_no, kind, edited_ts,
                 author_id, body_sha256, doc_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                workspace,
                channel_id,
                message_ts,
                next_no,
                kind,
                edited_ts,
                author_id,
                digest,
                doc_path,
            ),
        )
        return next_no
