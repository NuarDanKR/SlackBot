#!/usr/bin/env python3
"""Plan explicit file recovery; --apply requires exclusive Archiver collector ownership."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from slack_sdk import WebClient

from tybot import search_index
from tybot.archive.file_recovery import index_recovered, recover
from tybot.archive.write_owner import OwnerLookup
from tybot.archiving_bot import (
    _instance_lock_name,
    load_archiver_workspaces,
    shadow_archive_dir,
    validate_slack_identity,
)
from tybot.console.archiving_repo import default_repo
from tybot.envfile import load_env_file
from tybot.lock import instance_lock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--file-id", action="append", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--index-only", action="store_true")
    parser.add_argument("--by", default="")
    parser.add_argument("--reconvert", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.by.strip():
        parser.error("--apply requires --by")
    if args.reconvert and args.index_only:
        parser.error("--reconvert cannot be combined with --index-only")
    lock = None
    stage = "environment"
    try:
        load_env_file()
        if args.index_only:
            stage = "index"
            root = pathlib.Path(os.environ["ARCHIVE_DIR"])
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("an existing absolute archive root is required")
            report = index_recovered(root, args.workspace, args.channel, args.file_id,
                                     indexer=search_index.reindex)
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
            return 0 if all(item["status"] == "recovered" for item in report["results"]) else 2
        values = dict(os.environ, ARCHIVER_CONFIG_SOURCE="db", ARCHIVER_WORKSPACE=args.workspace)
        stage = "archiver-config"
        cfg, = load_archiver_workspaces(values)
        stage = "archive-layout"
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cfg.key):
            raise ValueError("invalid workspace key")
        shadow_root = shadow_archive_dir(values)
        live_root = pathlib.Path(values["ARCHIVE_DIR"])
        if (values.get("ARCHIVER_SHADOW_LAYOUT") != "per-channel-v1"
                or not live_root.is_absolute() or not live_root.is_dir()):
            raise ValueError("an existing absolute root and per-channel-v1 are required")
        # Mixed legacy roots are allowed here; recover() checks each file's
        # artifacts before writing. Collector startup keeps its stricter guard.
        if args.apply:
            stage = "live-write-env"
            if values.get("ARCHIVER_LIVE_WRITES_ENABLED", "").strip().lower() not in {
                "1", "true", "yes", "on",
            }:
                raise PermissionError("live writes are disabled")
        stage = "slack-identity"
        client = WebClient(token=cfg.bot_token, timeout=30)
        validate_slack_identity(cfg, client.auth_test())
        if args.apply:
            stage = "collector-lock"
            lock = instance_lock(_instance_lock_name(cfg.key))
            lock.acquire()
        stage = "file-recovery"
        report = recover(
            client, default_repo(), OwnerLookup(cfg.key, cache_seconds=0), cfg, args.file_id,
            live_root=live_root, shadow_root=shadow_root, channel_id=args.channel,
            apply=args.apply, actor=args.by, reconvert=args.reconvert,
        )
    except Exception as exc:  # noqa: BLE001 - no credential or private SDK response output
        print(json.dumps({"error": type(exc).__name__, "stage": stage,
                          "apply_requested": args.apply}))
        return 1
    finally:
        if lock is not None:
            lock.release()
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    good = {"recovered", "published-index-pending"} if args.apply else {
        "missing", "resume", "reconvert-planned",
    }
    return 0 if all(item["status"] in good for item in report["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
