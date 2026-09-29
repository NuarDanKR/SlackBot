#!/usr/bin/env python3
"""Archiving Bot 이 **초대된 채널**을 DB 와 맞춘다.

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/sync_archiver_channels.py --workspace tyit --dry-run
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/sync_archiver_channels.py --workspace tyit

## 왜 필요한가

수집 대상의 정본은 **초대**다(2026-09-29 오너 지시). 초대는 Slack 화면에서 그 방
사람들이 하고, 우리 DB 는 그것을 따라가야 한다. 따라가지 않으면 두 가지가 난다.

- 초대했는데 등록이 없어 **수집이 안 되는 채널.** 그 방은 수집되는 줄 안다
- 초대를 풀었는데 목록에 남아 **권한 오류만 쌓는 채널**

봇 기동 시점에도 같은 동기화를 돌지만, 기동 사이에 생긴 초대는 이 잡이 따라잡는다.

## 무엇을 바꾸나

| 상황 | 결과 |
|---|---|
| 새로 초대됨 | 등록 + `shadow` |
| 빠짐 | `paused` · `membership=left` |
| 다시 초대됨 | `shadow` 로 재개 |
| 사람이 꺼 둔 채널 | **그대로 둔다**(`operator_hold`) |

**운영 수집(`active`)으로 자동 전환하지 않는다.** 운영 원문의 주인을 바꾸는 일은
사람이 사유와 함께 하는 것이고, 동기화 잡이 할 일이 아니다.

## 안전

- `--dry-run` 이 기본 점검 수단이다. 무엇이 바뀔지 먼저 센다
- DM 은 받지 않는다(`users.conversations` 타입 + 값 검사)
- 토큰은 로그·출력 어디에도 안 나온다
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import channel_membership  # noqa: E402


def _client(workspace: str):
    """그 워크스페이스 Archiver 의 봇 토큰으로 만든 Slack 클라이언트.

    평문은 이 함수 안에서 끝난다. 실패하면 예외 **종류만** 남긴다.
    """
    from slack_sdk import WebClient

    from tybot.archiver_runtime_store import load_runtime_config

    config = load_runtime_config(workspace)
    return WebClient(token=str(config["bot_token"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dry-run", action="store_true", help="무엇이 바뀔지만 센다")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.console.archiving_repo import default_repo
    from tybot.envfile import load_env_file

    load_env_file()
    workspace = args.workspace.strip().lower()
    repo = default_repo()

    try:
        client = _client(workspace)
    except Exception as exc:  # noqa: BLE001 - 기동 설정 실패 종류가 여럿이다
        print(f"Archiver 설정을 읽지 못했습니다: {type(exc).__name__}")
        return 2

    joined = channel_membership.fetch_joined(client)
    plan = channel_membership.plan_sync(joined, repo.channels(workspace))

    print(f"참여 중인 채널: {len(joined)}개")
    print(f"새로 등록: {len(plan.registered)}개")
    print(f"수집 재개: {len(plan.resumed)}개")
    print(f"수집 중단(봇이 빠짐): {len(plan.stopped)}개")
    for action in [*plan.registered, *plan.resumed, *plan.stopped][:20]:
        label = action.name or action.channel_id
        print(f"  · [{action.kind}] {action.channel_id} {label}")

    if args.dry_run:
        print("\ndry-run 이었습니다. 실제 반영은 --dry-run 없이 다시 실행하세요.")
        return 0

    result = channel_membership.apply_plan(repo, workspace, plan)
    print(
        f"\n반영: 등록 {result['registered']} · 재개 {result['resumed']}"
        f" · 중단 {result['stopped']} · 확인 {result['checked']}"
    )
    print("운영 수집(active)으로 바꾼 채널은 없습니다 — 그 전환은 사람이 합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
