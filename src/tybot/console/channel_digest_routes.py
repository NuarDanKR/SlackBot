"""콘솔 「운영 > 검색 다이제스트」 API (B-93 2단계 §7-5).

설계: `docs/design/channel-day-digest.md` §6.

- **본문은 보이지 않는다.** 다이제스트는 검색 색인이지 읽을거리가 아니다. 화면에는 채널별
  날짜·건수·stale 수만 나간다
- 소급은 비용이 든다(CLAUDE.md 운영 원칙). 채널·기간을 고르고 **분량을 먼저 센 뒤** 실행한다.
  실행 요청은 미리보기에서 본 호출 수(`expectedCalls`)를 함께 보내고, 서버가 다시 센 값이
  그보다 크면 거절한다 — 사이에 원문이 늘었으면 사람이 다시 봐야 한다
- 실행은 배치 스크립트를 **별도 프로세스**로 띄운다. 날마다 커밋하므로 콘솔이 재시작돼도
  만든 것은 남고, 다시 실행하면 지문이 같은 날은 건너뛴다
- 전체 워크스페이스 관리자만 실행한다. 실행자·사유·범위·호출 수를 감사 기록에 남긴다
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import ConsoleUser

logger = logging.getLogger(__name__)

#: 콘솔에서 한 번에 실행할 수 있는 호출 상한.
MAX_CALLS = 1_000
#: 한 번에 고를 수 있는 기간(일).
MAX_RANGE_DAYS = 400

_STATUS_SQL = """SELECT s.workspace, s.channel_id, max(s.channel_name) AS channel_name,
       count(d.id) FILTER (WHERE d.stale_at IS NULL) AS current,
       count(d.id) FILTER (WHERE d.stale_at IS NOT NULL) AS stale,
       max(d.day) FILTER (WHERE d.stale_at IS NULL) AS latest_day,
       max(d.created_at) AS latest_created
  FROM bot_source_channel s
  LEFT JOIN channel_day_digest d
    ON d.workspace = s.workspace AND d.channel_id = s.channel_id
 WHERE s.bot = %s AND s.removed_at IS NULL
 GROUP BY s.workspace, s.channel_id
 ORDER BY s.workspace, s.channel_id"""


class RangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[str] = Field(min_length=1, max_length=200)
    since: date
    until: date


class RunBody(RangeBody):
    expectedCalls: int = Field(ge=1, le=MAX_CALLS)
    maxCalls: int = Field(ge=1, le=MAX_CALLS)
    reason: str = Field(min_length=1, max_length=500)


def _master():
    import psycopg

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL 이 없습니다.")
    try:
        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="DB 에 접속하지 못했습니다.") from None


def _archive() -> str:
    root = os.getenv("ARCHIVE_DIR", "").strip()
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="ARCHIVE_DIR 이 없습니다.")
    return root


def status(conn, bot: str = "vipbot") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL, (bot,))
        rows = cur.fetchall()
    return [
        {
            "workspace": r["workspace"],
            "channelId": r["channel_id"],
            "channelName": r["channel_name"] or "",
            "current": int(r["current"] or 0),
            "stale": int(r["stale"] or 0),
            "latestDay": r["latest_day"].isoformat() if r["latest_day"] else None,
            "latestCreated": r["latest_created"].isoformat() if r["latest_created"] else None,
        }
        for r in rows
    ]


def _scope(conn, body: RangeBody, user: ConsoleUser, bot: str) -> list[tuple[str, str]]:
    if body.since > body.until:
        raise HTTPException(status_code=422, detail="시작일이 종료일보다 늦습니다.")
    if (body.until - body.since).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"기간은 {MAX_RANGE_DAYS}일까지입니다.")
    from .. import channel_digest_batch as batch

    allowed = batch.source_channels(conn, bot)
    wanted = set(body.channels)
    picked = [c for c in allowed if c[1] in wanted]
    if len(picked) != len(wanted):
        # 근거 채널 밖은 고를 수 없다. 조용히 빼면 사람이 고른 범위와 실행 범위가 갈린다.
        raise HTTPException(status_code=422, detail="근거 채널이 아닌 채널이 섞여 있습니다.")
    if not user.all_workspaces:
        mine = set(getattr(user, "workspaces", ()) or ())
        if any(ws not in mine for ws, _ in picked):
            raise HTTPException(status_code=403, detail="권한이 없는 워크스페이스가 섞여 있습니다.")
    return picked


def _count(conn, picked, body: RangeBody) -> dict:
    from .. import channel_digest_batch as batch

    the_plan = batch.plan(conn, _archive(), picked, since=body.since, until=body.until)
    return {"since": body.since.isoformat(), "until": body.until.isoformat(), **the_plan.to_json()}


def _launch(args: list[str]) -> int:
    """배치를 별도 프로세스로 띄운다. 콘솔 요청은 기다리지 않는다."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "build_channel_digests.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    connect: Callable | None = None,
    launch: Callable[[list[str]], int] | None = None,
    bot: str = "vipbot",
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)
    open_db = connect or _master
    do_launch = launch or _launch

    @router.get("/api/channel-digest")
    def get_status(user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        with open_db() as conn:
            try:
                rows = status(conn, bot)
            except Exception as exc:  # noqa: BLE001 - 스키마 미적용도 화면은 뜬다
                return {"bot": bot, "channels": [], "error": type(exc).__name__}
        return {
            "bot": bot,
            "channels": rows,
            "error": "",
            "maxCalls": MAX_CALLS,
            "maxRangeDays": MAX_RANGE_DAYS,
        }

    @router.post("/api/channel-digest/preview")
    def preview(body: RangeBody, request: Request, user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        check_write_request(request)
        with open_db() as conn:
            picked = _scope(conn, body, user, bot)
            return _count(conn, picked, body)

    @router.post("/api/channel-digest/run")
    def run(body: RunBody, request: Request, user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        check_write_request(request)
        if not user.all_workspaces:
            raise HTTPException(
                status_code=403, detail="전체 워크스페이스 관리자만 실행할 수 있습니다."
            )
        with open_db() as conn:
            picked = _scope(conn, body, user, bot)
            counted = _count(conn, picked, body)
        if counted["calls"] > body.expectedCalls:
            raise HTTPException(
                status_code=409,
                detail=f"미리보기 뒤 분량이 늘었습니다({body.expectedCalls} → {counted['calls']}). 다시 확인하세요.",
            )
        if counted["calls"] == 0:
            return {**counted, "started": False}
        args = [
            "--apply",
            "--since",
            body.since.isoformat(),
            "--until",
            body.until.isoformat(),
            "--max-calls",
            str(min(body.maxCalls, counted["calls"])),
            "--bot",
            bot,
        ]
        for _, channel_id in picked:
            args += ["--channel", channel_id]
        pid = do_launch(args)
        logger.warning(
            "다이제스트 소급 시작 actor=%s calls=%s pid=%s", user.email, counted["calls"], pid
        )
        audit_event(
            actor=user.email,
            category="operations",
            action="channel_digest_backfill",
            target_type="channel_digest",
            target_id=f"{body.since}..{body.until}",
            outcome="started",
            metadata={
                "channels": [c for _, c in picked],
                "calls": counted["calls"],
                "maxCalls": body.maxCalls,
                "reason": body.reason[:500],
            },
        )
        return {**counted, "started": True}

    return router


__all__ = ["MAX_CALLS", "build_router", "status"]
