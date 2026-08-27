"""환경설정 로더. 시크릿은 env(서버 시크릿 매니저 주입)에서만 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def state_dir() -> Path:
    """봇이 쓰기 상태 파일을 두는 곳(락·상태·리포트의 기준).

    **절대경로로 돌려준다.** 상대경로로 두면 `WorkingDirectory` 기준이 되는데,
    운영 유닛은 `ProtectSystem=strict` 로 코드 경로(`/opt/tybot`)가 읽기 전용이라
    "Read-only file system" 으로 기동이 막힌다.
    """
    for key in ("STATE_DIR", "LOCK_DIR"):
        v = os.getenv(key)
        if v:
            return Path(v).expanduser().resolve()
    for key in ("ARCHIVE_DIR", "QA_LOG_DIR"):
        v = os.getenv(key)
        if v:
            return Path(v).expanduser().resolve().parent
    return Path.cwd()


def cost_state_path(qa_log_dir: str | None = None) -> str:
    """당일 누적 LLM 비용을 남길 파일 경로.

    감사기록 디렉터리 아래에 둔다 — 아카이브(`archive/channels/`) 밖이라 답변 근거로
    오염될 일이 없고, 봇이 기동 시 쓰기 가능 여부를 이미 점검하는 경로다.
    """
    base = qa_log_dir or os.getenv("QA_LOG_DIR", "./qa-log")
    return os.getenv("COST_STATE_PATH") or str(Path(base) / "cost-state.json")


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str | None
    slack_app_token: str | None
    anthropic_api_key: str | None
    openai_api_key: str | None
    default_model: str
    daily_cost_limit_usd: float
    archive_repo: str | None
    archive_pull_interval_min: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN"),
            slack_app_token=os.getenv("SLACK_APP_TOKEN"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
            daily_cost_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            archive_repo=os.getenv("ARCHIVE_REPO"),
            archive_pull_interval_min=int(os.getenv("ARCHIVE_PULL_INTERVAL_MIN", "15")),
        )
