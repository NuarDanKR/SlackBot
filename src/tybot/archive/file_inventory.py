"""Read-only Archiver file inventory. Never download or fabricate message evidence."""
from __future__ import annotations

import re
from pathlib import Path

from . import channel_files, channel_membership, shadow_paths
from .files import attachment_storage


def preview(client, repo, workspace: str, channel_id: str, *,
            live_root: Path) -> dict:
    """Compare channel-scoped Slack files with the live staging namespace.

    운영 루트(`ARCHIVE_DIR`) 하나만 본다(B-99 2단계 D7). 옛 섀도 루트는 보관만
    하고 대조 대상이 아니다.

    A metadata record means only that staging was attempted, not that the
    original was retained, converted, or made searchable. This is not a folder
    inventory and must not be used to assert a folder's file count.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace):
        raise ValueError("invalid workspace")
    if not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel_id):
        raise ValueError("invalid channel ID")
    root = Path(live_root)
    if not root.is_absolute():
        raise ValueError("archive root must be absolute")
    row = channel_membership.channel_row(repo, workspace, channel_id)
    if not channel_membership.is_collectible(row):
        raise PermissionError("channel collection is disabled or membership is unknown")
    info = client.conversations_info(channel=channel_id).get("channel") or {}
    if (info.get("id") != channel_id or not info.get("is_member")
            or info.get("is_im") or info.get("is_mpim") or info.get("is_archived")):
        raise PermissionError("current Slack channel membership is required")
    name = str(info.get("name") or channel_id)
    channel_root = shadow_paths.channel_root(root, workspace, channel_id, name)
    shadow_paths.refuse_symlinked_chain(root, channel_root / "staging")
    storage = attachment_storage(root, workspace, channel_id, channel_root=channel_root)
    result = channel_files.scan(client, channel_id, storage)
    missing = []
    for raw in result.candidates:
        file_id = str(raw["id"])
        if not re.fullmatch(r"F[A-Z0-9]+", file_id):
            raise ValueError("invalid Slack file ID")
        missing.append(file_id)
    return {
        "workspace": workspace, "channel_id": channel_id,
        "scope": "channel-files-not-folder", "read_only": True,
        "mode": row["mode"], "list_complete": not result.truncated,
        "files_seen": result.total, "live_metadata_records": result.known,
        "generated_excluded": result.generated_excluded,
        "missing_metadata_ids": missing,
        "warning_count": len(result.warnings),
    }
