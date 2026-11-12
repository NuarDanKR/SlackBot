"""Safe, local-only delivery queue for image-description completion notices.

The Vision worker intentionally has no Slack capability.  It can only append a
small, validated completion receipt here; the Archiver owns the later Slack
thread reply.  No model output, OCR transcript, file name, or source text is
ever placed in this queue.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from tybot.lock import AlreadyRunning, FileLock, LockUnavailable

_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CHANNEL = re.compile(r"^[CG][A-Z0-9]{8,}$")
_TS = re.compile(r"^\d{1,20}\.\d{1,9}$")
_JOB = re.compile(r"^[a-f0-9]{32}$")
_STATE = frozenset({"completed"})


@dataclass(frozen=True)
class ImageDescriptionNotice:
    job_id: str
    workspace: str
    channel_id: str
    message_ts: str
    state: str = "completed"

    def __post_init__(self) -> None:
        if not _JOB.fullmatch(self.job_id):
            raise ValueError("job_id_invalid")
        if not _WORKSPACE.fullmatch(self.workspace):
            raise ValueError("workspace_invalid")
        if not _CHANNEL.fullmatch(self.channel_id):
            raise ValueError("channel_invalid")
        if not _TS.fullmatch(self.message_ts):
            raise ValueError("message_ts_invalid")
        if self.state not in _STATE:
            raise ValueError("state_invalid")

    @property
    def client_msg_id(self) -> str:
        """Stable Slack idempotency key; never derived from source content."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tybot:image-notice:{self.job_id}"))

    @property
    def text(self) -> str:
        return "✅ 이미지 설명·텍스트 인식 완료 — 검색 보조 정보로 반영되었습니다."


def _base(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot")


def outbox_path(workspace: str, *, root: Path | str | None = None) -> Path:
    if not _WORKSPACE.fullmatch(workspace):
        raise ValueError("workspace_invalid")
    return _base(root) / "image-description-notify" / f"{workspace}.jsonl"


def _lock(path: Path) -> FileLock:
    return FileLock(path.with_suffix(".lock"), label=f"image notice outbox {path.stem}")


def _decode(line: str) -> ImageDescriptionNotice:
    raw = json.loads(line)
    if not isinstance(raw, dict) or set(raw) != {"job_id", "workspace", "channel_id", "message_ts", "state"}:
        raise ValueError("notice_invalid")
    return ImageDescriptionNotice(**raw)


def enqueue(notice: ImageDescriptionNotice, *, root: Path | str | None = None) -> bool:
    """Append one receipt. Duplicate rows are harmless: drain de-duplicates job IDs."""
    path = outbox_path(notice.workspace, root=root)
    lock = _lock(path)
    try:
        lock.acquire(timeout=20)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(_decode(line).job_id)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        if notice.job_id not in existing:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(asdict(notice), sort_keys=True) + "\n")
            os.chmod(path, 0o600)
        return True
    except (AlreadyRunning, LockUnavailable, OSError):
        return False
    finally:
        lock.release()


def drain(workspace: str, deliver, *, root: Path | str | None = None) -> dict[str, int]:
    """Deliver valid notices; retain every failed delivery for a later Archiver poll."""
    path = outbox_path(workspace, root=root)
    if not path.is_file():
        return {"sent": 0, "left": 0, "invalid": 0}
    lock = _lock(path)
    try:
        lock.acquire(timeout=20)
        lines = path.read_text(encoding="utf-8").splitlines()
    except (AlreadyRunning, LockUnavailable, OSError):
        return {"sent": 0, "left": 0, "invalid": 0}

    sent, invalid, left = 0, 0, []
    seen: set[str] = set()
    try:
        for line in lines:
            if not line.strip():
                continue
            try:
                notice = _decode(line)
            except (ValueError, TypeError, json.JSONDecodeError):
                invalid += 1
                continue
            if notice.job_id in seen:
                continue
            seen.add(notice.job_id)
            try:
                delivered = deliver(notice) is True
            except Exception:  # noqa: BLE001 - delivery failures stay queued
                delivered = False
            if delivered:
                sent += 1
            else:
                left.append(line)
        if left:
            replacement = path.with_suffix(".tmp")
            replacement.write_text("\n".join(left) + "\n", encoding="utf-8", newline="\n")
            os.chmod(replacement, 0o600)
            replacement.replace(path)
        else:
            path.unlink(missing_ok=True)
    finally:
        lock.release()
    return {"sent": sent, "left": len(left), "invalid": invalid}


__all__ = ["ImageDescriptionNotice", "drain", "enqueue", "outbox_path"]
