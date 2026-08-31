"""아카이브 v1 -> v2 비파괴 마이그레이션 CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tybot.archive.migrate import load_channel_map, migrate_archive


def discover_channel_map() -> dict[str, dict[str, str]]:
    """설정된 봇 토큰으로 채널명 -> Slack 채널 ID를 조회한다."""
    from slack_sdk import WebClient

    from tybot.envfile import load_env_file
    from tybot.workspaces import load_workspaces

    load_env_file()
    found: dict[str, dict[str, str]] = {}
    for workspace in load_workspaces():
        client = WebClient(token=workspace.bot_token)
        channels: dict[str, str] = {}
        cursor = None
        while True:
            response = client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=False,
                limit=200,
                cursor=cursor,
            )
            for channel in response.get("channels", []):
                if channel.get("id") and channel.get("name"):
                    channels[f"#{channel['name']}"] = channel["id"]
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        found[workspace.key] = channels
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="TYBot 아카이브 v2 비파괴 전환")
    parser.add_argument("--archive", default="./archive", help="ARCHIVE_DIR 경로")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--channel-map", help="워크스페이스/채널명/Slack ID JSON")
    source.add_argument(
        "--discover-slack",
        action="store_true",
        help="현재 TYBOT_ENV_FILE의 워크스페이스 토큰으로 채널 ID 조회",
    )
    parser.add_argument(
        "--apply", action="store_true", help="실제 복사. 생략하면 변경하지 않고 계획만 출력"
    )
    parser.add_argument(
        "--export-channel-map",
        help="--discover-slack 조회 결과를 사람이 검토할 JSON 파일로 저장",
    )
    args = parser.parse_args()

    if args.apply and args.discover_slack:
        parser.error("실제 적용은 사람이 검토한 --channel-map 파일로만 가능합니다")
    if args.export_channel_map and not args.discover_slack:
        parser.error("--export-channel-map은 --discover-slack과 함께 사용해야 합니다")

    channel_map = discover_channel_map() if args.discover_slack else load_channel_map(args.channel_map)
    if args.export_channel_map:
        export_path = Path(args.export_channel_map)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(
            json.dumps(channel_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    report = migrate_archive(Path(args.archive), channel_map, apply=args.apply)
    print(report.to_json())
    if args.apply and (
        report.unresolved_channels or report.blocked_files or report.broken_files
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
