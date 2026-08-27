"""관리 콘솔이 쓰는 제한된 환경설정 오버레이와 재시작 요청."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .heartbeat import state_dir


def managed_env_path() -> Path:
    explicit = os.getenv("ENV_SETTINGS_PATH")
    if explicit:
        return Path(explicit)
    return state_dir() / "config" / "console-managed.env"


def restart_request_path() -> Path:
    return state_dir() / "restart-request.json"


def request_restart(actor: str, changed: list[str]) -> None:
    path = restart_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "actor": actor,
        "changed": sorted(changed),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def consume_restart_request() -> dict | None:
    """재시작 요청을 한 번만 소비한다. 깨진 요청도 반복 재시작을 막기 위해 지운다."""
    path = restart_request_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"error": "invalid restart request"}
    try:
        path.unlink()
    except OSError:
        return None
    return payload if isinstance(payload, dict) else {"error": "invalid restart request"}

