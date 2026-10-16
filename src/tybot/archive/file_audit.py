"""Read-only, workspace-isolated Archiver inventory sweep. Never sync membership."""
from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path

from . import channel_membership
from .file_inventory import preview
from .file_recovery import existing_records

log = logging.getLogger("tybot.archive.file_audit")


def audit(workspaces: list[str], *, config_loader, client_factory, repo,
          live_root: Path, shadow_root: Path, pause: float = 3.0,
          identity_validator) -> dict:
    """Only intersect actual joined channels with currently collectible DB rows.

    A broken workspace/channel is recorded as an error, not an empty success.
    This does not use Master workspaces, tokens, writers, or its name filters.
    """
    if (not math.isfinite(pause) or pause < 0
            or not all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", ws) for ws in workspaces)):
        raise ValueError("invalid audit scope or pace")
    reports = []
    for workspace in sorted(set(workspaces)):
        report = {"workspace": workspace, "status": "ok", "channels": []}
        reports.append(report)
        log.info("file audit workspace=%s started", workspace)
        try:
            cfg = config_loader(workspace)
            if cfg.key != workspace:
                raise ValueError("workspace configuration mismatch")
            client = client_factory(cfg)
            identity_validator(cfg, client.auth_test())
            rows = {str(row["channel_id"]): row for row in repo.channels(workspace)}
            report["target_channels"] = sum(
                channel_membership.is_collectible(row) for row in rows.values()
            )
            joined = channel_membership.fetch_joined(client)
            report["joined_channels"] = len(joined)
            eligible = [channel for channel in joined
                        if channel_membership.is_collectible(rows.get(channel.channel_id))]
            report["skipped_channels"] = len(joined) - len(eligible)
            joined_ids = {channel.channel_id for channel in joined}
            report["db_joined_not_in_slack"] = sorted(
                cid for cid, row in rows.items()
                if channel_membership.is_collectible(row) and cid not in joined_ids
            )
            if report["db_joined_not_in_slack"]:
                report["status"] = "partial"
            for index, channel in enumerate(eligible):
                if index and pause:
                    time.sleep(pause)
                try:
                    item = preview(client, repo, workspace, channel.channel_id,
                                   live_root=live_root, shadow_root=shadow_root)
                    uncertain = []
                    missing = []
                    for file_id in item["missing_metadata_ids"]:
                        records = existing_records([live_root, shadow_root], workspace,
                                                   channel.channel_id, channel.name, file_id)
                        if records:
                            uncertain.append({"file_id": file_id, "records": records})
                        else:
                            missing.append(file_id)
                    item["existing_needs_review"] = uncertain
                    item["missing_after_record_check"] = missing
                    item["status"] = "ok" if item["list_complete"] else "partial"
                except Exception as exc:  # noqa: BLE001 - no SDK body or plaintext secrets
                    item = {"channel_id": channel.channel_id, "status": "failed",
                            "error": type(exc).__name__}
                report["channels"].append(item)
                if item["status"] != "ok":
                    report["status"] = "partial"
                log.info("file audit workspace=%s channel=%s status=%s",
                         workspace, channel.channel_id, item["status"])
        except Exception as exc:  # noqa: BLE001 - a broken workspace must remain visible
            report.update(status="failed", error=type(exc).__name__)
        log.info("file audit workspace=%s status=%s", workspace, report["status"])
    return {"read_only": True, "scope": "joined-collectible-channel-files-not-folders",
            "complete": bool(reports) and all(row["status"] == "ok" for row in reports),
            "workspaces": reports}
