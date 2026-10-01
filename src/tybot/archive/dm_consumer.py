"""Archive text-only Master DMs from the encrypted handoff inbox."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from . import shadow_paths, writer
from .dm_file_handoff import DmFileVault
from .dm_inbox import DmHandoff, DmInbox


class DmConsumer:
    def __init__(
        self, *, workspace: str, master_bot_user_id: str,
        archive_root: Path, inbox: DmInbox, file_vault: DmFileVault | None = None,
    ) -> None:
        self.workspace = workspace
        self.master_bot_user_id = master_bot_user_id
        self.archive_root = Path(archive_root)
        self.inbox = inbox
        self.file_vault = file_vault

    def run_once(self) -> tuple[int, int]:
        return self.inbox.drain_once(self.workspace, self._ingest)

    def _sync_directories(self, target: Path) -> None:
        if os.name == "nt":
            return
        current = self.archive_root
        parts = (None, *target.relative_to(self.archive_root).parts)
        for part in parts:
            if part is not None:
                current = current / part
            fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _ingest(self, handoff: DmHandoff) -> bool:
        handoff.validate()
        if (handoff.workspace != self.workspace
                or handoff.master_bot_user_id != self.master_bot_user_id):
            return False
        event = handoff.event
        files = event.get("files") or []
        if event.get("subtype") == "file_share" and not files:
            return False
        body = event.get("text")
        if body is not None and not isinstance(body, str):
            return False
        if body and writer.screen(body):
            return False
        if not body and not files:
            return False

        directory = shadow_paths.dm_archive_dir(
            self.archive_root, self.workspace, handoff.user_id, handoff.channel_id,
        )
        references: list[str] = []
        if files:
            keys = event.get("_dm_file_keys")
            if (self.file_vault is None or not isinstance(keys, list)
                    or len(keys) != len(files)):
                return False
            originals = []
            for item, key in zip(files, keys, strict=True):
                meta, data = self.file_vault.open(self.workspace, key)
                if (not isinstance(item, dict) or item.get("id") != meta.file_id
                        or meta.workspace != self.workspace
                        or meta.channel_id != handoff.channel_id
                        or meta.user_id != handoff.user_id
                        or meta.message_ts != handoff.message_ts):
                    return False
                originals.append((meta, data, key))
            private_root = shadow_paths.dm_root(
                self.archive_root, self.workspace, handoff.user_id, handoff.channel_id,
            )
            private_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_root.mkdir(exist_ok=True, mode=0o700)
            shadow_paths.refuse_symlinked_chain(self.archive_root, private_root)
            os.chmod(private_root.parent, 0o700)
            os.chmod(private_root, 0o700)
            (private_root / "objects").mkdir(exist_ok=True, mode=0o700)
            target = DmFileVault(private_root / "objects", self.inbox.cipher)
            for meta, data, key in originals:
                if target.put(meta, data) != key:
                    return False
                self._sync_directories(private_root / "objects" / self.workspace / "files")
                references.append(
                    f"[DM attachment original retained: {meta.file_id}; "
                    f"object={key}; extraction pending]"
                )

        messages = [
            writer.IncomingMessage(
                ts=datetime.fromtimestamp(float(handoff.message_ts), tz=UTC),
                speaker=handoff.speaker,
                text=text,
                source_ts=handoff.message_ts,
            )
            for text in ([body] if body and body.strip() else []) + references
        ]
        result = writer.ingest(
            self.archive_root,
            workspace=self.workspace,
            channel=writer.dm_channel(handoff.user_id),
            channel_id=handoff.channel_id,
            messages=messages,
            acl=[],
            dm_user=handoff.user_id,
            dm_directory=directory,
        )
        if result.refused:
            return False
        # A crash after raw append and before inbox ACK delivers this again.
        # Verify every expected line, including each attachment reference.
        expected = {writer.dedupe_line(writer.format_line(message)) for message in messages}
        remaining = set(expected)
        matched_paths = []
        for path in sorted((directory / "raw").glob("*.md")):
            lines = {writer.dedupe_line(line) for line in path.read_text(encoding="utf-8").splitlines()}
            if remaining & lines:
                matched_paths.append(path)
                remaining -= lines
            if not remaining:
                break
        if remaining:
            return False
        for path in matched_paths:
            with path.open("rb+") as raw:
                os.fsync(raw.fileno())
        self._sync_directories(directory / "raw")
        return True
