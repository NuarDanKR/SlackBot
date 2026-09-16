"""Provider SDK 의 timeout 예외를 알아본다.

SDK 마다 이름이 다르다(`APITimeoutError`·`Timeout`·`ReadTimeout`…). 클래스를
import 해서 비교하면 그 SDK 가 없는 설치에서 import 가 깨지고, 버전이 바뀌면
조용히 안 맞는다. **이름으로 본다** — 느슨하지만 여기서는 그게 맞다.

못 알아보면 `False` 다. timeout 이 아닌 오류를 timeout 으로 적으면 "다시 시도"
안내가 나가고, 실제 원인(인증·모델명)은 영영 안 보인다.
"""
from __future__ import annotations

_NAMES = ("timeout", "timedout")


def is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    if any(mark in name for mark in _NAMES):
        return True
    # `httpx.ConnectTimeout` 처럼 원인 사슬에만 들어 있는 경우가 있다.
    cause = exc.__cause__ or exc.__context__
    return bool(cause) and cause is not exc and is_timeout(cause)
