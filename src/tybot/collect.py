"""정기 백필 잡 — 실시간 수집이 놓친 구간을 메운다.

`python -m tybot.collect` (systemd 타이머가 업무시간 매시 정시에 실행)

## rate limit 사실관계
Slack 은 2025-05 이후 만들어진 **비-마켓플레이스 앱**의 `conversations.history` 를
**분당 1요청 / 요청당 15건**으로 제한한다. "하루 15회"가 아니라 **요청당 15건**이다.

따라서:
- 매시 정시에 돌리면 채널당 시간별 최대 15건, 하루 13회 × 15건 = **채널당 하루 195건**.
- 그보다 대화가 많은 채널은 배치만으로 못 따라간다 → **실시간 수집이 본선**이고
  이 잡은 봇 재시작·네트워크 단절 구간을 메우는 보조 경로다.
- 채널이 N개면 페이싱 때문에 최소 N분이 걸린다. 그래서 채널 사이에 60초 이상 쉰다.

## 하는 일
1. 봇이 멤버인 채널 중 이름 규칙에 맞는 채널만 선택
2. 채널마다 최근 15건과 캔버스 스냅샷을 원문에 append (멱등)
3. 결과를 로그로 남기고, 새로 쌓인 게 있으면 건수를 집계
"""
from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from .archive import writer
from .archive.canvas import canvas_lines
from .archive.files import attachment_storage, stage_files
from .channels import should_collect
from .envfile import load_env_file
from .lock import AlreadyRunning, LockUnavailable, instance_lock
from .workspaces import load_workspaces

log = logging.getLogger("tybot.collect")

HISTORY_LIMIT = 15  # 신규 앱 요청당 상한
PACE_SECONDS = 65  # 분당 1요청 제한 + 여유


def _messages_from(client, event: dict, bot_token: str, name_cache: dict, storage) -> list:
    ts = datetime.fromtimestamp(float(event["ts"]), tz=UTC)
    uid = event.get("user", "unknown")
    if uid not in name_cache:
        try:
            info = client.users_info(user=uid)["user"]
            name_cache[uid] = (
                info.get("profile", {}).get("real_name") or info.get("name") or uid
            )
        except Exception:
            name_cache[uid] = uid
    speaker = name_cache[uid]

    out = []
    body = (event.get("text") or "").strip()
    if body:
        out.append(writer.IncomingMessage(ts=ts, speaker=speaker, text=body))
    if event.get("files"):
        lines, warns = stage_files(event["files"], bot_token, storage)
        out.extend(writer.IncomingMessage(ts=ts, speaker=speaker, text=ln) for ln in lines)
        for w in warns:
            log.warning("첨부 처리 경고: %s", w)
    return out


def collect_workspace(cfg, archive_dir: str, *, pace: float = PACE_SECONDS) -> dict:
    from slack_sdk import WebClient

    client = WebClient(token=cfg.bot_token)
    stats = {"channels": 0, "written": 0, "skipped": 0, "skipped_rule": 0, "failed": 0}
    name_cache: dict[str, str] = {}

    try:
        channels = []
        cursor = None
        while True:
            res = client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for channel in res.get("channels", []):
                if not channel.get("is_member"):
                    continue
                if not should_collect("#" + channel.get("name", "")):
                    stats["skipped_rule"] += 1
                    continue
                channels.append(channel)
            cursor = (res.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        log.error("[%s] 채널 목록 조회 실패: %s", cfg.key, e)
        stats["failed"] += 1
        return stats

    log.info("[%s] 대상 채널 %d개 (채널당 %.0f초 간격)", cfg.key, len(channels), pace)

    for i, ch in enumerate(channels):
        name = "#" + ch["name"]
        storage = attachment_storage(archive_dir, cfg.key, ch["id"])
        if i:
            time.sleep(pace)  # 분당 1요청 페이싱
        try:
            res = client.conversations_history(channel=ch["id"], limit=HISTORY_LIMIT)
        except Exception as e:
            log.warning("[%s] %s 히스토리 실패: %s", cfg.key, name, e)
            stats["failed"] += 1
            continue

        msgs = []
        for m in reversed(res.get("messages", [])):
            if m.get("bot_id") or m.get("subtype") not in (None, "file_share"):
                continue
            msgs.extend(_messages_from(client, m, cfg.bot_token, name_cache, storage))

        canvas = canvas_lines(client, ch["id"], cfg.bot_token)
        if canvas.lines:
            now = datetime.now(UTC)
            msgs.extend(
                writer.IncomingMessage(
                    ts=now,
                    speaker="캔버스",
                    text=line,
                    dedupe_key=canvas.dedupe_key,
                )
                for line in canvas.lines
            )
        for warning in canvas.warnings:
            log.warning("[%s] %s 캔버스 처리 경고: %s", cfg.key, name, warning)
        if not msgs:
            continue

        try:
            r = writer.ingest(
                archive_dir,
                workspace=cfg.key,
                channel=name,
                channel_id=ch["id"],
                messages=msgs,
                acl=[name],
            )
        except Exception as e:
            log.error("[%s] %s 저장 실패(형식 검사): %s", cfg.key, name, e)
            stats["failed"] += 1
            continue

        stats["channels"] += 1
        stats["written"] += r.written
        stats["skipped"] += len(msgs) - r.written
        if r.written:
            log.info("[%s] %s 신규 %d건", cfg.key, name, r.written)
        if r.refused:
            log.warning("[%s] %s 제외 대상 %d건", cfg.key, name, len(r.refused))
    return stats


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("환경설정 출처: %s", load_env_file())

    # 앞 회차가 아직 안 끝났는데 타이머가 다시 뜨면(채널이 많으면 회차가 길어진다)
    # 같은 채널을 두 번 긁어 rate limit 만 소모한다. 봇 프로세스와는 다른 이름이라 서로 막지 않는다.
    lock = instance_lock("collect")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        log.warning("앞 회차 백필이 아직 실행 중입니다. 이번 회차는 건너뜁니다 — %s", e)
        return 0
    except LockUnavailable as e:
        log.error("단일 실행 락을 만들 수 없어 백필을 멈춥니다 — %s", e)
        return 1

    try:
        return _collect_all()
    finally:
        lock.release()


def _collect_all() -> int:
    archive_dir = os.getenv("ARCHIVE_DIR", "./archive")
    pace = float(os.getenv("COLLECT_PACE_SECONDS", PACE_SECONDS))

    total = {"channels": 0, "written": 0, "skipped": 0, "failed": 0}
    for cfg in load_workspaces():
        s = collect_workspace(cfg, archive_dir, pace=pace)
        for k in total:
            total[k] += s[k]
    log.info(
        "백필 완료 - 채널 %d, 신규 %d건, 중복 %d건, 실패 %d",
        total["channels"], total["written"], total["skipped"], total["failed"],
    )
    return 1 if total["failed"] and not total["written"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
