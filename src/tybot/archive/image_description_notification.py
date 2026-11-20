"""Safe, local-only delivery queue for image-description completion notices.

The Vision worker intentionally has no Slack capability.  It can only append a
small, validated completion receipt here; the Archiver owns the later Slack
thread reply.  File names and source text are never placed in this queue.

**2026-11-20 (owner decision) - show what was recognized.** The uploader must be
able to check how the image was read, so the receipt may now carry the
*published* recognition: the description and tags that passed
``image_description_output_guard`` and the OCR lines that passed
``guard_transcript``. Raw model output is still never queued. Everything is
masked again (``sensitive_mask``) right before it becomes Slack text, Slack
control sequences are escaped, and sizes are capped. The reply is a bot message
in the image's own thread, so it reaches exactly the people who can already see
the image and it is never re-collected as evidence (principle 1).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from tybot.lock import AlreadyRunning, FileLock, LockUnavailable
from tybot.sensitive_mask import contains_sensitive, mask_sensitive

_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CHANNEL = re.compile(r"^[CG][A-Z0-9]{8,}$")
_TS = re.compile(r"^\d{1,20}\.\d{1,9}$")
_JOB = re.compile(r"^[a-f0-9]{32}$")
_STATE = frozenset({"completed"})
_REQUIRED = frozenset({"job_id", "workspace", "channel_id", "message_ts", "state"})
_OPTIONAL = frozenset({"description", "tags", "transcript", "transcript_more", "uncertain"})

#: Slack reply caps. A wall of OCR is not something a person checks.
MAX_DESCRIPTION_CHARS = 600
MAX_TAGS = 12
MAX_TAG_CHARS = 40
MAX_TRANSCRIPT_LINES = 12
MAX_TRANSCRIPT_LINE_CHARS = 160

FIXED_TEXT = "✅ 이미지 설명·텍스트 인식 완료 — 검색 보조 정보로 반영되었습니다."


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _slack_safe(text: str) -> str:
    """Escape Slack control characters so model text cannot mention or link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _masked(text: str) -> str:
    return mask_sensitive(text).text


