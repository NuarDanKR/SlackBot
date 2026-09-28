#!/usr/bin/env python3
"""Invite the Archiving Bot to every public/private channel joined by Master.

The default is a dry run. Use ``--apply`` only after reviewing the candidate
count. Tokens are loaded from the existing runtime configuration and are never
printed or accepted as command-line arguments.
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive.channel_membership import fetch_joined  # noqa: E402


@dataclass(frozen=True)
class InviteResult:
    candidates: int
    invited: int
    already_joined: int
    failed: tuple[tuple[str, str], ...]


def invite_missing(master_client, archiver_client, *, apply: bool) -> InviteResult:
    """Mirror Master's channel membership to Archiver, excluding DMs."""
    master_channels = fetch_joined(master_client)
    archiver_ids = {channel.channel_id for channel in fetch_joined(archiver_client)}
    missing = [channel for channel in master_channels if channel.channel_id not in archiver_ids]
    failed: list[tuple[str, str]] = []
    invited = 0

    if apply:
        archiver_id = str(archiver_client.auth_test().get("user_id") or "")
        if not archiver_id:
            raise RuntimeError("Archiver Slack identity did not include user_id")
        for channel in missing:
            try:
                master_client.conversations_invite(
                    channel=channel.channel_id,
                    users=archiver_id,
                )
                invited += 1
            except Exception as exc:  # noqa: BLE001 - Slack errors are reported per channel
                error = getattr(exc, "response", {}).get("error") or type(exc).__name__
                if error == "already_in_channel":
                    continue
                failed.append((channel.channel_id, str(error)))

    return InviteResult(
        candidates=len(missing),
        invited=invited,
        already_joined=len(archiver_ids & {c.channel_id for c in master_channels}),
        failed=tuple(failed),
    )


def _clients(
    workspace: str,
    *,
    client_factory=None,
    master_loader=None,
    token_loader=None,
):
    if client_factory is None:
        from slack_sdk import WebClient

        client_factory = WebClient
    if master_loader is None:
        from tybot.workspaces import load_workspaces

        master_loader = load_workspaces
    if token_loader is None:
        from tybot.console.bot_identity import bot_token_for_admin_operation

        token_loader = bot_token_for_admin_operation

    master = next((row for row in master_loader() if row.key == workspace), None)
    if master is None:
        raise RuntimeError(f"Master workspace configuration not found: {workspace}")
    archiver_bot_token = token_loader(workspace, "archiver")
    try:
        return (
            client_factory(token=master.bot_token),
            client_factory(token=archiver_bot_token),
        )
    finally:
        archiver_bot_token = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--apply", action="store_true", help="actually invite Archiver")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.envfile import load_env_file

    load_env_file()
    workspace = args.workspace.strip().lower()
    try:
        master_client, archiver_client = _clients(workspace)
        result = invite_missing(master_client, archiver_client, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 - never include token-bearing exception details
        print(f"초대 준비 실패: {type(exc).__name__}")
        return 2

    print(f"Master와 Archiver가 이미 함께 참여: {result.already_joined}개")
    print(f"Archiver 초대 대상: {result.candidates}개")
    if not args.apply:
        print("dry-run 이었습니다. 실제 초대는 --apply를 붙여 다시 실행하세요.")
        return 0
    print(f"초대 완료: {result.invited}개")
    if result.failed:
        print(f"초대 실패: {len(result.failed)}개")
        for channel_id, error in result.failed:
            print(f"  · {channel_id}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
