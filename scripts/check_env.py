#!/usr/bin/env python3
"""환경변수 점검 - 값은 절대 전부 출력하지 않는다(마스킹).

사용: python scripts/check_env.py
출력물은 그대로 공유해도 안전하다(앞 6자 + 뒤 4자만 노출).
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

ENV_SOURCE = load_env_file()

OPTIONAL = [
    "ARCHIVE_DIR", "QA_LOG_DIR", "DEFAULT_MODEL", "DAILY_COST_LIMIT_USD",
    "BOT_NAME", "REALTIME_INGEST", "EXEC_USERS", "CROSS_WS_READ",
]


def mask(v: str) -> str:
    return f"{v[:6]}…{v[-4:]} (len={len(v)})" if len(v) > 14 else f"<짧음 len={len(v)}>"


def check_workspaces() -> bool:
    """워크스페이스 설정을 실제 로더로 검증한다.

    단일/멀티 설정을 모두 같은 코드로 확인해야, 점검은 통과하는데 기동은 실패하는
    상황이 생기지 않는다.
    """
    try:
        from tybot.workspaces import ConfigError, load_workspaces
    except ImportError as e:
        print(f"  [MISS] tybot 패키지를 불러올 수 없습니다: {e}")
        return False
    try:
        cfgs = load_workspaces()
    except ConfigError as e:
        print(f"  [MISS] 워크스페이스 설정 오류: {e}")
        if not os.getenv("WORKSPACES"):
            print("         힌트: tybot.env 의 'WORKSPACES=' 줄이 없거나 '#' 으로 주석 처리돼 "
                  "있는지 확인하세요(.env.example 에서는 주석 상태입니다).")
        return False

    mode = "멀티" if os.getenv("WORKSPACES") else "단일"
    print(f"  [OK]   워크스페이스 {len(cfgs)}개 ({mode} 설정)")
    ok = True
    for c in cfgs:
        for label, tok, prefix in (
            ("bot", c.bot_token, "xoxb-"), ("app", c.app_token, "xapp-")
        ):
            if "REPLACE_ME" in tok:
                print(f"  [MISS] {c.key} {label} 토큰: 자리표시자 그대로")
                ok = False
            elif not tok.startswith(prefix):
                print(f"  [WARN] {c.key} {label} 토큰: 접두사가 '{prefix}' 가 아님 -> {mask(tok)}")
                ok = False
        cross = ", ".join(sorted(c.readable)) or "없음(격리)"
        print(f"  [ .. ] {c.key}({c.label}) bot={mask(c.bot_token)} 크로스열람={cross}")
    return ok


def main() -> int:
    ok = True
    print(f"환경변수 출처: {ENV_SOURCE}")
    print("=== 워크스페이스 ===")
    print(f"  [ .. ] WORKSPACES: {os.getenv('WORKSPACES') or '(미설정 -> 단일 워크스페이스 모드)'}")
    ok = check_workspaces() and ok
    print("=== LLM ===")
    v = os.getenv("ANTHROPIC_API_KEY", "")
    if not v or "REPLACE_ME" in v:
        print("  [MISS] ANTHROPIC_API_KEY: 미설정")
        ok = False
    elif not v.startswith("sk-ant-"):
        print(f"  [WARN] ANTHROPIC_API_KEY: 접두사가 'sk-ant-' 가 아님 -> {mask(v)}")
        ok = False
    else:
        print(f"  [OK]   ANTHROPIC_API_KEY: {mask(v)}")
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
