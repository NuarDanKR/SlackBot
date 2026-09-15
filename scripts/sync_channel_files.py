#!/usr/bin/env python3
"""Slack 채널 파일 탭의 미수집 파일을 찾고 명시적으로 수집한다(B-46).

기본 실행은 조회만 한다. ``--apply``를 붙여야 다운로드·변환·원문 반영·색인을
수행한다. 모든 ``files.list`` 호출에는 채널 ID가 들어가며 워크스페이스 전체 파일
목록은 요청하지 않는다.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot import search_index
from tybot.archive import writer
from tybot.archive.channel_files import collect, scan
from tybot.archive.files import attachment_storage
from tybot.archive.store import ArchiveStore
from tybot.attachment_trace import confirm_archived
from tybot.channels import should_collect
from tybot.envfile import load_env_file
from tybot.workspaces import load_workspaces

log = logging.getLogger("tybot.sync_channel_files")


def member_channels(client) -> list[dict]:
    """봇이 참여한 수집 대상 채널만 반환한다."""
    out: list[dict] = []
    cursor = None
    while True:
        response = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        for channel in response.get("channels") or []:
            name = "#" + str(channel.get("name") or "")
            if channel.get("is_member") and channel.get("id") and should_collect(name):
                out.append(channel)
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def _publish(archive: str, cfg, channel: dict, staged: list) -> tuple[int, bool]:
    """staging 결과를 원문에 추가하고 해당 문서를 즉시 색인한다."""
    if not staged:
        return 0, True
    now = datetime.now(UTC)
    messages = [
        writer.IncomingMessage(ts=now, speaker="채널 파일", text=line)
        for item in staged
        for line in item.lines
    ]
    name = "#" + str(channel["name"])
    result = writer.ingest(
        archive,
        workspace=cfg.key,
        channel=name,
        channel_id=str(channel["id"]),
        messages=messages,
        acl=[name],
    )
    store = ArchiveStore(archive)
    states = confirm_archived(
        store,
        staged,
        workspace=cfg.key,
        channel_id=str(channel["id"]),
    )
    confirmed = all(states.get(item.file_id) == "archived" for item in staged)
    if not confirmed:
        log.error("[%s] %s 첨부 원문 반영을 확인하지 못했습니다", cfg.key, name)

    changed = {path.resolve() for path in result.paths}
    docs = [doc for doc in store.docs() if doc.path.resolve() in changed]
    try:
        search_index.reindex(docs, store.root)
    except search_index.IndexError_ as exc:
        # 원문은 이미 보존됐다. 색인 실패를 수집 실패로 가장하지 않고 명시한다.
        log.error("[%s] %s 검색 색인 실패: %s", cfg.key, name, exc)
        return result.written, False
    return result.written, confirmed


def sync_workspace(
    cfg,
    archive: str,
    *,
    apply: bool,
    channel_ids: set[str] | None = None,
    pace: float = 3.0,
) -> dict[str, int]:
    """워크스페이스의 참여 채널을 채널별로 조회한다."""
    from slack_sdk import WebClient

    client = WebClient(token=cfg.bot_token)
    stats = {"channels": 0, "listed": 0, "known": 0, "missing": 0, "collected": 0,
             "written": 0, "failed": 0}
    try:
        channels = member_channels(client)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] 채널 목록 조회 실패: %s", cfg.key, exc)
        stats["failed"] += 1
        return stats
    if channel_ids:
        channels = [channel for channel in channels if str(channel["id"]) in channel_ids]

    for index, channel in enumerate(channels):
        if index and pace:
            time.sleep(pace)
        channel_id = str(channel["id"])
        name = "#" + str(channel["name"])
        storage = attachment_storage(archive, cfg.key, channel_id)
        result = scan(client, channel_id, storage)
        stats["channels"] += 1
        stats["listed"] += result.total
        stats["known"] += result.known
        stats["missing"] += result.missing
        print(f"[{cfg.key}] {name}({channel_id}) · {result.summary()}")
        for warning in result.warnings:
            log.warning("[%s] %s %s", cfg.key, name, warning)
        if result.truncated:
            stats["failed"] += 1
        if not apply or not result.candidates:
            continue
        staged = collect(result, cfg.bot_token, storage, workspace=cfg.key)
        stats["collected"] += len(staged)
        written, complete = _publish(archive, cfg, channel, staged)
        stats["written"] += written
        if not complete:
            stats["failed"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="미수집 파일을 실제로 수집")
    parser.add_argument("--workspace", action="append", default=[], help="워크스페이스 키")
    parser.add_argument("--channel", action="append", default=[], help="Slack 채널 ID")
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    parser.add_argument("--pace", type=float, default=3.0, help="채널 조회 사이 대기 초")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")

    selected = set(args.workspace)
    workspaces = [cfg for cfg in load_workspaces() if not selected or cfg.key in selected]
    if selected - {cfg.key for cfg in workspaces}:
        parser.error("등록되지 않은 워크스페이스 키가 있습니다")

    total = {key: 0 for key in ("channels", "listed", "known", "missing", "collected",
                                "written", "failed")}
    for cfg in workspaces:
        got = sync_workspace(
            cfg,
            args.archive,
            apply=args.apply,
            channel_ids=set(args.channel) or None,
            pace=max(0.0, args.pace),
        )
        for key in total:
            total[key] += got[key]
    print(
        "\n채널 {channels}개 · 파일 {listed}건 · 기존 {known}건 · 미수집 {missing}건 · "
        "수집 {collected}건 · 원문 {written}줄 · 실패 {failed}건".format(**total)
    )
    if not args.apply and total["missing"]:
        print("실제로 수집하려면 같은 명령에 `--apply`를 붙이세요.")
    return 1 if total["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
