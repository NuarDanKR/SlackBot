#!/usr/bin/env python3
"""config.json 의 `timezone` 을 읽어 그 시간대로 「지금·오늘」을 돌려준다. 정본은 여기 하나.

JS 쪽(`src/config.js` 의 `config.timezone`, 13곳)은 이미 config.json 을 그대로 읽는다.
파이썬 넷(`board.py`·`fetch_slack_files.py`·`sync_index.py`·`decide_work.py`)은 각자
"Asia/Seoul" 을 따로 하드코딩해 두고 있었다 — 지금은 config.json 의 값도 Asia/Seoul 이라
**우연히** 안 갈리지만, 그 값이 바뀌는 날 조용히 갈린다(WHK 지시, 2026-09-02 전수조사).
이 파일이 정본이고 네 스크립트가 전부 여기로 온다.

윈도우 파이썬에는 IANA 시간대 자료가 없을 수 있어 `zoneinfo` 가 실패한다(board.py·
fetch_slack_files.py 가 이미 겪은 자리). 그때는 **config.json 의 timezone 이 "Asia/Seoul"
인 동안만 유효한** 고정 +09:00 로 간다 — 한국은 1988년 이후 서머타임이 없어 그 둘이 언제나
같은 값이다. 다른 시간대로 바뀌면 그 폴백을 쓸 수 없으므로 **에러로 세운다** — 조용히
틀린 시간대로 도는 것보다 낫다.
"""
import json
from datetime import datetime, timedelta, timezone

from paths import CONFIG


def _configured_name() -> str:
    """config.json 을 못 읽거나 timezone 키가 없으면 지금까지 하드코딩되어 있던 값
    (Asia/Seoul)으로 fail-safe — 설정 자체가 없는 것을 시간대 변경으로 오인하면 안 된다."""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        name = data.get("timezone")
        if name:
            return str(name)
    except OSError:
        pass
    return "Asia/Seoul"


#: config.json 이 읽힌 시각의 값. 프로세스 수명 동안 고정 — JS 쪽 `config` 도 모듈 로드
#: 시점에 한 번만 읽는 상수라(`config.js` 의 `export const config = loadConfig()`) 같은 방식.
TZ_NAME = _configured_name()


def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception as exc:
        if TZ_NAME == "Asia/Seoul":
            return timezone(timedelta(hours=9))
        raise RuntimeError(
            f"zoneinfo 로 시간대 {TZ_NAME!r} 을 못 읽었고, 고정 폴백은 Asia/Seoul 에만 "
            "유효합니다 — config.json 의 timezone 이 바뀌었는데 이 기계에는 그 IANA 자료가 "
            "없습니다."
        ) from exc


TZINFO = _tzinfo()


def now() -> datetime:
    """config.json 의 timezone 기준 현재 시각 (tz-aware)."""
    return datetime.now(TZINFO)


def today():
    """config.json 의 timezone 기준 오늘 날짜."""
    return now().date()
