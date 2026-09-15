#!/usr/bin/env python3
"""Slack 채널의 초대 이전 메시지·스레드·파일·현재 Canvas를 소급 수집한다.

기본 실행은 대상과 체크포인트만 보여 준다. 실제 Slack 조회와 아카이브 변경은
``--apply``를 붙였을 때만 수행한다. 채널별 진행 위치는 STATE_DIR에 기록하므로 긴
작업이 중단돼도 같은 명령으로 이어서 실행할 수 있다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot import search_index
from tybot.archive import writer
from tybot.archive.canvas import canvas_lines
from tybot.archive.channel_files import (
    ChannelFileScan,
    referenced_files,
    scan,
)
from tybot.archive.channel_files import (
    collect as collect_channel_files,
)
from tybot.archive.files import attachment_storage
from tybot.archive.store import ArchiveStore
from tybot.attachment_trace import confirm_archived
from tybot.channels import should_collect
from tybot.collect import HISTORY_LIMIT, PACE_SECONDS, _messages_from
from tybot.envfile import load_env_file
from tybot.lock import AlreadyRunning, LockUnavailable, instance_lock
from tybot.workspaces import load_workspaces

log = logging.getLogger("tybot.backfill_channel_history")


def member_channels(client) -> list[dict]:
    """봇이 현재 참여한 수집 대상 채널만 반환한다."""
    channels: list[dict] = []
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
                channels.append(channel)
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return channels


class ApiPacer:
    """Slack 제한이 메서드·워크스페이스별이라는 경계에 맞춘 호출 간격."""

    def __init__(self, seconds: float):
        self.seconds = max(0.0, seconds)
        self._last: dict[str, float] = {}

    def wait(self, method: str) -> None:
        previous = self._last.get(method)
        if previous is not None:
            remaining = self.seconds - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last[method] = time.monotonic()


def _paced_call(pacer: ApiPacer, method: str, function, **kwargs):
    """429이면 Slack이 지정한 시간만 기다린 뒤 제한된 횟수로 재시도한다."""
    for attempt in range(4):
        pacer.wait(method)
        try:
            return function(**kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) != 429 or attempt == 3:
                raise
            headers = getattr(response, "headers", None) or {}
            try:
                retry_after = max(1.0, float(headers.get("Retry-After", 60)))
            except (TypeError, ValueError):
                retry_after = 60.0
            log.warning("Slack %s 제한: %.0f초 뒤 재시도", method, retry_after)
            time.sleep(retry_after)
    raise RuntimeError(f"Slack {method} 재시도 횟수를 초과했습니다")


class Checkpoints:
    """업무 원문 없이 Slack 좌표와 진행 상태만 저장하는 체크포인트."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data = {"version": 1, "channels": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if loaded.get("version") == 1 and isinstance(loaded.get("channels"), dict):
                    self.data = loaded
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"체크포인트를 읽지 못했습니다: {path}: {exc}") from exc

    def get(self, workspace: str, channel_id: str) -> dict:
        key = f"{workspace}:{channel_id}"
        return self.data["channels"].setdefault(
            key,
            {
                "latest": "",
                "history_complete": False,
                "pending_threads": [],
                "canvas_complete": False,
                "files_complete": False,
            },
        )

    def reset(self, workspace: str, channel_id: str) -> None:
        self.data["channels"].pop(f"{workspace}:{channel_id}", None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)


@dataclass
class BackfillStats:
    pages: int = 0
    messages: int = 0
    written: int = 0
    files: int = 0
    failures: int = 0
    changed_paths: set[pathlib.Path] = field(default_factory=set)


def _accepted(event: dict) -> bool:
    """봇 출력과 시스템 이벤트를 원문 근거로 넣지 않는다."""
    return not event.get("bot_id") and event.get("subtype") in (None, "file_share")


def _thread_messages(
    client,
    channel_id: str,
    parent_ts: str,
    pacer: ApiPacer,
) -> list[dict]:
    replies: list[dict] = []
    cursor = None
    while True:
        response = _paced_call(
            pacer,
            "conversations.replies",
            client.conversations_replies,
            channel=channel_id,
            ts=parent_ts,
            limit=HISTORY_LIMIT,
            cursor=cursor,
        )
        for message in response.get("messages") or []:
            if str(message.get("ts") or "") != parent_ts and _accepted(message):
                replies.append(message)
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return replies


