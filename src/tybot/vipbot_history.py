"""Load VIPBot follow-up coordinates without reusing prior model output."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .audit import KST
from .evidence_refs import attachment_refs_from_json, refs_from_json

VIP_HISTORY_DAYS = 30
VIP_HISTORY_TURNS = 20


def _recorded_at(value: object) -> datetime | None:
    try:
        recorded = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=KST)
    return recorded


def _month_paths(root: Path, start: datetime, end: datetime) -> list[Path]:
    year, month = start.year, start.month
    stop = (end.year, end.month)
    paths: list[Path] = []
    while (year, month) <= stop:
        paths.append(root / f"qa-{year:04d}-{month:02d}.jsonl")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return paths


def _strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()][:limit]


def load_vip_history(
    root: Path | str,
    *,
    workspace: str,
    slack_user: str,
    now: datetime | None = None,
    days: int = VIP_HISTORY_DAYS,
    limit: int = VIP_HISTORY_TURNS,
) -> list[dict]:
    """Return prior questions and reopenable coordinates for one VIP caller.

    The previous answer is deliberately absent. A later stage must reopen the
    returned references with the caller's current managed VIP source set.
    """
    if not workspace or not slack_user or days < 1 or limit < 1:
        return []
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    cutoff = current - timedelta(days=days)
    rows: list[tuple[datetime, dict]] = []
    try:
        for path in _month_paths(Path(root), cutoff, current):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("workspace") != workspace or row.get("user") != slack_user:
                    continue
                recorded = _recorded_at(row.get("ts"))
                if recorded is None or recorded < cutoff or recorded > current:
                    continue
                refs = refs_from_json(row.get("evidence_refs"))
                attachments = attachment_refs_from_json(row.get("attachment_refs"))
                if not refs and not attachments:
                    continue
                rows.append(
                    (
                        recorded,
                        {
                            "record_id": str(row.get("record_id") or ""),
                            "ts": str(row.get("ts") or ""),
                            "question": str(row.get("question") or ""),
                            "intent_kind": str(row.get("intent_kind") or ""),
                            "subject_terms": _strings(row.get("subject_terms"), limit=12),
                            "evidence_refs": refs,
                            "attachment_refs": attachments,
                            "context_parent_ids": _strings(row.get("context_parent_ids"), limit=8),
                        },
                    )
                )
    except OSError:
        return []
    rows.sort(key=lambda item: item[0])
    return [row for _, row in rows[-limit:]]


__all__ = ["VIP_HISTORY_DAYS", "VIP_HISTORY_TURNS", "load_vip_history"]
