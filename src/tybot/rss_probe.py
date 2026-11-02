"""메모리 관측 — **읽기 전용, 기본 꺼짐.**

결정: 2026-11-02 `tybot.service` OOM 조사.

## 왜 있나

OOM 은 커널 로그에 한 줄 남고 끝난다 — 어느 요청이 얼마를 썼는지는 남지 않는다.
그래서 「검색 42 건짜리 질문」 과 「3GiB」 를 이어 붙이는 데 사람이 추측을 했다.
그 추측을 없애려고 **요청 경계에서 숫자만** 남긴다.

## 남기지 않는 것

질문·검색어·원문·채널명·사람 이름·경로·시크릿은 **한 글자도 넣지 않는다.**
메모리 로그는 장애 때 통째로 퍼 날라지는 자리라, 여기에 들어간 것은 아카이브
권한 밖으로 나간다고 봐야 한다. 남기는 것은 도구 이름(`search` 등 고정 낱말)과
바이트 수뿐이다.

## 기본으로 꺼 둔다

`TYBOT_RSS_LOG=1` 일 때만 읽는다. 켜도 하는 일은 `/proc/self/status` 를 여는
것뿐이고, 리눅스가 아니면 조용히 아무것도 하지 않는다 — 개발 PC(Windows)에서
없는 파일을 찾다 경고를 찍으면 그게 더 혼란스럽다.

    sudo systemctl set-environment TYBOT_RSS_LOG=1   # 조사할 때만
    sudo systemctl restart tybot                     # 새 프로세스부터 반영
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("tybot.rss")

#: `/proc/self/status` 에서 볼 칸. `RssAnon` 이 OOM killer 가 세는 값에 가장 가깝다.
FIELDS = ("RssAnon", "VmRSS", "VmSwap")

STATUS = "/proc/self/status"

# 모델이 도구 이름 자리에 임의 문자열을 내더라도 로그에는 고정 어휘만 남긴다.
LABELS = frozenset({"search", "read_channel", "read_document", "fetch_recent_slack"})


def enabled() -> bool:
    return (os.getenv("TYBOT_RSS_LOG") or "").strip().lower() in {"1", "true", "yes", "on"}


def sample() -> dict[str, int]:
    """현재 프로세스의 메모리 — KiB. 읽을 수 없으면 빈 dict.

    리눅스가 아니거나 `/proc` 이 없으면 **빈 값**이다. 0 을 돌려주면 「메모리를
    안 쓴다」 로 보이고, 그건 못 읽은 것과 구별되지 않는다.
    """
    try:
        with open(STATUS, encoding="ascii") as f:
            raw = f.read()
    except OSError:
        return {}
    found: dict[str, int] = {}
    for line in raw.splitlines():
        name, _, rest = line.partition(":")
        if name in FIELDS:
            parts = rest.split()
            if parts and parts[0].isdigit():
                found[name] = int(parts[0])
    return found


@contextmanager
def observe(label: str):
    """구간 전후의 메모리를 남긴다. 꺼져 있으면 아무 일도 하지 않는다.

    `label` 은 **고정 낱말**이어야 한다(도구 이름 등). 호출부가 질문이나 채널
    이름을 넣으면 그 순간 이 모듈의 약속이 깨진다.
    """
    if not enabled():
        yield
        return
    label = label if label in LABELS else "unknown"
    before = sample()
    try:
        yield
    finally:
        after = sample()
        if before and after:
            log.info(
                "rss label=%s anon_kib=%d anon_delta_kib=%+d vmrss_kib=%d swap_kib=%d",
                label,
                after.get("RssAnon", 0),
                after.get("RssAnon", 0) - before.get("RssAnon", 0),
                after.get("VmRSS", 0),
                after.get("VmSwap", 0),
            )
