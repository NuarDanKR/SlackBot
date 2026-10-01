"""Encrypted, durable handoff of Master app DM attachment bytes to the Archiver.

The text side is `dm_inbox`; this module carries the original bytes. They are
separate because an event envelope is small and a file is not: one size limit,
one retry policy, and one failure mode cannot serve both.

The Archiver never re-downloads a Master DM file. Its own token does not see
that conversation, and a token that did would make the Archiver a second reader
of someone's private DM. So the bytes travel with the handoff or not at all.
This module therefore performs no network access; a test pins that.

Metadata and payload are sealed in one ciphertext. Swapping either alone fails
authentication, so a stored entry cannot claim a file ID or coordinate that does
not belong to the bytes beside it.

This module is not wired to any runtime. It creates nothing outside the vault
root it is handed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_DM_ID = re.compile(r"D[A-Z0-9]{7,}\Z")
_USER_ID = re.compile(r"[UW][A-Z0-9]{7,}\Z")
_MESSAGE_TS = re.compile(r"\d{10,}\.\d{1,6}\Z")
_FILE_ID = re.compile(r"F[A-Z0-9]{7,}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")

#: A DM attachment larger than this is refused rather than queued. The vault
#: holds plaintext-sized ciphertext in memory to seal it; an unbounded file
#: would trade a storage problem for a memory one.
MAX_FILE_BYTES = 64 * 1024 * 1024

#: Header length prefix. Fixed width so the reader never guesses.
_HEADER_LENGTH_BYTES = 4

log = logging.getLogger("tybot.archive.dm_file_handoff")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class DmFile:
    """One DM attachment's identity. The bytes are passed beside it, never here.

    `sha256` is the digest of the original bytes as Slack served them. It is the
    only value that can tell a re-delivery from a different file with the same
    ID, which happens when a file is replaced in place.
    """

    workspace: str
    channel_id: str
    user_id: str
    message_ts: str
    file_id: str
    name: str
    mimetype: str
    size: int
    sha256: str

    def validate(self) -> None:
        if not _WORKSPACE.fullmatch(self.workspace):
            raise ValueError("invalid workspace")
        if not _DM_ID.fullmatch(self.channel_id):
            raise ValueError("invalid DM channel")
        if not _USER_ID.fullmatch(self.user_id):
            raise ValueError("invalid DM user")
        if not _MESSAGE_TS.fullmatch(self.message_ts):
            raise ValueError("invalid message timestamp")
        if not _FILE_ID.fullmatch(self.file_id):
            raise ValueError("invalid Slack file id")
        if not _DIGEST.fullmatch(self.sha256):
            raise ValueError("invalid content digest")
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise ValueError("invalid file size")
        if self.size < 0 or self.size > MAX_FILE_BYTES:
            raise ValueError("invalid file size")
        # A name is carried for provenance, not for a path. It never becomes one
        # here, but a separator in it would become one in a careless consumer.
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 255:
            raise ValueError("invalid file name")
        if any(part in self.name for part in ("/", "\\", "\0")):
            raise ValueError("invalid file name")
        if not isinstance(self.mimetype, str) or len(self.mimetype) > 255:
            raise ValueError("invalid mimetype")

    @property
    def key(self) -> str:
        """Storage identity. The digest is in it, so replaced content gets a new key.

        Leaving the digest out would make a re-upload under the same file ID look
        like a retry, and the first version would win silently.
        """
        self.validate()
        identity = [self.workspace, self.channel_id, self.user_id,
                    self.message_ts, self.file_id, self.sha256]
        return hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()


def digest_of(data: bytes) -> str:
    """The digest the producer must record. One function so both sides agree."""
    return hashlib.sha256(data).hexdigest()


class DmFileVault:
    """Encrypted-at-rest store for DM attachment bytes awaiting the Archiver."""

    def __init__(self, root: Path, cipher: Fernet):
        self.root = Path(root)
        self.cipher = cipher

    def _directory(self, workspace: str) -> Path:
        if not _WORKSPACE.fullmatch(workspace):
            raise ValueError("invalid workspace")
        workspace_dir = self.root / workspace
        directory = workspace_dir / "files"
        if any(path.is_symlink() for path in (self.root, workspace_dir, directory)):
            raise ValueError("DM file vault path must not be a symlink")
        for path in (self.root, workspace_dir, directory):
            path.mkdir(exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        return directory

    def _seal(self, meta: DmFile, data: bytes) -> bytes:
        header = json.dumps(
            asdict(meta), ensure_ascii=False, separators=(",", ":")
        ).encode()
        return self.cipher.encrypt(
            len(header).to_bytes(_HEADER_LENGTH_BYTES, "big") + header + data
        )

    def _unseal(self, blob: bytes) -> tuple[DmFile, bytes]:
        body = self.cipher.decrypt(blob)
        if len(body) < _HEADER_LENGTH_BYTES:
            raise ValueError("DM file entry is truncated")
        length = int.from_bytes(body[:_HEADER_LENGTH_BYTES], "big")
        start = _HEADER_LENGTH_BYTES + length
        if length < 0 or start > len(body):
            raise ValueError("DM file entry is truncated")
        meta = DmFile(**json.loads(body[_HEADER_LENGTH_BYTES:start]))
        meta.validate()
        return meta, body[start:]

    def put(self, meta: DmFile, data: bytes) -> str:
        """Seal one attachment. Returns its key; a repeat of the same entry is a no-op.

        The digest is checked against the bytes before anything is written. A
        mismatch here is the producer handing over a file it did not read
        correctly, and accepting it would put an unverifiable byte stream into
        the archive's provenance chain.
        """
        meta.validate()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("DM attachment is too large for the handoff")
        if len(data) != meta.size:
            raise ValueError("DM attachment size does not match its metadata")
        if digest_of(data) != meta.sha256:
            raise ValueError("DM attachment content does not match its digest")

        directory = self._directory(meta.workspace)
        target = directory / f"{meta.key}.bin"
        if target.exists():
            self._confirm_same(meta, data, target)
            return meta.key

        sealed = self._seal(meta, data)
        # The temporary file holds ciphertext only. Writing plaintext first and
        # encrypting in place would leave the file readable on disk for as long
        # as that took, and a crash in between would leave it there.
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".dmfile-", delete=False
        ) as temp:
            temporary = Path(temp.name)
            os.chmod(temporary, 0o600)
            try:
                temp.write(sealed)
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
                self._confirm_same(meta, data, target)
        finally:
            temporary.unlink(missing_ok=True)
        return meta.key

    def _confirm_same(self, meta: DmFile, data: bytes, target: Path) -> None:
        """A retry must find what it already stored, or stop.

        Overwriting would let a second producer replace an attachment that the
        Archiver may already have read and cited.
        """
        try:
            stored_meta, stored_data = self._unseal(target.read_bytes())
        except (InvalidToken, ValueError, OSError) as exc:
            raise ValueError("stored DM attachment cannot be verified") from exc
        if stored_meta != meta or stored_data != data:
            raise ValueError("DM attachment identity has conflicting content")

    def keys(self, workspace: str) -> list[str]:
        directory = self._directory(workspace)
        return sorted(
            path.stem for path in directory.glob("*.bin") if _DIGEST.fullmatch(path.stem)
        )

    def open(self, workspace: str, key: str) -> tuple[DmFile, bytes]:
        """Read one entry back, verifying the digest again after decryption."""
        if not _DIGEST.fullmatch(key):
            raise ValueError("invalid DM file vault key")
        path = self._directory(workspace) / f"{key}.bin"
        try:
            meta, data = self._unseal(path.read_bytes())
        except (InvalidToken, FileNotFoundError) as exc:
            raise ValueError("DM file vault entry cannot be read") from exc
        if meta.workspace != workspace or meta.key != key:
            raise ValueError("DM file vault identity mismatch")
        if len(data) != meta.size or digest_of(data) != meta.sha256:
            raise ValueError("DM attachment content does not match its digest")
        return meta, data

    def acknowledge(self, workspace: str, key: str) -> None:
        """Drop an entry. The caller must already hold a durable archive write."""
        self.open(workspace, key)
        directory = self._directory(workspace)
        (directory / f"{key}.bin").unlink()
        _sync_directory(directory)
