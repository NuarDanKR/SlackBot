"""Persistent, body-free deduplication for Slack VIPBot events."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

STATE_UNCONFIGURED = "vipbot_event_state_unconfigured"
STATE_INVALID = "vipbot_event_state_invalid"
STATE_UNAVAILABLE = "vipbot_event_state_unavailable"
EVENT_ID_INVALID = "vipbot_event_id_invalid"

_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RETENTION_SECONDS = 30 * 24 * 60 * 60


class EventClaimError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def state_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    raw = str(values.get("STATE_DIRECTORY", "") or "").strip()
    if not raw:
        raise EventClaimError(STATE_UNCONFIGURED)
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise EventClaimError(STATE_INVALID)
    return root / "slack-events.sqlite3"


class EventClaims:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.is_absolute() or not self._path.parent.is_dir():
            raise EventClaimError(STATE_INVALID)

    def claim(self, event_id: str, *, now: int | None = None) -> bool:
        """Atomically claim one event ID; return false for a prior delivery attempt."""
        stable_id = str(event_id or "").strip()
        if _EVENT_ID.fullmatch(stable_id) is None:
            raise EventClaimError(EVENT_ID_INVALID)
        claimed_at = int(time.time() if now is None else now)
        if claimed_at < 0:
            raise EventClaimError(EVENT_ID_INVALID)

        try:
            with sqlite3.connect(self._path, timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS claimed_event (
                           event_id TEXT PRIMARY KEY,
                           claimed_at INTEGER NOT NULL CHECK (claimed_at >= 0)
                       )"""
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM claimed_event WHERE claimed_at < ?",
                    (max(0, claimed_at - _RETENTION_SECONDS),),
                )
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO claimed_event(event_id, claimed_at) VALUES (?, ?)",
                    (stable_id, claimed_at),
                ).rowcount
            return inserted == 1
        except (OSError, sqlite3.Error):
            raise EventClaimError(STATE_UNAVAILABLE) from None


__all__ = [
    "EVENT_ID_INVALID",
    "STATE_INVALID",
    "STATE_UNAVAILABLE",
    "STATE_UNCONFIGURED",
    "EventClaimError",
    "EventClaims",
    "state_path",
]
