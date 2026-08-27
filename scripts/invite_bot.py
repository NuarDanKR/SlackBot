#!/usr/bin/env python3
"""비공개 채널에 봇을 일괄 초대한다 — 관리자가 자기 PC에서 한 번 실행하는 도구.

    export SLACK_ADMIN_TOKEN=xoxp-...        # 실행 시에만. 서버에 저장하지 않는다
    export SLACK_BOT_TOKEN=xoxb-...          # 봇 user id 조회용(읽기만)
    python scripts/invite_bot.py --dry-run
    python scripts/invite_bot.py

## 왜 이 도구가 필요한가
**봇 토큰으로는 비공개 채널에 자가 참여가 불가능하다.** `conversations.join` 은 공개 채널
전용이고, 봇은 자기가 없는 비공개 채널을 목록 조회조차 못 한다(Slack 설계).

그래서 사람 토큰이 필요하다. 다만 **관리자 사용자 토큰은 워크스페이스 전반 권한**을 갖는 자산이라
봇 프로세스에 상주시키지 않는다. 이 스크립트는 실행 시 환경변수로만 받고 저장하지 않는다.
(같은 원칙: docs/design/identity-and-legacy-login.md 5절)

## 한계 — 미리 알고 시작할 것
- 이 토큰의 주인이 **멤버인 비공개 채널만** 보이고 초대할 수 있다.
  관리자가 안 들어가 있는 비공개 채널은 그 채널 사람이 직접 `/invite` 해야 한다.
- Enterprise Grid 라면 `admin.conversations.invite` 로 전체를 처리할 수 있다(현재 플랜 밖).

필요 스코프(사용자 토큰): `groups:read`, `groups:write`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tybot.channels import parse


def _client(token: str):
    try:
        from slack_sdk import WebClient
    except ImportError:
        print("slack_sdk 미설치: pip install slack-sdk")
        raise SystemExit(1) from None
    return WebClient(token=token)


def bot_user_id(bot_token: str) -> str:
    return _client(bot_token).auth_test()["user_id"]


def private_channels(admin_token: str) -> list[dict]:
    """관리자가 멤버인 비공개 채널 목록."""
    client = _client(admin_token)
    out: list[dict] = []
    cursor = None
    while True:
        res = client.conversations_list(
            types="private_channel", exclude_archived=True, limit=200, cursor=cursor
        )
        out.extend(res.get("channels", []))
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def invite(admin_token: str, bot_id: str, *, dry_run: bool = False) -> int:
    client = _client(admin_token)
    invited, skipped, already, failed = [], [], [], []

    for ch in private_channels(admin_token):
        name = "#" + ch.get("name", "")
        if parse(name) is None:
            skipped.append(name)
            continue
        if dry_run:
            invited.append(name)
            continue
        try:
            client.conversations_invite(channel=ch["id"], users=bot_id)
            invited.append(name)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            (already if "already_in_channel" in msg else failed).append(name)

    verb = "초대 예정" if dry_run else "초대 완료"
    print(f"{verb}: {len(invited)}건")
    for n in invited:
        print(f"  + {n}")
    if already:
        print(f"이미 참여 중: {len(already)}건")
    if skipped:
        print(f"규칙 밖이라 건너뜀: {len(skipped)}건")
        for n in skipped[:10]:
            print(f"  - {n}")
    if failed:
        print(f"실패: {len(failed)}건")
        for n in failed:
            print(f"  ! {n}")
    if not dry_run and invited:
        print("\n초대된 채널은 지금부터 실시간 수집됩니다.")
        print("과거 대화는 해당 채널에서 `@tybot 수집` 으로 일부(요청당 15건) 보충할 수 있습니다.")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="규칙에 맞는 비공개 채널에 봇 일괄 초대")
    ap.add_argument("--dry-run", action="store_true", help="초대 대상만 출력")
    a = ap.parse_args()

    admin = os.getenv("SLACK_ADMIN_TOKEN", "")
    bot = os.getenv("SLACK_BOT_TOKEN", "")
    if not admin.startswith("xoxp-"):
        print("SLACK_ADMIN_TOKEN(사용자 토큰 xoxp-)이 필요합니다.")
        print("  Slack 앱 설정 → OAuth & Permissions → User Token Scopes 에")
        print("  groups:read, groups:write 를 추가하고 재설치해 발급합니다.")
        print("  이 토큰은 서버(.env)에 저장하지 마세요. 실행 시에만 환경변수로 넘깁니다.")
        return 1
    if not bot.startswith("xoxb-"):
        print("SLACK_BOT_TOKEN(봇 토큰)이 필요합니다 - 봇 user id 를 조회합니다.")
        return 1

    try:
        bot_id = bot_user_id(bot)
    except Exception as e:  # noqa: BLE001
        print(f"봇 정보 조회 실패: {e}")
        return 1
    print(f"봇 user id: {bot_id}")
    return invite(admin, bot_id, dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
