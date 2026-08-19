#!/usr/bin/env python3
"""환경변수 점검 - 값은 절대 전부 출력하지 않는다(마스킹).

사용: python scripts/check_env.py
출력물은 그대로 공유해도 안전하다(앞 6자 + 뒤 4자만 노출).
"""
from __future__ import annotations

import os
import pathlib
import sys

def _load_env() -> str:
    """환경변수 출처를 결정한다. 서버는 /etc/tybot/tybot.env, 로컬은 ./.env."""
    candidates = [
        os.getenv("TYBOT_ENV_FILE"),
        "/etc/tybot/tybot.env",
        str(pathlib.Path(__file__).resolve().parent.parent / ".env"),
    ]
    try:
        from dotenv import load_dotenv
    except ImportError:
        return "dotenv 미설치 - 프로세스 환경변수만 확인"
    for c in candidates:
        if c and pathlib.Path(c).is_file():
            load_dotenv(c)
            return c
    load_dotenv()
    return "파일 없음 - 프로세스 환경변수만 확인"


ENV_SOURCE = _load_env()

REQUIRED = [
    ("SLACK_BOT_TOKEN", "xoxb-"),
    ("SLACK_APP_TOKEN", "xapp-"),
    ("ANTHROPIC_API_KEY", "sk-ant-"),
]
OPTIONAL = ["PILOT_WORKSPACE", "ARCHIVE_DIR", "DEFAULT_MODEL", "DAILY_COST_LIMIT_USD", "BOT_NAME"]


def mask(v: str) -> str:
    return f"{v[:6]}…{v[-4:]} (len={len(v)})" if len(v) > 14 else f"<짧음 len={len(v)}>"


def main() -> int:
    ok = True
    print(f"환경변수 출처: {ENV_SOURCE}")
    print("=== 필수 ===")
    for key, prefix in REQUIRED:
        v = os.getenv(key, "")
        if not v or "REPLACE_ME" in v:
            print(f"  [MISS] {key}: 미설정")
            ok = False
        elif not v.startswith(prefix):
            print(f"  [WARN] {key}: 접두사가 '{prefix}' 가 아님 -> {mask(v)}")
            ok = False
        else:
            print(f"  [OK]   {key}: {mask(v)}")
    print("=== 선택 ===")
    for key in OPTIONAL:
        print(f"  [ .. ] {key}: {os.getenv(key) or '(기본값)'}")
    print("=== 패키지 ===")
    for mod in ("slack_bolt", "anthropic", "dotenv"):
        try:
            __import__(mod)
            print(f"  [OK]   {mod}")
        except ImportError:
            print(f"  [MISS] {mod} 미설치")
            ok = False
    print("\n결과:", "준비 완료 - python -m tybot.slack.pilot" if ok else "미완료(위 [MISS] 항목)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
