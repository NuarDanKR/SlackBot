"""관리 콘솔의 TYBot systemd 타이머 조회·제어."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from . import reader

KST = timezone(timedelta(hours=9))
HELPER_DEFAULT = "/usr/local/libexec/tybot-console-timers"
ALLOWED_ACTIONS = frozenset({"enable", "disable", "run", "schedule"})

TIMERS = {
    "tybot-collect.timer": {
        "label": "정기 백필",
        "description": "실시간 수집에서 빠진 업무시간 대화를 보충합니다.",
        "default": "business-hourly",
        "presets": {
            "business-hourly": "업무시간 매시",
            "business-30m": "업무시간 30분마다",
            "all-hours-hourly": "하루 종일 매시",
        },
    },
    "tybot-schedule-dm.timer": {
        "label": "일정 DM 알림",
        "description": "예정된 일정을 10분·30분 전에 개인 DM으로 알립니다.",
        "default": "fixed-1m",
        "presets": {},
    },
    "tybot-schedule-sync.timer": {
        "label": "일정 동기화",
        "description": "Oracle에서 만든 일정 스냅샷을 PostgreSQL에 반영합니다.",
        "default": "fixed-1m",
        "presets": {},
    },
    "tybot-tidy.timer": {
        "label": "아카이브 점검",
        "description": "수집 중단·문서 형식·PII 상태를 주기적으로 점검합니다.",
        "default": "15m",
        "presets": {"5m": "5분마다", "15m": "15분마다", "30m": "30분마다", "60m": "매시간"},
    },
    "tybot-update.timer": {
        "label": "자동 업데이트",
        "description": "업무시간에 새 커밋을 확인하고 테스트를 통과한 버전을 배포합니다.",
        "default": "business-10m",
        "presets": {
            "business-10m": "평일 업무시간 10분마다",
            "business-30m": "평일 업무시간 30분마다",
            "business-hourly": "평일 업무시간 매시",
        },
    },
}


class TimerManagerError(RuntimeError):
    """허용되지 않았거나 systemd에서 처리하지 못한 타이머 작업."""


def _run(args: list[str]) -> str:
    helper = os.getenv("CONSOLE_TIMER_HELPER", HELPER_DEFAULT)
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", helper, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TimerManagerError(f"배치 관리 헬퍼 실행 실패: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or "systemd 권한과 타이머 설치 상태를 확인하세요.").strip()
        raise TimerManagerError(f"배치 작업 실패: {detail}")
    return result.stdout


def snapshot() -> list[dict]:
    rows: dict[str, list[str]] = {}
    for line in _run(["list"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 8 and parts[0] in TIMERS:
            rows[parts[0]] = parts
    if len(rows) != len(TIMERS):
        missing = sorted(set(TIMERS) - set(rows))
        raise TimerManagerError(f"배치 상태 응답이 불완전합니다: {', '.join(missing)}")

    result = []
    for unit, meta in TIMERS.items():
        parts = rows[unit]
        preset = parts[7] if parts[7] != "default" else str(meta["default"])
        presets = meta["presets"]
        schedule_label = "매분(고정)" if preset == "fixed-1m" else presets.get(preset, preset)
        result.append(
            {
                "unit": unit,
                "label": meta["label"],
                "description": meta["description"],
                "enabled": parts[1] == "enabled",
                "active": parts[2] == "active",
                "state": parts[3],
                "nextRun": None if parts[4] in {"", "-", "n/a"} else parts[4],
                "lastRun": None if parts[5] in {"", "-", "n/a"} else parts[5],
                "lastResult": parts[6] or "unknown",
                "preset": preset,
                "scheduleLabel": schedule_label,
                "scheduleEditable": bool(presets),
                "presets": [{"value": key, "label": label} for key, label in presets.items()],
            }
        )
    return result


def apply(unit: str, action: str, preset: str | None = None) -> list[dict]:
    if unit not in TIMERS:
        raise TimerManagerError("관리할 수 없는 타이머입니다.")
    if action not in ALLOWED_ACTIONS:
        raise TimerManagerError("허용되지 않은 배치 작업입니다.")
    args = [action, unit]
    if action == "schedule":
        presets = TIMERS[unit]["presets"]
        if not preset or preset not in presets:
            raise TimerManagerError("이 타이머에 허용되지 않은 실행 주기입니다.")
        args.append(preset)
    _run(args)
    return snapshot()


def audit(actor: str, email: str, unit: str, action: str, preset: str | None, result: str) -> None:
    path = reader.qa_log_dir() / "timer-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "actor": actor,
        "email": email,
        "unit": unit,
        "action": action,
        "preset": preset,
        "result": result,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
