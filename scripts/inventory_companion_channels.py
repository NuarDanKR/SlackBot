#!/usr/bin/env python3
"""Read-only channel inventory for companion app rollout.

The union is limited to channels currently joined by the configured Master or
Archiver bot. Private channels invisible to both bots cannot be discovered.
No invitation, message, or archive write is performed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive.channel_membership import fetch_joined  # noqa: E402

SOURCES = frozenset({"master", "archiver"})


def inventory(connections, client_factory, token_loader, *, workspaces=(), exclude=("tec",)):
    """Fail closed if any configured source cannot be read completely."""
    selected = set(workspaces)
    excluded = set(exclude)
    rows = defaultdict(dict)
    for row in connections:
        workspace = str(row["workspace"])
        key = str(row["bot_key"])
        if (
            key not in SOURCES
            or workspace in excluded
            or (selected and workspace not in selected)
            or row.get("state") != "enabled"
        ):
            continue
        rows[workspace][key] = row

    result = []
    for workspace, bots in sorted(rows.items()):
        channels = defaultdict(set)
        for key, row in sorted(bots.items()):
            if row.get("identity_ok") is not True:
                raise RuntimeError(f"unverified source: {workspace}/{key}")
            token = token_loader(workspace, key)
            try:
                joined = fetch_joined(client_factory(token=token))
            finally:
                token = ""
            for channel in joined:
                channels[channel.channel_id].add(key)
        result.append(
            {
                "workspace": workspace,
                "source_bots": sorted(bots),
                "channels": [
                    {"channel_id": channel_id, "seen_by": sorted(source_keys)}
                    for channel_id, source_keys in sorted(channels.items())
                ],
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace", action="append", default=[], help="repeat to restrict the inventory"
    )
    args = parser.parse_args()

    from slack_sdk import WebClient

    from tybot.console.bot_identity import bot_token_for_admin_operation
    from tybot.console.bot_repo import default_repo
    from tybot.envfile import load_env_file

    try:
        load_env_file()
        result = inventory(
            default_repo().connections(),
            WebClient,
            bot_token_for_admin_operation,
            workspaces=args.workspace,
        )
    except Exception as exc:  # noqa: BLE001 - never print token-bearing exceptions
        print(f"inventory failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.workspace and set(args.workspace) != {r["workspace"] for r in result}:
        print("inventory incomplete: requested workspace has no enabled source", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "read_only": True,
                "scope": "joined-master-or-archiver-channels",
                "excluded_workspaces": ["tec"],
                "total_channels": sum(len(r["channels"]) for r in result),
                "workspaces": result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