def _ingest_events(
    cfg, channel: dict, archive: str, client, events: list[dict]
) -> tuple[int, set[pathlib.Path]]:
    channel_id = str(channel["id"])
    name = "#" + str(channel["name"])
    storage = attachment_storage(archive, cfg.key, channel_id)
    name_cache: dict[str, str] = {}
    staged: list = []
    incoming: list[writer.IncomingMessage] = []
    for event in sorted(events, key=lambda row: float(row["ts"])):
        incoming.extend(
            _messages_from(
                client,
                event,
                cfg.bot_token,
                name_cache,
                storage,
                workspace=cfg.key,
                channel_id=channel_id,
                staged_out=staged,
            )
        )
    if not incoming:
        return 0, set()
    result = writer.ingest(
        archive,
        workspace=cfg.key,
        channel=name,
        channel_id=channel_id,
        messages=incoming,
        acl=[name],
    )
    if staged:
        confirm_archived(
            ArchiveStore(archive), staged, workspace=cfg.key, channel_id=channel_id
        )
    return result.written, {path.resolve() for path in result.paths}


def _sync_canvas(
    cfg, channel: dict, archive: str, client
) -> tuple[int, set[pathlib.Path]]:
    channel_id = str(channel["id"])
    name = "#" + str(channel["name"])
    storage = attachment_storage(archive, cfg.key, channel_id)
    capture = canvas_lines(client, channel_id, cfg.bot_token)
    for warning in capture.warnings:
        log.warning("[%s] %s %s", cfg.key, name, warning)
    staged: list = []
    if capture.file_ids:
        events, warnings = referenced_files(client, channel_id, capture.file_ids)
        for warning in warnings:
            log.warning("[%s] %s Canvas 첨부: %s", cfg.key, name, warning)
        staged = collect_channel_files(
            ChannelFileScan(channel_id=channel_id, candidates=events),
            cfg.bot_token,
            storage,
            workspace=cfg.key,
        )
    now = datetime.now(UTC)
    messages = [
        writer.IncomingMessage(
            ts=now, speaker="Canvas", text=line, dedupe_key=capture.dedupe_key
        )
        for line in capture.lines
    ]
    messages.extend(
        writer.IncomingMessage(ts=now, speaker="Canvas 첨부", text=line)
        for item in staged
        for line in item.lines
    )
    if not messages:
        return 0, set()
    result = writer.ingest(
        archive,
        workspace=cfg.key,
        channel=name,
        channel_id=channel_id,
        messages=messages,
        acl=[name],
    )
    if staged:
        confirm_archived(
            ArchiveStore(archive), staged, workspace=cfg.key, channel_id=channel_id
        )
    return result.written, {path.resolve() for path in result.paths}


def _sync_files(
    cfg, channel: dict, archive: str, client
) -> tuple[int, int, set[pathlib.Path]]:
    channel_id = str(channel["id"])
    name = "#" + str(channel["name"])
    storage = attachment_storage(archive, cfg.key, channel_id)
    result = scan(client, channel_id, storage)
    for warning in result.warnings:
        log.warning("[%s] %s %s", cfg.key, name, warning)
    if result.truncated:
        raise RuntimeError("채널 파일 목록을 끝까지 읽지 못했습니다")
    collected = 0
    written = 0
    changed: set[pathlib.Path] = set()
    for index, candidate in enumerate(result.candidates, start=1):
        file_name = str(candidate.get("name") or candidate.get("id") or "이름 없음")
        log.info(
            "[%s] %s 채널 파일 %d/%d 처리 중: %s",
            cfg.key,
            name,
            index,
            len(result.candidates),
            file_name,
        )
        staged = collect_channel_files(
            ChannelFileScan(channel_id=channel_id, candidates=[candidate]),
            cfg.bot_token,
            storage,
            workspace=cfg.key,
        )
        if not staged:
            continue
        messages = [
            writer.IncomingMessage(ts=datetime.now(UTC), speaker="채널 파일", text=line)
            for item in staged
            for line in item.lines
        ]
        ingest = writer.ingest(
            archive,
            workspace=cfg.key,
            channel=name,
            channel_id=channel_id,
            messages=messages,
            acl=[name],
        )
        confirm_archived(
            ArchiveStore(archive), staged, workspace=cfg.key, channel_id=channel_id
        )
        collected += len(staged)
        written += ingest.written
        changed.update(path.resolve() for path in ingest.paths)
    return collected, written, changed


