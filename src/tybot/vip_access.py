"""VIPBot 호출 권한과 명시적 근거 채널을 하나의 닫힌 범위로 만든다.

Slack 사용자 권한과 근거 채널은 독립된 두 문이다. 둘 중 하나라도 확인되지 않으면
답변 컨텍스트를 만들지 않는다. 이 모듈은 Slack API나 LLM을 호출하지 않는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import bot_access, identity
from .access import RequestContext

logger = logging.getLogger("tybot.vip_access")

BOT = "vipbot"
ALLOWED = "allowed"
NO_SOURCES = "no_sources"


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    code: str
    workspace: str
    slack_user: str
    emp_no: str | None = None
    grant_id: int | None = None
    sources: frozenset[tuple[str, str]] = frozenset()

    def context(self) -> RequestContext:
        """허용된 판정을 검색 컨텍스트로 바꾼다.

        호출자의 Slack 채널 멤버십과 현재 질문 채널은 일부러 싣지 않는다. VIPBot의
        자료 범위는 콘솔에 등록된 근거 채널뿐이다.
        """
        if not self.allowed or not self.sources:
            raise ValueError(self.code)
        return RequestContext(
            workspace=self.workspace,
            user_id=self.slack_user,
            evidence_sources=self.sources,
        )


def authorize(conn, *, workspace: str, slack_user: str) -> Authorization:
    """VIP 사용자와 전역 VIP 근거 채널을 함께 판정한다.

    ``bot_access.source_channels``는 DB 오류도 빈 집합으로 닫는다. 그러므로 근거를
    읽지 못한 경우 역시 ``no_sources``로 답변을 막고, 구체적인 DB 오류는 서버 로그에만
    남는다.
    """
    decision = bot_access.may_use_bot(
        conn, bot=BOT, workspace=workspace, slack_user=slack_user
    )
    if not decision.allowed:
        return Authorization(
            False,
            decision.code,
            workspace,
            slack_user,
            emp_no=decision.emp_no,
            grant_id=decision.grant_id,
        )

    sources = bot_access.source_channels(conn, bot=BOT)
    if not sources:
        return Authorization(
            False,
            NO_SOURCES,
            workspace,
            slack_user,
            emp_no=decision.emp_no,
            grant_id=decision.grant_id,
        )
    return Authorization(
        True,
        ALLOWED,
        workspace,
        slack_user,
        emp_no=decision.emp_no,
        grant_id=decision.grant_id,
        sources=sources,
    )


def authorize_slack(
    *, connect, client, workspace: str, slack_user: str
) -> Authorization:
    """Slack 요청 한 건의 사번 매핑과 VIP 판정을 같은 DB 연결에서 수행한다.

    이메일은 ``identity.ensure`` 안에서 사번을 찾는 데만 쓰고 반환하거나 로그에
    남기지 않는다. 연결·Slack·DB 오류는 호출자에게 예외 본문을 넘기지 않고 닫힌
    판정으로 바꾼다.
    """
    try:
        with connect() as conn:
            identity.ensure(
                conn, client, workspace=workspace, slack_user=slack_user
            )
            return authorize(conn, workspace=workspace, slack_user=slack_user)
    except Exception as exc:  # noqa: BLE001 - 권한 인프라 실패는 거절이다
        logger.warning(
            "[%s] VIPBot 요청 권한 확인 실패 user=%s code=%s",
            workspace,
            slack_user,
            exc.__class__.__name__,
        )
        return Authorization(
            False, bot_access.CHECK_FAILED, workspace, slack_user
        )


__all__ = [
    "ALLOWED",
    "BOT",
    "NO_SOURCES",
    "Authorization",
    "authorize",
    "authorize_slack",
]
