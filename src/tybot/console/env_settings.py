"""관리 콘솔용 환경설정 조회·검증·저장.

기존 `/etc/tybot/tybot.env`는 사람이 관리하는 원본으로 유지한다. 콘솔은 허용된 비시크릿
항목만 별도 오버레이에 쓰며, Slack 토큰·LLM 키·계정 값은 읽거나 반환하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values

from ..managed_env import managed_env_path, request_restart, restart_request_path
from ..workspaces import ConfigError, env_suffix, parse_cross_read
from . import reader

KST = timezone(timedelta(hours=9))
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
MANAGED_STATIC_KEYS = {
    "WORKSPACES",
    "ROOT_WORKSPACES",
    "CROSS_WS_READ",
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


def _workspace_keys(values: dict[str, str]) -> list[str]:
    return [key.strip() for key in values.get("WORKSPACES", "").split(",") if key.strip()]


def snapshot() -> dict:
    values = _effective_env()
    keys = _workspace_keys(values)
    known = set(keys)
    roots = {k.strip() for k in values.get("ROOT_WORKSPACES", "").split(",") if k.strip()}
    try:
        cross = parse_cross_read(values.get("CROSS_WS_READ"), known)
    except ConfigError:
        cross = {}

    workspaces = []
    for key in keys:
        suffix = env_suffix(key)
        workspaces.append(
            {
                "key": key,
                "label": values.get(f"WORKSPACE_LABEL_{suffix}", key),
                "root": key in roots,
                "readable": sorted(cross.get(key, frozenset())),
            }
        )
    return {
        "path": str(managed_env_path()),
        "editable": bool(keys),
        "reason": None if keys else "WORKSPACES가 설정된 멀티 워크스페이스 모드에서만 편집할 수 있습니다.",
        "restartPending": restart_request_path().is_file(),
        "workspaces": workspaces,
        "realtimeIngest": _truthy(values.get("REALTIME_INGEST"), True),
        "autojoinChannels": _truthy(values.get("AUTOJOIN_CHANNELS"), True),
        "replyInThread": _truthy(values.get("REPLY_IN_THREAD"), True),
    }


def _validate(payload: dict) -> tuple[list[dict], dict[str, str]]:
    current = snapshot()
    current_keys = [row["key"] for row in current["workspaces"]]
    rows = payload.get("workspaces") or []
    incoming_keys = [str(row.get("key", "")).strip() for row in rows]
    if not current_keys:
        raise ValueError("멀티 워크스페이스 설정이 없어 저장할 수 없습니다.")
    if incoming_keys != current_keys:
        raise ValueError("워크스페이스 키를 추가·삭제하거나 순서를 바꿀 수 없습니다.")

    known = set(current_keys)
    normalized: list[dict] = []
    for row in rows:
        key = str(row.get("key", "")).strip()
        if not KEY_RE.fullmatch(key):
            raise ValueError(f"워크스페이스 키 형식이 잘못됐습니다: {key!r}")
        label = str(row.get("label", "")).strip()
        if not label or len(label) > 80 or "\n" in label or "\r" in label:
            raise ValueError(f"{key} 표시 이름은 1~80자 한 줄이어야 합니다.")
        readable = [str(v).strip() for v in (row.get("readable") or [])]
        if len(readable) != len(set(readable)):
            raise ValueError(f"{key} 열람 대상에 중복이 있습니다.")
        unknown = set(readable) - known
        if unknown:
            raise ValueError(f"{key}의 알 수 없는 열람 대상: {sorted(unknown)}")
        if key in readable:
            raise ValueError(f"{key}는 자기 자신을 크로스 열람 대상으로 지정할 수 없습니다.")
        normalized.append(
            {"key": key, "label": label, "root": bool(row.get("root")), "readable": readable}
        )

    roots = [row["key"] for row in normalized if row["root"]]
    cross = ",".join(
        f"{row['key']}:{'|'.join(row['readable'])}" for row in normalized if row["readable"]
    )
    try:
        parse_cross_read(cross, known)
    except ConfigError as e:
        raise ValueError(str(e)) from e
    values = {
        "WORKSPACES": ",".join(current_keys),
        "ROOT_WORKSPACES": ",".join(roots),
        "CROSS_WS_READ": cross,
        "REALTIME_INGEST": "1" if payload.get("realtimeIngest") else "0",
        "AUTOJOIN_CHANNELS": "1" if payload.get("autojoinChannels") else "0",
        "REPLY_IN_THREAD": "1" if payload.get("replyInThread") else "0",
    }
    for row in normalized:
        values[f"WORKSPACE_LABEL_{env_suffix(row['key'])}"] = row["label"]
    return normalized, values


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
    _, values = _validate(payload)
    managed_keys = MANAGED_STATIC_KEYS | {
        key for key in values if key.startswith("WORKSPACE_LABEL_")
    }
    changed = sorted(key for key in managed_keys if before.get(key, "") != values.get(key, ""))

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