def backfill_channel(
    cfg,
    channel: dict,
    archive: str,
    checkpoints: Checkpoints,
    *,
    pace: float,
    max_pages: int = 0,
    pacer: ApiPacer | None = None,
) -> BackfillStats:
    from slack_sdk import WebClient

    client = WebClient(token=cfg.bot_token)
    channel_id = str(channel["id"])
    state = checkpoints.get(cfg.key, channel_id)
    stats = BackfillStats()
    pacer = pacer or ApiPacer(pace)

    pending = list(dict.fromkeys(state.get("pending_threads") or []))
    state["pending_threads"] = []
    for parent_ts in pending:
        try:
            replies = _thread_messages(client, channel_id, parent_ts, pacer)
            written, paths = _ingest_events(cfg, channel, archive, client, replies)
            stats.messages += len(replies)
            stats.written += written
            stats.changed_paths.update(paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] %s 스레드 재시도 실패(%s): %s", cfg.key, channel_id, parent_ts, exc)
            state["pending_threads"].append(parent_ts)
            stats.failures += 1
        checkpoints.save()

    while not state.get("history_complete") and (not max_pages or stats.pages < max_pages):
        kwargs = {"channel": channel_id, "limit": HISTORY_LIMIT}
        if state.get("latest"):
            kwargs.update(latest=state["latest"], inclusive=False)
        response = _paced_call(
            pacer, "conversations.history", client.conversations_history, **kwargs
        )
        page = response.get("messages") or []
        if response.get("is_limited"):
            log.warning(
                "[%s] %s Slack 보존 한계 이전 메시지는 API에서 제공되지 않습니다",
                cfg.key,
                channel_id,
            )
            state["retention_limited"] = True
            stats.failures += 1
        if not page:
            state["history_complete"] = True
            checkpoints.save()
            break

        accepted = [message for message in page if _accepted(message)]
        events = list(accepted)
        for parent in accepted:
            if not parent.get("reply_count"):
                continue
            parent_ts = str(parent.get("ts") or "")
            try:
                events.extend(_thread_messages(client, channel_id, parent_ts, pacer))
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] %s 스레드 수집 실패(%s): %s", cfg.key, channel_id, parent_ts, exc)
                state["pending_threads"].append(parent_ts)
                stats.failures += 1

        written, paths = _ingest_events(cfg, channel, archive, client, events)
        stats.pages += 1
        stats.messages += len(events)
        stats.written += written
        stats.changed_paths.update(paths)
        state["latest"] = min(str(message["ts"]) for message in page if message.get("ts"))
        if not response.get("has_more"):
            state["history_complete"] = True
        checkpoints.save()

    if not state.get("canvas_complete"):
        try:
            written, paths = _sync_canvas(cfg, channel, archive, client)
            stats.written += written
            stats.changed_paths.update(paths)
            state["canvas_complete"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] %s Canvas 수집 실패: %s", cfg.key, channel_id, exc)
            stats.failures += 1
        checkpoints.save()

    if not state.get("files_complete"):
        try:
            files, written, paths = _sync_files(cfg, channel, archive, client)
            stats.files += files
            stats.written += written
            stats.changed_paths.update(paths)
            state["files_complete"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] %s 파일 목록 수집 실패: %s", cfg.key, channel_id, exc)
            stats.failures += 1
        checkpoints.save()

    if stats.changed_paths:
        store = ArchiveStore(archive)
        docs = [doc for doc in store.docs() if doc.path.resolve() in stats.changed_paths]
        search_index.reindex(docs, store.root)
    return stats


