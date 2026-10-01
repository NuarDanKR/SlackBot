"""Encrypted, durable handoff of Master app DM events to the Archiver.

This module does not subscribe to Slack or switch the live DM writer. The
producer must verify the conversation with the Master app before enqueueing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..lock import FileLock

_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_DM_ID = re.compile(r"D[A-Z0-9]{7,}\Z")
_USER_ID = re.compile(r"[UW][A-Z0-9]{7,}\Z")
_MESSAGE_TS = re.compile(r"\d{10,}\.\d{1,6}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_MAX_ENVELOPE_BYTES = 2_000_000
log = logging.getLogger("tybot.archive.dm_inbox")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class DmHandoff:
    workspace: str
    channel_id: str
    user_id: str
    message_ts: str
    revision_id: str
    master_bot_user_id: str
    speaker: str
    event: dict

    def validate(self) -> None:
        if not _WORKSPACE.fullmatch(self.workspace):
            raise ValueError("invalid workspace")
        if not _DM_ID.fullmatch(self.channel_id):
            raise ValueError("invalid DM channel")
        if not _USER_ID.fullmatch(self.user_id):
            raise ValueError("invalid DM user")
        if not _USER_ID.fullmatch(self.master_bot_user_id) or self.master_bot_user_id == self.user_id:
            raise ValueError("invalid Master bot identity")
        if not isinstance(self.speaker, str) or not self.speaker.strip() or len(self.speaker) > 200:
            raise ValueError("invalid speaker")
        if not _MESSAGE_TS.fullmatch(self.message_ts):
            raise ValueError("invalid message timestamp")
        if not self.revision_id or len(self.revision_id) > 128:
            raise ValueError("invalid revision identity")
        if not isinstance(self.event, dict):
            raise ValueError("invalid event")
        if (self.event.get("channel_type") != "im"
                or self.event.get("channel") != self.channel_id
                or self.event.get("user") != self.user_id
                or self.event.get("ts") != self.message_ts
                or self.event.get("bot_id")
                or self.event.get("app_id")
                or self.event.get("hidden")
                or self.event.get("subtype") not in (None, "file_share")):
            raise ValueError("not a human one-to-one DM event")
        file_keys = self.event.get("_dm_file_keys")
        if file_keys is not None:
            files = self.event.get("files")
            if (not isinstance(files, list) or not isinstance(file_keys, list)
                    or len(file_keys) != len(files)
                    or not all(isinstance(key, str) and _DIGEST.fullmatch(key)
                               for key in file_keys)):
                raise ValueError("invalid DM attachment handoff references")

    @property
    def key(self) -> str:
        self.validate()
        identity = [self.workspace, self.channel_id, self.user_id,
                    self.message_ts, self.revision_id]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


class DmInbox:
    def __init__(self, root: Path, cipher: Fernet):
        self.root = Path(root)
        self.cipher = cipher
        self._last_key = ""

    def _directory(self, workspace: str) -> Path:
        if not _WORKSPACE.fullmatch(workspace):
            raise ValueError("invalid workspace")
        workspace_dir = self.root / workspace
        directory = workspace_dir / "pending"
        if any(path.is_symlink() for path in (self.root, workspace_dir, directory)):
            raise ValueError("DM inbox path must not be a symlink")
        for path in (self.root, workspace_dir, directory):
            path.mkdir(exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        return directory

    def enqueue(self, handoff: DmHandoff) -> str:
        handoff.validate()
        body = json.dumps(asdict(handoff), ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > _MAX_ENVELOPE_BYTES:
            raise ValueError("DM event is too large for the inbox")
        directory = self._directory(handoff.workspace)
        target = directory / f"{handoff.key}.bin"
        if target.exists():
            if self.read(handoff.workspace, handoff.key) != handoff:
                raise ValueError("DM revision identity has conflicting content")
            return handoff.key

        encrypted = self.cipher.encrypt(body)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".dm-", delete=False) as temp:
            temporary = Path(temp.name)
            os.chmod(temporary, 0o600)
            try:
                temp.write(encrypted)
                temp.flush()
                os.fsync(temp.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            try:
                os.link(temporary, target)
                _sync_directory(directory)
            except FileExistsError:
                if self.read(handoff.workspace, handoff.key) != handoff:
                    raise ValueError("DM revision identity has conflicting content") from None
        finally:
            temporary.unlink(missing_ok=True)
        return handoff.key

    def pending_keys(self, workspace: str) -> list[str]:
        directory = self._directory(workspace)
        return sorted(p.stem for p in directory.glob("*.bin") if _DIGEST.fullmatch(p.stem))

    def read(self, workspace: str, key: str) -> DmHandoff:
        if not _DIGEST.fullmatch(key):
            raise ValueError("invalid DM inbox key")
        path = self._directory(workspace) / f"{key}.bin"
        try:
            data = self.cipher.decrypt(path.read_bytes())
        except (InvalidToken, FileNotFoundError) as exc:
            raise ValueError("DM inbox entry cannot be read") from exc
        if len(data) > _MAX_ENVELOPE_BYTES:
            raise ValueError("DM inbox entry is too large")
        try:
            handoff = DmHandoff(**json.loads(data))
            handoff.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid DM inbox entry") from exc
        if handoff.workspace != workspace or handoff.key != key:
            raise ValueError("DM inbox identity mismatch")
        return handoff

    def acknowledge(self, workspace: str, key: str) -> None:
        # The caller must first confirm durable archive write or policy refusal.
        self.read(workspace, key)
        directory = self._directory(workspace)
        (directory / f"{key}.bin").unlink()
        _sync_directory(directory)

    def drain_once(
        self, workspace: str, handle: Callable[[DmHandoff], bool], *, limit: int = 50,
    ) -> tuple[int, int]:
        """ACK only after the handler confirms a durable write or recorded refusal.

        The handler must deduplicate by handoff identity: a crash after its write
        and before the ACK causes the same entry to be delivered again.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        directory = self._directory(workspace)
        completed = failed = 0
        with FileLock(directory.parent / ".consumer.lock", label="DM handoff consumer"):
            keys = self.pending_keys(workspace)
            next_keys = [key for key in keys if key > self._last_key]
            next_keys.extend(key for key in keys if key <= self._last_key)
            selected = next_keys[:limit]
            if selected:
                self._last_key = selected[-1]
            for key in selected:
                try:
                    handoff = self.read(workspace, key)
                    if not handle(handoff):
                        failed += 1
                        continue
                    self.acknowledge(workspace, key)
                    completed += 1
                except Exception as exc:  # noqa: BLE001 - one poisoned DM must not block other entries
                    log.warning("DM inbox delivery failed workspace=%s error=%s",
                                workspace, type(exc).__name__)
                    failed += 1
        return completed, failed
