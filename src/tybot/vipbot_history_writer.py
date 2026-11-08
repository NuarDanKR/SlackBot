"""Append VIPBot follow-up coordinates without persisting model output."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from .audit import KST
from .evidence_refs import attachment_refs_to_json, refs_to_json

_WORKSPACE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_QUESTION_CHARS = 4_000


def _strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def record_vip_turn(
    root: Path | str,
    *,
    workspace: str,
    slack_user: str,
    question: str,
    answer,
    now: datetime | None = None,
) -> bool:
    """Record only a delivered question and its reopenable source coordinates.

    The model response and rendered citations are deliberately absent. A later
    question must reopen these coordinates through the caller's current VIP
    source set instead of treating a previous model sentence as evidence.
    """
    ws = str(workspace or "").strip().lower()
    user = str(slack_user or "").strip().upper()
    asked = str(question or "").strip()[:_QUESTION_CHARS]
    if not _WORKSPACE.fullmatch(ws) or not _SLACK_USER.fullmatch(user) or not asked:
        return False

    try:
        refs = refs_to_json(getattr(answer, "evidence_refs", ()))
        attachments = attachment_refs_to_json(getattr(answer, "attachment_refs", ()))
    except (AttributeError, TypeError, ValueError):
        return False
    if not refs and not attachments:
        return False

    recorded = now or datetime.now(KST)
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=KST)
    row = {
        "record_id": uuid.uuid4().hex,
        "ts": recorded.isoformat(),
        "workspace": ws,
        "user": user,
        "question": asked,
        "intent_kind": str(getattr(answer, "reason", "") or "")[:64],
        "subject_terms": _strings(getattr(answer, "subject_terms", ()), limit=12),
        "evidence_refs": refs,
        "attachment_refs": attachments,
        "context_parent_ids": _strings(
            getattr(answer, "context_parent_ids", ()), limit=8
        ),
    }
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    directory = Path(root)
    path = directory / f"qa-{recorded:%Y-%m}.jsonl"
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written < 1:
                    raise OSError("vip history append made no progress")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError:
        return False
    return True


__all__ = ["record_vip_turn"]
