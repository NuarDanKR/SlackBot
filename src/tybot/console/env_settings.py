"""관리 콘솔용 환경설정 조회·검증·저장.

기존 `/etc/tybot/tybot.env`는 사람이 관리하는 원본으로 유지한다. 콘솔은 허용된 비시크릿
항목만 별도 오버레이에 쓰며, Slack 토큰·LLM 키·계정 값은 읽거나 반환하지 않는다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values

from ..managed_env import managed_env_path, request_restart, restart_request_path
from . import reader

KST = timezone(timedelta(hours=9))
MANAGED_STATIC_KEYS = {
    "REALTIME_INGEST",
    "AUTOJOIN_CHANNELS",
    "REPLY_IN_THREAD",
}


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _effective_env() -> dict[str, str]:
    values = dict(os.environ)
    path = managed_env_path()
    if path.is_file():
        for key, value in dotenv_values(path).items():
            if value is not None:
                values[key] = value
    return values


def snapshot() -> dict:
    values = _effective_env()
    return {
        "path": str(managed_env_path()),
        "editable": True,
        "reason": None,
        "restartPending": restart_request_path().is_file(),
        "realtimeIngest": _truthy(values.get("REALTIME_INGEST"), True),
        "autojoinChannels": _truthy(values.get("AUTOJOIN_CHANNELS"), True),
        "replyInThread": _truthy(values.get("REPLY_IN_THREAD"), True),
    }


def _validate(payload: dict) -> dict[str, str]:
    return {
        "REALTIME_INGEST": "1" if payload.get("realtimeIngest") else "0",
        "AUTOJOIN_CHANNELS": "1" if payload.get("autojoinChannels") else "0",
        "REPLY_IN_THREAD": "1" if payload.get("replyInThread") else "0",
    }


def _serialize(values: dict[str, str]) -> str:
    lines = [
        "# TYBot 관리 콘솔이 생성한 설정 오버레이",
        "# 직접 편집하지 말고 관리 콘솔의 '환경변수 설정'에서 변경합니다.",
    ]
    for key, value in values.items():
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def save(payload: dict, *, actor: str) -> tuple[dict, list[str]]:
    before = _effective_env()
    values = _validate(payload)
    changed = sorted(
        key for key in MANAGED_STATIC_KEYS if before.get(key, "") != values.get(key, "")
    )

    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_serialize(values), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    # 콘솔의 다른 읽기 화면도 저장 직후 새 라벨·권한 관계를 보게 한다. 봇 프로세스에는
    # 영향이 없으며, 봇은 아래 재시작 요청 후 같은 파일을 새로 읽는다.
    os.environ.update(values)

    if changed:
        request_restart(actor, changed)
    return snapshot(), changed


def audit_change(actor: str, email: str, changed: list[str], action: str) -> None:
    """값은 남기지 않고 키 이름만 append-only 감사 로그에 기록한다."""
    path = reader.qa_log_dir() / "env-settings.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "actor": actor,
        "email": email,
        "action": action,
        "changed": sorted(changed),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
