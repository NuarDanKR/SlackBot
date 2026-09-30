"""Channel-scoped shadow layout. Live archives keep their existing paths."""

from __future__ import annotations

import re
from pathlib import Path


class ShadowPathError(ValueError):
    pass


def _component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣()_-]+", "_", value.strip().lstrip("#"))
    return cleaned.strip("_")[:80] or "unnamed"


def channel_root(root: Path | str, workspace: str, channel_id: str, name: str) -> Path:
    """Resolve the first channel directory by ID, regardless of later renames."""
    if not re.fullmatch(r"(?:[CG][A-Z0-9]{8,}|legacy-[a-f0-9]{16})", channel_id):
        raise ShadowPathError("invalid channel ID for shadow layout")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace):
        raise ShadowPathError("invalid workspace key for shadow layout")
    base = Path(root) / workspace
    if not base.resolve().is_relative_to(Path(root).resolve()):
        raise ShadowPathError("shadow workspace directory escaped its root")
    matches = sorted(path for path in base.glob(f"{channel_id}__*") if path.is_dir())
    if len(matches) > 1:
        raise ShadowPathError("multiple shadow directories claim one channel ID")
    if matches and matches[0].is_symlink():
        raise ShadowPathError("shadow channel directory is a symlink")
    return matches[0] if matches else base / f"{channel_id}__{_component(name)}"


def existing_channel_root(root: Path | str, workspace: str, channel_id: str) -> Path:
    """Find a previously written channel without inventing a new display name."""
    candidate = channel_root(root, workspace, channel_id, channel_id)
    if not candidate.is_dir():
        raise ShadowPathError("shadow channel directory is missing")
    return candidate


def archive_dir(root: Path | str, workspace: str, channel_id: str, name: str) -> Path:
    return channel_root(root, workspace, channel_id, name) / "archive"
