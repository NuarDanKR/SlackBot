"""채널×일 다이제스트 배치 — 대상·분량 계산·실행 (B-93 2단계 §7-3).

설계: `docs/design/channel-day-digest.md` §4.

LLM 비용이 드는 일이라 **먼저 센다**(CLAUDE.md 운영 원칙). `plan()` 은 아무것도 쓰지
않고 「몇 채널·며칠·몇 번 호출·대략 몇 토큰」 을 돌려준다. `run()` 은 그 계획을 상한
(`max_calls`) 안에서만 실행한다.

DB 는 **전용 로그인 계정**(`tybot_digest_runtime`, `tybot_digest` 그룹 멤버)으로만 붙는다.
Master(`DATABASE_URL`)로 내려가지 않는다 — Master 는 다이제스트를 읽기만 하고, 쓰는
쪽이 섞이면 누가 무엇을 썼는지가 흐려진다.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import channel_digest, channel_digest_build

RUNTIME_ROLE = "tybot_digest_runtime"
DSN_FILE_ENV = "TYBOT_DIGEST_DATABASE_URL_FILE"
DEFAULT_DSN_FILE = Path("/etc/tybot/tybot-digest-database-url")

#: 다이제스트 모델. 색인 문장이라 저가 모델로 충분하다(판단·답변이 아니다).
DIGEST_MODEL = "claude-haiku-4-5"
DAILY_LIMIT_ENV = "TYBOT_DIGEST_DAILY_LIMIT_USD"
DEFAULT_DAILY_LIMIT_USD = 5.0
#: 매일 밤 다시 보는 최근 일 수. 그 안에서도 지문이 같은 날은 건너뛴다.
NIGHTLY_DAYS = 3
#: 한 번 실행에서 부를 수 있는 LLM 호출 상한(기본).
DEFAULT_MAX_CALLS = 200
#: 토큰 추정: 한국어 원문 글자 → 입력 토큰 대략 배수. 미리보기용이고 청구 기준이 아니다.
CHARS_PER_TOKEN = 1.5


class DigestDsnError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def resolve_dsn() -> str:
    """전용 DSN. 파일 권한(0600)·URI 형식·접속 계정을 확인한다. Master 로 내려가지 않는다."""
    from .console.vision_dsn import VisionDsnError, _dsn_login, _read_file

    explicit = os.getenv(DSN_FILE_ENV, "").strip()
    path = Path(explicit) if explicit else DEFAULT_DSN_FILE
    try:
        url = _read_file(path, explicit=True)
    except VisionDsnError as exc:
        raise DigestDsnError(exc.code.replace("vision_", "digest_")) from None
    scheme, user = _dsn_login(url)
    if scheme not in {"postgresql", "postgres"}:
        raise DigestDsnError("digest_database_not_uri")
    if user != RUNTIME_ROLE:
        raise DigestDsnError("digest_database_wrong_role")
    if url == os.getenv("DATABASE_URL", "").strip():
        raise DigestDsnError("digest_database_is_master")
    return url


def source_channels(connection, bot: str = "vipbot") -> list[tuple[str, str]]:
    """대상 채널 — 그 봇의 근거 채널(제거되지 않은 것). `(workspace, channel_id)`."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT workspace, channel_id FROM bot_source_channel
               WHERE bot = %s AND removed_at IS NULL ORDER BY workspace, channel_id""",
            (bot,),
        )
        rows = cur.fetchall()
    return [
        (r["workspace"], r["channel_id"]) if isinstance(r, dict) else (r[0], r[1]) for r in rows
    ]


def nightly_window(today: date) -> tuple[date, date]:
    """매일 밤 범위: 최근 `NIGHTLY_DAYS` 일(오늘 제외 — 아직 끝나지 않은 날)."""
    return today - timedelta(days=NIGHTLY_DAYS), today - timedelta(days=1)


@dataclass
class Plan:
    channels: int = 0
    days_seen: int = 0
    unchanged: int = 0
    todo: list = field(default_factory=list)  # list[DayInput]
    input_chars: int = 0

    @property
    def calls(self) -> int:
        return len(self.todo)

    @property
    def est_input_tokens(self) -> int:
        return int(self.input_chars / CHARS_PER_TOKEN)

    def to_json(self) -> dict:
        return {
            "channels": self.channels,
            "days_seen": self.days_seen,
            "unchanged": self.unchanged,
            "calls": self.calls,
            "est_input_tokens": self.est_input_tokens,
        }


def plan(
    connection,
    root: Path | str,
    channels,
    *,
    since: date,
    until: date,
    model: str = DIGEST_MODEL,
) -> Plan:
    """무엇을 만들지 센다. **쓰지 않는다.** 지문이 현재 판과 같은 날은 건너뛴다."""
    out = Plan()
    for workspace, channel_id in channels:
        out.channels += 1
        current = channel_digest_build.current_revisions(connection, workspace, channel_id)
        for day in channel_digest.collect_channel_days(root, workspace, channel_id):
            if not (since <= day.day <= until) or day.empty:
                continue
            out.days_seen += 1
            if current.get(day.day) == channel_digest.revision(day, model=model):
                out.unchanged += 1
                continue
            out.todo.append(day)
            out.input_chars += len(channel_digest.render_input(day).text)
    return out


@dataclass
class RunStats:
    made: int = 0
    rejected: Counter = field(default_factory=Counter)
    skipped_over_cap: int = 0
    cost_usd: float = 0.0

    def to_json(self) -> dict:
        return {
            "made": self.made,
            "rejected": dict(self.rejected),
            "skipped_over_cap": self.skipped_over_cap,
            "cost_usd": round(self.cost_usd, 4),
        }


def run(
    connection,
    the_plan: Plan,
    router,
    *,
    model: str = DIGEST_MODEL,
    max_calls: int = DEFAULT_MAX_CALLS,
) -> RunStats:
    """계획을 상한 안에서 실행한다. 날마다 커밋한다 — 중간에 멈춰도 만든 것은 남는다."""
    stats = RunStats()
    for index, day in enumerate(the_plan.todo):
        if index >= max_calls:
            stats.skipped_over_cap = len(the_plan.todo) - index
            break
        outcome = channel_digest_build.generate(day, router, model=model)
        stats.cost_usd += outcome.cost_usd
        if outcome.draft is None:
            stats.rejected[outcome.code or "unknown"] += 1
            continue
        channel_digest_build.save(connection, day, outcome, model=model)
        connection.commit()
        stats.made += 1
    return stats


def build_router(*, cost_state_path: str):
    from .gateway.router import Router

    raw = os.getenv(DAILY_LIMIT_ENV, "").strip()
    try:
        limit = float(raw) if raw else DEFAULT_DAILY_LIMIT_USD
    except ValueError:
        limit = DEFAULT_DAILY_LIMIT_USD
    return Router.from_default_registry(
        default_model=DIGEST_MODEL,
        daily_limit_usd=limit,
        cost_state_path=cost_state_path,
        fallback_models=(),
    )


def today_kst() -> date:
    return datetime.now(channel_digest.KST).date()


__all__ = [
    "DEFAULT_MAX_CALLS",
    "DIGEST_MODEL",
    "NIGHTLY_DAYS",
    "RUNTIME_ROLE",
    "DigestDsnError",
    "Plan",
    "RunStats",
    "build_router",
    "nightly_window",
    "plan",
    "resolve_dsn",
    "run",
    "source_channels",
    "today_kst",
]
