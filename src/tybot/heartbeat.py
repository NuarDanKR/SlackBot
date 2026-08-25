"""봇 상태 파일 — 콘솔이 봇에게 직접 묻지 않고 상태를 알 수 있게 한다.

## 왜 파일인가
콘솔이 Slack 에 직접 물어보면 콘솔에도 봇 토큰이 필요하고, 화면을 열 때마다 Slack API 를
호출하게 된다(rate limit 을 콘솔이 갉아먹는다). 대신 **봇이 아는 것을 봇이 적어 두고**,
콘솔은 그 파일을 읽는다. 봇이 죽으면 파일이 낡으므로 그 사실 자체가 신호가 된다.

파일 위치: `<STATE_DIR>/status/<워크스페이스>.json`
(`STATE_DIR` 기본값은 `ARCHIVE_DIR` 의 상위 폴더 — 락 파일과 같은 자리)

아카이브가 아니라 상태 디렉터리에 둔다. `ArchiveStore` 가 이 파일을 근거로 읽는 일이 없어야 한다.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tybot.heartbeat")

# 이 시간이 지난 상태 파일은 '봇이 살아 있다'는 근거로 쓰지 않는다.
STALE_AFTER_SECONDS = 180


def state_dir() -> Path:
    explicit = os.getenv("STATE_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.getenv("ARCHIVE_DIR", "./archive")).parent


def status_path(workspace: str) -> Path:
    return state_dir() / "status" / f"{workspace}.json"


@dataclass
class BotStatus:
    """봇만 알 수 있는 것들. 파일을 읽는 쪽은 이 모양을 기대한다."""

    workspace: str
    connected: bool
    realtime: bool
    # 봇이 들어가 있는 채널 수 / 초대되지 않아 수집에서 빠진 채널 수
    channels: int
    uninvited_channels: int
    spend_today_usd: float
    limit_usd: float
    started_at: str
    updated_at: str
    # 아카이브·감사기록에 쓸 수 없는 상태면 사유. 정상이면 None
    write_problem: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def write(status: BotStatus) -> None:
    """상태를 남긴다. 실패해도 봇을 멈추지 않는다 — 상태 파일은 부가 정보다."""
    path = status_path(status.workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(status.to_json(), encoding="utf-8")
        tmp.replace(path)  # 반쯤 쓰인 파일을 콘솔이 읽지 않게 원자적으로 교체
    except Exception as e:  # noqa: BLE001 - 상태 기록 실패로 봇을 죽이지 않는다
        logger.warning("봇 상태 파일 기록 실패 (%s): %s", path, e)


def read(workspace: str) -> dict | None:
    """콘솔이 읽는다. 파일이 없거나 깨졌으면 None."""
    try:
        raw = status_path(workspace).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("봇 상태 파일이 깨졌습니다: %s", status_path(workspace))
        return None


def is_stale(status: dict, *, now: datetime | None = None) -> bool:
    """마지막 갱신이 오래됐으면 '연결됨'을 그대로 믿지 않는다."""
    stamp = status.get("updated_at")
    if not stamp:
        return True
    try:
        updated = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return (current - updated).total_seconds() > STALE_AFTER_SECONDS


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")
