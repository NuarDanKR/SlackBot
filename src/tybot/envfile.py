"""환경변수 파일 로딩 — 봇과 점검 스크립트가 **같은 방식**으로 읽는다.

systemd 의 `EnvironmentFile=` 파싱은 python-dotenv 보다 엄격하다.
`export VAR=...`, `VAR = ...`(= 주변 공백), 인라인 주석을 systemd 는 무시하거나
값에 포함시킨다. 그래서 점검 스크립트는 통과하는데 서비스는 기동 실패하는
어긋남이 생겼다. 두 경로가 같은 로더를 쓰게 해서 그 계열의 문제를 없앤다.

우선순위: 이미 프로세스에 설정된 값(systemd 가 넣어준 것) > 파일.
`override=False` 이므로 systemd 로 들어온 값을 덮지 않는다.
"""
from __future__ import annotations

import os
import pathlib

CANDIDATES = ("/etc/tybot/tybot.env",)


def load_env_file() -> str:
    """설정 파일을 찾아 읽고, 사용한 경로를 반환한다(진단용)."""
    paths = [
        os.getenv("TYBOT_ENV_FILE"),
        *CANDIDATES,
        str(pathlib.Path(__file__).resolve().parents[2] / ".env"),
    ]
    try:
        from dotenv import load_dotenv
    except ImportError:
        return "dotenv 미설치 - 프로세스 환경변수만 사용"

    for p in paths:
        if p and pathlib.Path(p).is_file():
            load_dotenv(p, override=False)
            return p
    load_dotenv(override=False)
    return "파일 없음 - 프로세스 환경변수만 사용"
