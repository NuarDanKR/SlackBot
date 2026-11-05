"""VIPBot 전달 정책 — **상세 답변은 요청자 DM 으로만** 간다 (B-83).

채널에서 부를 수 있게 두되, 답은 채널에 남기지 않는다. 남기면 그 방에 있는
모두가 **요청자 권한으로 만든 답**을 보게 된다(원칙 3: 막는 쪽이 기본값).
채널에 남는 것은 내용이 없는 고정 안내 한 줄뿐이다.

## 이 모듈이 지키는 네 가지

1. **채널에는 고정 문구만.** 채널로 나가는 글은 :data:`CHANNEL_NOTICES` 안에
   있어야 한다. 밖의 글을 채널로 보내려 하면 호출이 터진다 — 「실수로 섞여
   들어간다」 가 가능한 자리를 아예 없앤다
2. **DM 실패는 채널 폴백이 아니다.** DM 이 안 가면 거기서 끝이다. 못 보낸 답을
   채널에 떨어뜨리는 폴백은 이 모듈이 막으려는 사고 그 자체다
3. **거절은 채널에 안 적는다.** 누가 물었고 거절당했는지가 방에 남는다. 거절
   사유도 사람에게는 안 밝힌다(`bot_access.DENY_MESSAGE`) — 코드로만 남는다
4. **본문은 로그에 없다.** 질문·답변·근거는 결과 객체에도 로그에도 안 들어간다.
   남는 것은 고정 낱말과 참·거짓뿐이다

## 이 모듈이 하지 않는 것

수집·백필·첨부 보관·Canvas 쓰기. 부르지 않을 뿐 아니라 **import 하지 않는다**
(`tests/test_vipbot_boundary.py` 가 import 목록째로 고정한다). Slack 호출도
`chat_postMessage` · `conversations_open` 둘뿐이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from tybot.bot_access import (
    CHECK_FAILED,
    DENY_MESSAGE,
    EXPIRED,
    INACTIVE_EMPLOYEE,
    NO_GRANT,
    NO_IDENTITY,
    UNKNOWN_BOT,
    UNKNOWN_EMPLOYEE,
)

log = logging.getLogger("tybot.vipbot_delivery")

VIPBOT = "vipbot"

#: 「이 사람은 쓸 수 있지만 **등록된 근거 채널이 하나도 없다**」. `bot_access` 의
#: 코드가 아니라 `vip_access.NO_SOURCES` 라서 여기 따로 적는다 — 낱말을 베끼는
#: 대신 두 쪽이 같은지는 시험이 붙들고 있다(`tybot.vip_access` 를 들여오면 전달
#: 계층이 권한 계층에 묶이고, 경계 계약의 import 화이트리스트가 깨진다).
#:
#: `no_grant` 로 뭉뚱그리면 조치가 갈린다 — 한쪽은 **사람에게 권한을 주는 일**이고
#: 다른 한쪽은 **근거 채널을 등록하는 일**이다. 뭉뚱그린 코드를 보고 전산팀은
#: 권한을 다시 주고, 봇은 여전히 답하지 않는다.
NO_SOURCES = "no_sources"

#: 채널에 남을 수 있는 **전부.** 내용이 없고, 답이 있었는지 여부만 알린다.
CHANNEL_NOTICE = "요청하신 내용은 DM 으로 보냈습니다."

#: 채널로 나갈 수 있는 글의 전체 목록. 여기 없는 글은 채널로 못 나간다.
CHANNEL_NOTICES = frozenset({CHANNEL_NOTICE})

#: 전달이 끝난 방식. 고정 낱말이라 그대로 세고 로그에 남길 수 있다.
DELIVERY_OUTCOMES = frozenset(
    {
        "delivered",
        "permission_denied",
        "access_check_failed",
        "dm_delivery_failed",
        "channel_notice_failed",
    }
)

#: `reason` 이 가질 수 있는 전부. 프로바이더·Slack 메시지가 이 칸으로 새는 길을
#: 막는다 — 집합 밖 값이면 결과 객체를 못 만든다.
DELIVERY_REASONS = frozenset(
    {
        "",
        NO_IDENTITY,
        UNKNOWN_EMPLOYEE,
        INACTIVE_EMPLOYEE,
        NO_GRANT,
        EXPIRED,
        UNKNOWN_BOT,
        CHECK_FAILED,
        NO_SOURCES,
        "dm_channel_invalid",
        "dm_open_failed",
        "dm_post_failed",
        "notice_post_failed",
    }
)


class ChannelLeak(RuntimeError):
    """채널로 고정 안내가 아닌 글을 보내려 했다. 코드 결함이지 운영 상황이 아니다."""


@dataclass(frozen=True)
class DeliveryResult:
    """전달 결과. **질문도 답변도 근거도 들지 않는다.**"""

    outcome: str
    reason: str = ""
    dm_sent: bool = False
    notice_posted: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in DELIVERY_OUTCOMES:
            raise ValueError("unknown delivery outcome")
        if self.reason not in DELIVERY_REASONS:
            raise ValueError("unknown delivery reason")
        if self.notice_posted and not self.dm_sent:
            # 채널 안내는 「DM 으로 보냈다」 는 말이다. DM 이 안 갔는데 그 말이
            # 방에 남으면, 사람은 오지 않은 DM 을 기다린다.
            raise ValueError("a notice may not claim a DM that was not sent")


def deliver_channel_answer(
    client,
    *,
    workspace: str,
    channel: str,
    slack_user: str,
    answer: str | None,
    check_access: Callable[[], object],
    thread_ts: str | None = None,
) -> DeliveryResult:
    """채널에서 받은 호출의 **상세 답변을 요청자 DM 으로** 보낸다.

    `check_access` 는 인자 없는 호출 한 번이다. DB 연결을 여기로 들이지 않는
    이유는 이 모듈이 **전달 정책**이기 때문이다 — 판정은 `bot_access` 가 소유하고,
    여기서는 그 결정을 받아 쓴다.

    `answer` 는 오직 DM 으로만 간다. 채널로 가는 인자가 아니다.
    """
    if not str(channel or "").strip() or not str(slack_user or "").strip():
        raise ValueError("channel and slack_user are required")
    try:
        decision = check_access()
        allowed = bool(getattr(decision, "allowed", False))
        code = str(getattr(decision, "code", "") or "")
    except Exception as exc:  # noqa: BLE001 - 판정을 못 했으면 거절이다
        # 예외 본문은 남기지 않는다. 판정 SQL 에는 사번이 들어 있다.
        log.warning(
            "[%s] VIP 권한 판정 실패 user=%s err=%s", workspace, slack_user, type(exc).__name__
        )
        return _done("access_check_failed", CHECK_FAILED, workspace, slack_user)

    if code == CHECK_FAILED:
        return _done("access_check_failed", CHECK_FAILED, workspace, slack_user)
    if not allowed:
        # 거절은 **채널에 안 적는다.** 누가 거절당했는지가 방에 남으면 그 자체가
        # 사람에 대한 정보다. 사유도 사람에게는 안 밝힌다.
        failure = _dm(client, slack_user, DENY_MESSAGE)
        reason = code if code in DELIVERY_REASONS and code else NO_GRANT
        return _done("permission_denied", reason, workspace, slack_user, dm_sent=not failure)

    if not str(answer or "").strip():
        raise ValueError("answer is required for an allowed request")
    failure = _dm(client, slack_user, answer)
    if failure:
        # **폴백 없음.** 못 보낸 답을 채널에 떨어뜨리지 않는다.
        return _done("dm_delivery_failed", failure, workspace, slack_user)

    if not _notice(client, channel, thread_ts=thread_ts):
        return _done(
            "channel_notice_failed",
            "notice_post_failed",
            workspace,
            slack_user,
            dm_sent=True,
        )
    return _done("delivered", "", workspace, slack_user, dm_sent=True, notice_posted=True)


def deliver_dm_answer(
    client,
    *,
    workspace: str,
    channel: str,
    slack_user: str,
    answer: str | None,
    check_access: Callable[[], object],
) -> DeliveryResult:
    """Reply to a DM event without opening another conversation or posting a notice."""
    if not str(channel or "").startswith("D") or not str(slack_user or "").strip():
        raise ValueError("a DM channel and slack_user are required")

    try:
        decision = check_access()
        allowed = bool(getattr(decision, "allowed", False))
        code = str(getattr(decision, "code", "") or "")
    except Exception as exc:  # noqa: BLE001 - an unknown decision denies the request
        log.warning(
            "[%s] VIP 권한 판정 실패 user=%s err=%s",
            workspace,
            slack_user,
            type(exc).__name__,
        )
        return _done("access_check_failed", CHECK_FAILED, workspace, slack_user)

    if code == CHECK_FAILED:
        return _done("access_check_failed", CHECK_FAILED, workspace, slack_user)
    if not allowed:
        reason = code if code in DELIVERY_REASONS and code else NO_GRANT
        failure = _post_dm_channel(client, channel, DENY_MESSAGE)
        return _done("permission_denied", reason, workspace, slack_user, dm_sent=not failure)

    if not str(answer or "").strip():
        raise ValueError("answer is required for an allowed request")
    failure = _post_dm_channel(client, channel, answer)
    if failure:
        return _done("dm_delivery_failed", failure, workspace, slack_user)
    return _done("delivered", "", workspace, slack_user, dm_sent=True)


def _dm(client, slack_user: str, text: str) -> str:
    """요청자와의 DM 을 열고 보낸다. 성공은 빈 문자열, 실패는 고정 사유 하나."""
    try:
        opened = client.conversations_open(users=slack_user) or {}
    except Exception as exc:  # noqa: BLE001 - Slack 장애로 호출 전체를 죽이지 않는다
        log.warning("VIP DM 열기 실패 err=%s", type(exc).__name__)
        return "dm_open_failed"
    dm_channel = str(((opened.get("channel") or {}).get("id")) or "")
    if not dm_channel.startswith("D"):
        # 열린 것이 DM 이 아니다. 여기서 멈추지 않으면 상세 답변이
        # **공개 채널로** 나갈 수 있다 — 응답 하나가 어긋났을 뿐인데.
        log.warning("VIP DM 채널이 아니다 — 보내지 않는다")
        return "dm_channel_invalid"
    try:
        client.chat_postMessage(channel=dm_channel, text=text)
    except Exception as exc:  # noqa: BLE001 - Slack 장애로 호출 전체를 죽이지 않는다
        log.warning("VIP DM 전송 실패 err=%s", type(exc).__name__)
        return "dm_post_failed"
    return ""


def _post_dm_channel(client, channel: str, text: str) -> str:
    """Post only to a DM channel already supplied by a verified Slack event."""
    if not str(channel or "").startswith("D"):
        return "dm_channel_invalid"
    try:
        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:  # noqa: BLE001 - Slack failure is a fixed outcome
        log.warning("VIP DM 전송 실패 err=%s", type(exc).__name__)
        return "dm_post_failed"
    return ""


def _notice(client, channel: str, *, thread_ts: str | None = None) -> bool:
    """채널에 **고정 안내만** 남긴다."""
    text = CHANNEL_NOTICE
    if text not in CHANNEL_NOTICES:
        raise ChannelLeak("only a fixed notice may be posted to a channel")
    try:
        client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except Exception as exc:  # noqa: BLE001 - 안내 실패가 DM 을 되돌리지 않는다
        log.warning("VIP 채널 안내 실패 err=%s", type(exc).__name__)
        return False
    return True


def _done(
    outcome: str,
    reason: str,
    workspace: str,
    slack_user: str,
    *,
    dm_sent: bool = False,
    notice_posted: bool = False,
) -> DeliveryResult:
    result = DeliveryResult(
        outcome=outcome,
        reason=reason,
        dm_sent=dm_sent,
        notice_posted=notice_posted,
    )
    # 고정 낱말과 참·거짓뿐이다. 질문·답변·근거는 여기 없다.
    log.info(
        "[%s] VIP 전달 결과=%s 사유=%s user=%s DM=%s 안내=%s",
        workspace,
        result.outcome,
        result.reason or "-",
        slack_user,
        result.dm_sent,
        result.notice_posted,
    )
    return result


__all__ = [
    "CHANNEL_NOTICE",
    "CHANNEL_NOTICES",
    "DELIVERY_OUTCOMES",
    "DELIVERY_REASONS",
    "NO_SOURCES",
    "VIPBOT",
    "ChannelLeak",
    "DeliveryResult",
    "deliver_channel_answer",
    "deliver_dm_answer",
]
