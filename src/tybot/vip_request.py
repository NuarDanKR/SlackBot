"""VIPBot 요청의 권한 확인과 답변 생성을 순서대로 묶는다.

이 모듈은 Slack 전달이나 LLM 구현을 소유하지 않는다. 답변 생성 콜백에
``RequestContext``를 넘길 뿐이며, VIP 사용자 권한과 근거 채널 범위가 확인되기
전에는 그 콜백을 절대 부르지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .access import RequestContext
from .vip_access import Authorization, authorize_slack

T = TypeVar("T")


@dataclass(frozen=True)
class RunResult(Generic[T]):
    authorization: Authorization
    value: T | None = None

    @property
    def generated(self) -> bool:
        if not self.authorization.allowed or self.value is None:
            return False
        text = getattr(self.value, "text", self.value)
        if isinstance(text, str):
            return bool(text.strip())
        return True


def run_authorized(
    *,
    connect,
    client,
    workspace: str,
    slack_user: str,
    generate: Callable[[RequestContext], T],
) -> RunResult[T]:
    """권한을 먼저 확인하고 허용된 근거 컨텍스트로만 답변을 만든다.

    거절·DB 오류·근거 채널 없음은 모두 ``value=None``으로 닫힌다. 호출자는 이
    결과의 고정 사유 코드만 전달 계층에 넘기고, 거절된 요청에 답변 생성 콜백을
    다시 호출하면 안 된다.
    """
    authorization = authorize_slack(
        connect=connect,
        client=client,
        workspace=workspace,
        slack_user=slack_user,
    )
    if not authorization.allowed:
        return RunResult(authorization)
    return RunResult(authorization, generate(authorization.context()))


__all__ = ["RunResult", "run_authorized"]
