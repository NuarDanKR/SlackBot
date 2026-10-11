"""Per-specialist, per-workspace HMAC files for the v3 RPC bridge."""
from __future__ import annotations

import os
import re
import stat
import threading
from pathlib import Path

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def credential_id(specialist: str, workspace: str) -> str:
    if not _KEY.fullmatch(specialist) or not _KEY.fullmatch(workspace):
        raise ValueError("invalid specialist credential key")
    return f"{specialist}:{workspace}"


class CredentialStore:
    """Read fixed-name HMAC files without accepting paths from RPC input."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, specialist: str, workspace: str) -> tuple[str, bytes]:
        identity = credential_id(specialist, workspace)
        with self._lock:
            cached = self._cache.get(identity)
            if cached is not None:
                return identity, cached

        target = self.root / f"{specialist}-{workspace}.hmac"
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise RuntimeError(
                f"external specialist HMAC credential is unreadable: {target.name}"
            ) from exc
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("external specialist HMAC credential is not a regular file")
            if os.name != "nt" and info.st_mode & 0o022:
                raise RuntimeError("external specialist HMAC credential is writable by group/world")
            raw = handle.read()

        # Match the Hermes worker: remove one editor-added LF, preserving all
        # other bytes as part of the secret.
        secret = raw[:-1] if raw.endswith(b"\n") else raw
        if len(secret) < 32:
            raise RuntimeError("external specialist HMAC credential is too short")
        with self._lock:
            self._cache[identity] = secret
        return identity, secret


__all__ = ["CredentialStore", "credential_id"]
