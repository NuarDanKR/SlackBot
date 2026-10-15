#!/usr/bin/env python3
"""Read-only Archiver file audit across registered workspaces; never recover automatically."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from slack_sdk import WebClient

from tybot.archive.file_audit import audit
from tybot.archiving_bot import (
    load_archiver_workspaces,
    shadow_archive_dir,
    validate_slack_identity,
)
from tybot.console.archiving_repo import default_repo
from tybot.console.workspace_store import _connect
from tybot.envfile import load_env_file


def registered_workspaces() -> list[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT workspace FROM bot_connection
             WHERE bot_key = 'archiver' AND connector_type = 'slack_socket'
             ORDER BY workspace
        """)
        return [str(row["workspace"]) for row in cur.fetchall()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all-workspaces", action="store_true")
    scope.add_argument("--workspace", action="append")
    parser.add_argument("--pause", type=float, default=3.0)
    parser.add_argument("--result-json")
    args = parser.parse_args(argv)
    if not math.isfinite(args.pause) or args.pause < 0:
        parser.error("--pause must be finite and nonnegative")
    try:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        load_env_file()
        live_root = pathlib.Path(os.environ["ARCHIVE_DIR"])
        shadow_root = shadow_archive_dir()
        if os.getenv("ARCHIVER_SHADOW_LAYOUT") != "per-channel-v1":
            raise ValueError("per-channel-v1 is required")
        if not live_root.is_absolute() or not live_root.is_dir():
            raise ValueError("existing absolute archive root is required")
        output = pathlib.Path(args.result_json).resolve() if args.result_json else None
        if output and any(output.is_relative_to(root.resolve()) for root in (live_root, shadow_root)):
            raise ValueError("audit output must be outside both archives")
        workspaces = registered_workspaces() if args.all_workspaces else args.workspace

        def config_loader(workspace):
            values = dict(os.environ, ARCHIVER_CONFIG_SOURCE="db", ARCHIVER_WORKSPACE=workspace)
            cfg, = load_archiver_workspaces(values)
            return cfg

        report = audit(
            workspaces, config_loader=config_loader,
            client_factory=lambda cfg: WebClient(token=cfg.bot_token, timeout=30),
            repo=default_repo(), live_root=live_root, shadow_root=shadow_root,
            identity_validator=validate_slack_identity, pause=args.pause,
        )
        rendered = json.dumps(report, ensure_ascii=True, sort_keys=True)
        if output:
            # Refuse overwriting an existing operator file, including credentials.
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        print(rendered)
        return 0 if report["complete"] else 2
    except Exception as exc:  # noqa: BLE001 - never print decrypted tokens or SDK response body
        print(json.dumps({"read_only": True, "error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
