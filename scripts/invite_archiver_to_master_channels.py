#!/usr/bin/env python3
"""Invite the Archiving Bot to every public/private channel joined by Master.

초대는 곧 **운영 수집**이다(2026-11-18 오너 결정: shadow 수집 폐기). Archiver 가
참여하면 다음 멤버십 동기화가 그 채널을 `active` · `writer_owner=archiver` 로
등록하고, 인수 좌표(cutover_ts)는 DB 가 「등록 시각 + 60초」 로 박는다. Master 는
판정 캐시가 만료되는 대로 그 채널의 운영 원문 쓰기를 멈춘다.

- 기본은 dry-run 이다. 대상 수를 확인한 뒤 ``--apply`` 를 붙인다
- 전역 스위치 ``archiver_writes_live`` 가 꺼져 있으면 ``--apply`` 를 거부한다.
  꺼진 채 초대하면 등록이 보류되어(Master 가 계속 씀) 초대한 의미가 없다
- 콘솔에서 사람이 멈춘 채널(``operator_hold``)은 초대하지 않는다
- 토큰은 서버 설정에서 읽고 출력하지도, 명령행으로 받지도 않는다
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

LIVE_SWITCH = "archiver_writes_live"


class LiveSwitchOff(RuntimeError):
    """전역 스위치가 꺼져 있어 초대해도 운영 수집으로 등록되지 않는다."""


@dataclass(frozen=True)
class InviteResult:
    candidates: int
    invited: int
    already_joined: int
    failed: tuple[tuple[str, str], ...]
    #: 사람이 멈춘 채널이라 초대하지 않은 수. 초대해도 paused 로 남는다.
    held: int = 0


def invite_missing(
    master_client,
    archiver_client,
    *,
    apply: bool,
    held_ids: frozenset[str] = frozenset(),
    live_switch_on: bool | None = None,
) -> InviteResult:
    """Mirror Master's channel membership to Archiver, excluding DMs.

    ``held_ids`` 는 콘솔에서 ``operator_hold`` 로 멈춘 채널이다 — 초대하지 않는다.
    ``live_switch_on`` 이 참이 아니면 실제 초대를 거부한다(모르면 거부).
    """
    master_channels = fetch_joined(master_client)
    archiver_ids = {channel.channel_id for channel in fetch_joined(archiver_client)}
    not_joined = [
        channel for channel in master_channels if channel.channel_id not in archiver_ids
    ]
    missing = [channel for channel in not_joined if channel.channel_id not in held_ids]
    held = len(not_joined) - len(missing)
    failed: list[tuple[str, str]] = []
    invited = 0

    if apply and live_switch_on is not True:
        raise LiveSwitchOff(
            "archiver_writes_live 가 켜져 있지 않습니다. 초대해도 운영 수집으로 등록되지 않습니다."
        )

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
        held=held,
    )


def _collection_state(workspace: str, *, repo=None) -> tuple[frozenset[str], bool | None]:
    """콘솔 DB 에서 멈춘 채널과 전역 스위치를 읽는다. 못 읽으면 스위치는 None(모름).

    콘솔 역할로 읽는다 — Archiver 런타임 DB 역할을 쓰지 않는다.
    """
    if repo is None:
        from tybot.console.archiving_repo import PostgresArchivingRepo

        repo = PostgresArchivingRepo()
    held = frozenset(
        str(row.get("channel_id"))
        for row in repo.channels(workspace)
        if bool(row.get("operator_hold"))
    )
    flag = repo.flag(LIVE_SWITCH, "global", "")
    return held, bool(flag and flag.get("enabled") is True)


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
        held_ids, live_on = _collection_state(workspace)
    except Exception as exc:  # noqa: BLE001 - DSN 등 상세는 출력하지 않는다
        print(f"수집 상태 DB 확인 실패: {type(exc).__name__}")
        held_ids, live_on = frozenset(), None
    try:
        master_client, archiver_client = _clients(workspace)
        result = invite_missing(
            master_client, archiver_client, apply=args.apply,
            held_ids=held_ids, live_switch_on=live_on,
        )
    except LiveSwitchOff:
        print("초대 거부: 전역 스위치(archiver_writes_live)가 켜져 있지 않거나 확인되지 않았습니다.")
        print("  꺼진 채 초대하면 등록이 보류되고 Master 가 계속 씁니다.")
        print("  콘솔 > 아카이빙에서 스위치를 켠 뒤 다시 실행하세요.")
        return 3
    except Exception as exc:  # noqa: BLE001 - never include token-bearing exception details
        print(f"초대 준비 실패: {type(exc).__name__}")
        return 2

    switch = {True: "켜짐", False: "꺼짐", None: "확인 실패"}[live_on]
    print(f"Master와 Archiver가 이미 함께 참여: {result.already_joined}개")
    print(f"Archiver 초대 대상: {result.candidates}개")
    print(f"사람이 멈춘 채널(operator_hold)이라 제외: {result.held}개")
    print(f"전역 스위치 archiver_writes_live: {switch}")
    print("초대된 채널은 다음 멤버십 동기화(최대 5분)에서 운영 수집(active · writer=archiver)으로")
    print("등록됩니다. 인수 좌표는 등록 시각 + 60초이며, 그 전까지는 Master 도 Archiver 도 쓰지 않는")
    print("구간이 생길 수 있습니다(범위는 DB cutover_at~cutover_ts 에 남습니다).")
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
