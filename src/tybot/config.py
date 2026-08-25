"""환경설정 로더. 시크릿은 env(서버 시크릿 매니저 주입)에서만 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
