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
    "DEFAULT_MODEL", "DAILY_COST_LIMIT_USD", "STATE_DIR",
    "BOT_NAME", "REALTIME_INGEST", "EXEC_USERS", "CROSS_WS_READ",
]


def mask(v: str) -> str:
    return f"{v[:6]}…{v[-4:]} (len={len(v)})" if len(v) > 14 else f"<짧음 len={len(v)}>"


def check_write_paths() -> bool:
    """원문·감사기록을 실제로 쓸 수 있는지 확인한다.

    봇과 같은 코드(`tybot.paths.check_paths`)를 쓴다. 이 점검이 없어서, 경로 설정이 빠진
    서버가 "설정 정상"으로 보이는데 봇은 원문을 한 줄도 저장하지 못하는 상태로 떴다.
    """
    from tybot.paths import LABELS, archive_dir, check_paths, qa_log_dir

    a, q = archive_dir(), qa_log_dir()
    for label, path in (("아카이브", a), ("감사기록", q)):
        if not pathlib.Path(path).is_absolute():
            print(f"  [WARN] {label} 경로가 상대경로입니다: {path}")
            print(f"         서비스는 WorkingDirectory 기준으로 해석하며 그 경로는 "
                  f"읽기 전용입니다. {LABELS[label]} 를 /var/lib/tybot 아래 절대경로로 "
                  f"지정하세요.")
    problems = check_paths(a, q)
    for label, why in problems.items():
        print(f"  [MISS] {label} 쓰기 불가: {why}")
        print(f"         조치: tybot.env 에 {LABELS[label]}=/var/lib/tybot/... 지정 후 "
              f"sudo chown -R tybot:tybot /var/lib/tybot")
    if not problems:
        print(f"  [OK]   쓰기 가능 - archive={a} qa_log={q}")
    return not problems


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
    print("=== 쓰기 경로 ===")
    ok = check_write_paths() and ok
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
