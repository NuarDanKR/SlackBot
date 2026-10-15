#!/usr/bin/env python3
"""Read-only file discovery using the registered Archiver, never the Master token."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from slack_sdk import WebClient

from tybot.archive.file_inventory import preview
from tybot.archiving_bot import (
    load_archiver_workspaces,
    shadow_archive_dir,
    validate_slack_identity,
)
from tybot.console.archiving_repo import default_repo
from tybot.envfile import load_env_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--channel", required=True)
    args = parser.parse_args(argv)
    try:
        load_env_file()
        values = dict(os.environ, ARCHIVER_CONFIG_SOURCE="db", ARCHIVER_WORKSPACE=args.workspace)
        cfg, = load_archiver_workspaces(values)
        shadow_root = shadow_archive_dir(values)
        if values.get("ARCHIVER_SHADOW_LAYOUT") != "per-channel-v1":
            raise ValueError("per-channel-v1 layout is required")
        client = WebClient(token=cfg.bot_token, timeout=30)
        validate_slack_identity(cfg, client.auth_test())
        report = preview(
            client, default_repo(), cfg.key, args.channel,
            live_root=pathlib.Path(values["ARCHIVE_DIR"]), shadow_root=shadow_root,
        )
    except Exception as exc:  # noqa: BLE001 - never expose SDK response or credentials
        print(json.dumps({"error": type(exc).__name__, "read_only": True}))
        return 1
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["list_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