@dataclass(frozen=True)
class ImageDescriptionNotice:
    job_id: str
    workspace: str
    channel_id: str
    message_ts: str
    state: str = "completed"
    # Published, guard-approved recognition. Empty means "fixed text only".
    description: str = ""
    tags: tuple[str, ...] = ()
    transcript: tuple[str, ...] = ()
    transcript_more: int = 0
    uncertain: bool = False

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
        if not isinstance(self.description, str) or len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValueError("description_invalid")
        if (
            not isinstance(self.tags, tuple)
            or len(self.tags) > MAX_TAGS
            or any(not isinstance(t, str) or len(t) > MAX_TAG_CHARS for t in self.tags)
        ):
            raise ValueError("tags_invalid")
        if (
            not isinstance(self.transcript, tuple)
            or len(self.transcript) > MAX_TRANSCRIPT_LINES
            or any(
                not isinstance(t, str) or len(t) > MAX_TRANSCRIPT_LINE_CHARS
                for t in self.transcript
            )
        ):
            raise ValueError("transcript_invalid")
        if type(self.transcript_more) is not int or self.transcript_more < 0:
            raise ValueError("transcript_invalid")
        if type(self.uncertain) is not bool:
            raise ValueError("uncertain_invalid")

    @property
    def has_recognition(self) -> bool:
        return bool(self.description or self.tags or self.transcript)

    @property
    def client_msg_id(self) -> str:
        """Stable Slack idempotency key; never derived from source content."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tybot:image-notice:{self.job_id}"))

    @property
    def text(self) -> str:
        if not self.has_recognition:
            return FIXED_TEXT
        head = (
            "🟡 이미지를 이렇게 인식했습니다 (일부 불확실)"
            if self.uncertain
            else "✅ 이미지를 이렇게 인식했습니다"
        )
        lines = [f"{head} — 검색 보조 정보로 반영되었습니다."]
        if self.description:
            lines.append(f"*설명* {self.description}")
        if self.tags:
            lines.append("*태그* " + ", ".join(self.tags))
        if self.transcript:
            lines.append("*읽은 글자* (민감정보는 가림)")
            lines.extend(f"│ {line}" for line in self.transcript)
            if self.transcript_more:
                lines.append(f"│ …외 {self.transcript_more}줄")
        lines.append("_AI 인식 결과입니다. 틀린 곳이 있으면 이 스레드에 바로잡아 주세요._")
        # Masked again at delivery (principle 5), then escaped for Slack.
        return _slack_safe(_masked("\n".join(lines)))

    def to_json(self) -> dict:
        payload = {
            "job_id": self.job_id,
            "workspace": self.workspace,
            "channel_id": self.channel_id,
            "message_ts": self.message_ts,
            "state": self.state,
        }
        if self.has_recognition:
            payload |= {
                "description": self.description,
                "tags": list(self.tags),
                "transcript": list(self.transcript),
                "transcript_more": self.transcript_more,
                "uncertain": self.uncertain,
            }
        return payload


def recognition_fields(
    *,
    description: str = "",
    tags=(),
    transcript: str = "",
    uncertain: bool = False,
) -> dict:
    """Clip and mask *published* recognition into notice fields.

    Callers pass only guard-approved values (the stored revision), never the
    raw model response. Anything that still looks sensitive after masking is
    dropped rather than shown.
    """
    desc = _clip(_masked(description), MAX_DESCRIPTION_CHARS)
    if contains_sensitive(desc):
        desc = ""
    kept_tags: list[str] = []
    for tag in tags or ():
        clipped = _clip(_masked(str(tag)), MAX_TAG_CHARS)
        if clipped and not contains_sensitive(clipped) and clipped not in kept_tags:
            kept_tags.append(clipped)
        if len(kept_tags) >= MAX_TAGS:
            break
    lines = [
        _clip(line, MAX_TRANSCRIPT_LINE_CHARS)
        for line in _masked(transcript).splitlines()
        if line.strip()
    ]
    lines = [line for line in lines if not contains_sensitive(line)]
    return {
        "description": desc,
        "tags": tuple(kept_tags),
        "transcript": tuple(lines[:MAX_TRANSCRIPT_LINES]),
        "transcript_more": max(0, len(lines) - MAX_TRANSCRIPT_LINES),
        "uncertain": bool(uncertain),
    }


def load_recognition(connection, revision_id: str) -> dict:
    """Read the published description/transcript revisions for one job.

    Only the stored, guard-approved revisions are read. Missing rows give an
    empty dict, which keeps the fixed completion text.
    """
    if not _JOB.fullmatch(str(revision_id or "")):
        return {}
    with connection.cursor() as cur:
        cur.execute(
            "SELECT status, description, tags FROM archiver_image_description_revision "
            "WHERE id=%s",
            (revision_id,),
        )
        described = cur.fetchone()
        cur.execute(
            "SELECT status, body FROM archiver_image_transcript_revision "
            "WHERE description_revision_id=%s LIMIT 1",
            (revision_id,),
        )
        transcribed = cur.fetchone() or {}
    if not described:
        return {}
    statuses = {str(described.get("status") or ""), str(transcribed.get("status") or "")}
    return recognition_fields(
        description=str(described.get("description") or ""),
        tags=tuple(described.get("tags") or ()),
        transcript=str(transcribed.get("body") or ""),
        uncertain="uncertain" in statuses,
    )


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
    if (
        not isinstance(raw, dict)
        or not set(raw) >= _REQUIRED
        or not set(raw) <= _REQUIRED | _OPTIONAL
    ):
        raise ValueError("notice_invalid")
    for key in ("tags", "transcript"):
        if key in raw:
            if not isinstance(raw[key], list):
                raise ValueError("notice_invalid")
            raw[key] = tuple(raw[key])
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
                handle.write(
                    json.dumps(notice.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
                )
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


__all__ = [
    "ImageDescriptionNotice",
    "drain",
    "enqueue",
    "load_recognition",
    "outbox_path",
    "recognition_fields",
]
