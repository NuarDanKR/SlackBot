"""관리 콘솔용 TYBot 서비스 로그 조회.

전체 journal 권한을 콘솔에 주지 않는다. root 소유 헬퍼가 tybot.service와 허용된
레벨·개수만 처리하고, 여기서 시크릿 패턴을 다시 마스킹한다.
"""
from __future__ import annotations

import os
import re
import subprocess

LEVELS = ("info", "warning", "error")
MAX_LIMIT = 500
RECORD_SEPARATOR = "\x1e"
_SECRET_PATTERNS = (
    re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]+"),
    re.compile(r"\b(sk-ant-[A-Za-z0-9_-]+)"),
    re.compile(r"\b(postgresql(?:\+\w+)?://[^\s]+)", re.IGNORECASE),
    re.compile(r"\b(password=)[^\s]+", re.IGNORECASE),
)


class ServiceLogError(RuntimeError):
    """서비스 로그를 제한된 경로로 읽지 못했다."""


def _redact(line: str) -> str:
    out = line
    out = _SECRET_PATTERNS[0].sub(r"\1***", out)
    for pattern in _SECRET_PATTERNS[1:]:
        out = pattern.sub("***", out)
    return out


def read(*, level: str, limit: int) -> list[dict[str, str]]:
    level = level.strip().lower()
    if level not in LEVELS:
        raise ServiceLogError("로그 레벨은 info, warning, error 중 하나여야 합니다.")
    if not 1 <= limit <= MAX_LIMIT:
        raise ServiceLogError(f"로그 개수는 1~{MAX_LIMIT} 사이여야 합니다.")
    helper = os.getenv("CONSOLE_LOG_HELPER", "/usr/local/libexec/tybot-console-logs")
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", helper, level, str(limit)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ServiceLogError(f"서비스 로그 헬퍼 실행 실패: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or "권한 또는 journal 설정을 확인하세요.").strip()
        raise ServiceLogError(f"서비스 로그 조회 실패: {detail}")
    # 새 헬퍼는 traceback을 포함한 로그 블록 사이에 ASCII RS를 넣는다. 배포 중
    # 구 헬퍼와 잠시 조합되더라도 한 줄 로그 조회는 계속 동작하도록 fallback을 둔다.
    if RECORD_SEPARATOR in result.stdout:
        messages = result.stdout.split(RECORD_SEPARATOR)
    else:
        messages = result.stdout.splitlines()
    return [
        {"level": level, "message": _redact(message.strip())}
        for message in messages
        if message.strip()
    ]