def _checkpoint_summary(state: dict) -> str:
    if state.get("history_complete") and not state.get("pending_threads"):
        history = "메시지 완료"
    elif state.get("latest"):
        history = f"메시지 {state['latest']} 이전부터 재개"
    else:
        history = "메시지 처음부터"
    limited = " · Slack 보존 한계" if state.get("retention_limited") else ""
    return (
        f"{history} · 스레드 재시도 {len(state.get('pending_threads') or [])}건 · "
        f"Canvas {'완료' if state.get('canvas_complete') else '대기'} · "
        f"파일 {'완료' if state.get('files_complete') else '대기'}{limited}"
    )


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제 소급 수집 실행")
    parser.add_argument("--workspace", action="append", default=[], help="워크스페이스 키(반복 가능)")
    parser.add_argument("--channel", action="append", default=[], help="Slack 채널 ID(반복 가능)")
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    parser.add_argument(
        "--state",
        default=str(pathlib.Path(os.getenv("STATE_DIR", ".")) / "history-backfill.json"),
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=float(os.getenv("COLLECT_PACE_SECONDS", PACE_SECONDS)),
        help="동일 Slack 메서드 호출 간 대기 초",
    )
    parser.add_argument("--max-pages", type=int, default=0, help="채널당 이번 실행 페이지 수(0=끝까지)")
    parser.add_argument("--restart", action="store_true", help="선택 채널 체크포인트 초기화 후 재수집")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")

    selected = set(args.workspace)
    workspaces = [cfg for cfg in load_workspaces() if not selected or cfg.key in selected]
    missing = selected - {cfg.key for cfg in workspaces}
    if missing:
        parser.error("등록되지 않은 워크스페이스: " + ", ".join(sorted(missing)))

    checkpoints = Checkpoints(pathlib.Path(args.state))
    targets: list[tuple[object, dict]] = []
    found_channel_ids: set[str] = set()
    selection_failures = 0
    from slack_sdk import WebClient

    for cfg in workspaces:
        try:
            channels = member_channels(WebClient(token=cfg.bot_token))
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] 참여 채널 목록 조회 실패: %s", cfg.key, exc)
            selection_failures += 1
            continue
        if args.channel:
            wanted = set(args.channel)
            channels = [channel for channel in channels if str(channel["id"]) in wanted]
        for channel in channels:
            channel_id = str(channel["id"])
            found_channel_ids.add(channel_id)
            if args.restart and args.apply:
                checkpoints.reset(cfg.key, str(channel["id"]))
            targets.append((cfg, channel))
            state = checkpoints.get(cfg.key, channel_id)
            if args.restart and not args.apply:
                state = {
                    "latest": "",
                    "history_complete": False,
                    "pending_threads": [],
                    "canvas_complete": False,
                    "files_complete": False,
                }
            print(f"[{cfg.key}] #{channel['name']}({channel['id']}) · {_checkpoint_summary(state)}")
    missing_channels = set(args.channel) - found_channel_ids
    if missing_channels and not selection_failures:
        parser.error(
            "봇이 참여한 수집 대상에서 찾지 못한 채널: "
            + ", ".join(sorted(missing_channels))
        )
    if args.restart and args.apply:
        checkpoints.save()
    if not args.apply:
        print(f"\n대상 채널 {len(targets)}개. 실제 수집은 같은 명령에 `--apply`를 붙이세요.")
        return 1 if selection_failures else 0

    lock = instance_lock("history-backfill")
    try:
        lock.acquire()
    except AlreadyRunning:
        print("과거 데이터 소급 수집이 이미 실행 중입니다.")
        return 0
    except LockUnavailable as exc:
        log.error("소급 수집 락을 만들지 못했습니다: %s", exc)
        return 1

    totals = BackfillStats()
    totals.failures = selection_failures
    pacers = {cfg.key: ApiPacer(args.pace) for cfg in workspaces}
    interrupted = False
    try:
        for cfg, channel in targets:
            try:
                got = backfill_channel(
                    cfg,
                    channel,
                    args.archive,
                    checkpoints,
                    pace=args.pace,
                    max_pages=max(0, args.max_pages),
                    pacer=pacers[cfg.key],
                )
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] %s 소급 수집 실패: %s", cfg.key, channel["id"], exc)
                totals.failures += 1
                continue
            totals.pages += got.pages
            totals.messages += got.messages
            totals.written += got.written
            totals.files += got.files
            totals.failures += got.failures
            print(
                f"[{cfg.key}] #{channel['name']} 완료 · 페이지 {got.pages} · "
                f"메시지/답글 {got.messages} · 원문 {got.written}줄 · 파일 {got.files}건 · "
                f"실패 {got.failures}건"
            )
    except KeyboardInterrupt:
        interrupted = True
        print("\n중단했습니다. 체크포인트와 완료된 파일을 보존했습니다. 같은 명령으로 재개하세요.")
    finally:
        lock.release()
    if interrupted:
        return 130
    print(
        f"\n페이지 {totals.pages} · 메시지/답글 {totals.messages} · "
        f"원문 {totals.written}줄 · 파일 {totals.files}건 · 실패 {totals.failures}건"
    )
    return 1 if totals.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
