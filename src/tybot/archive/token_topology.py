"""App Token 이 같은 연결은 **Socket 을 하나만 연다.**

결정: 2026-09-29 supervisor 작업지시서 §2.1 · §7-1

## 왜 세는가

Slack 은 같은 앱의 Socket Mode 연결이 여러 개일 때 payload 를 **어느 연결로 보낼지
보장하지 않는다.** workspace 마다 무조건 WebSocket 을 하나씩 열면, 같은 앱을 두
워크스페이스에 설치한 순간부터 이벤트가 둘 중 아무 데나 간다. 한쪽 worker 만 그
이벤트를 받고 다른 쪽은 조용하다 — 오류가 아니라 **누락**으로 나타난다.

그래서 supervisor 를 만들기 전에 지금 토큰이 어떻게 묶여 있는지부터 센다. 같은 App
Token 이 몇 개인지 모르면 worker 를 몇 개 열어야 하는지도 모른다.

## 평문을 남기지 않는다

토큰을 비교하려면 값을 알아야 하지만, **비교 결과만 남기고 값은 버린다.**

- 묶음 표시는 실행할 때마다 새로 만드는 무작위 소금으로 만든 HMAC 이다
- 그래서 같은 토큰이라도 **실행이 다르면 표시가 다르다.** 로그에 남은 표시를 모아
  토큰을 되짚을 수 없다
- 표시는 앞 8자만 쓰고, 그것도 화면에는 `그룹 1` 같은 순번으로 바꿔 보여 준다

토큰·표시·DSN 은 반환값에도 출력에도 들어가지 않는다(금지사항 §8).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

#: 한 실행 안에서만 쓰는 소금. 모듈이 로드될 때 한 번 만든다.
_SALT = os.urandom(32)


def fingerprint(token: str) -> str:
    """같은 값인지 **이 실행 안에서만** 비교할 수 있는 표시.

    실행마다 소금이 바뀌므로 표시를 저장해 두고 나중에 대조할 수 없다. 그게 의도다 —
    저장할 수 있으면 언젠가 저장되고, 저장된 것은 언젠가 새어 나간다.
    """
    if not token:
        return ""
    return hmac.new(_SALT, token.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


@dataclass(frozen=True)
class Connection:
    """조사 대상 연결 하나. **평문 토큰은 이 객체 안에서 끝난다.**"""

    workspace: str
    bot_key: str
    bot_token: str = ""
    app_token: str = ""
    state: str = ""


@dataclass
class SocketGroup:
    """Socket 연결 하나가 맡을 workspace 들."""

    index: int
    workspaces: list[str] = field(default_factory=list)
    #: 이 그룹에 App Token 이 없는 연결이 섞였나. 그러면 Socket 을 못 연다.
    missing_app_token: bool = False


@dataclass
class Topology:
    groups: list[SocketGroup] = field(default_factory=list)
    #: 같은 Bot Token 을 두 workspace 가 쓰고 있다. **설치가 잘못된 것**이다.
    shared_bot_tokens: list[list[str]] = field(default_factory=list)
    #: App Token 이 없어 Socket 을 열 수 없는 연결.
    without_app_token: list[str] = field(default_factory=list)

    @property
    def socket_count(self) -> int:
        return len([g for g in self.groups if not g.missing_app_token])


def survey(connections: list[Connection]) -> Topology:
    """지금 토큰이 어떻게 묶여 있나. **값은 안 돌려준다.**

    같은 App Token → 한 Socket. 다른 App Token → 다른 Socket.

    Bot Token 이 겹치는 것은 다른 문제다. 그건 한 워크스페이스의 토큰을 다른
    워크스페이스 연결에 붙여 넣은 것이고, 그대로 두면 **남의 워크스페이스에
    수집한다.** 오류가 아니라 유출이므로 따로 센다.
    """
    by_app: dict[str, list[str]] = {}
    by_bot: dict[str, list[str]] = {}
    missing: list[str] = []

    for item in connections:
        # Bot Token 중복은 App Token 유무와 무관한 설치 오류다. App Token 이
        # 비었다고 먼저 건너뛰면 가장 위험한 교차-workspace 오설정을 놓친다.
        bot_mark = fingerprint(item.bot_token)
        if bot_mark:
            by_bot.setdefault(bot_mark, []).append(item.workspace)
        app_mark = fingerprint(item.app_token)
        if not app_mark:
            missing.append(item.workspace)
            continue
        by_app.setdefault(app_mark, []).append(item.workspace)

    groups = [
        SocketGroup(index=order, workspaces=sorted(workspaces))
        for order, (_, workspaces) in enumerate(sorted(by_app.items()), start=1)
    ]
    if missing:
        groups.append(SocketGroup(
            index=len(groups) + 1, workspaces=sorted(missing), missing_app_token=True,
        ))
    return Topology(
        groups=groups,
        shared_bot_tokens=[
            sorted(workspaces) for workspaces in by_bot.values() if len(workspaces) > 1
        ],
        without_app_token=sorted(missing),
    )


def summarize(topology: Topology) -> list[str]:
    """사람이 읽을 요약. **토큰도 표시도 안 들어간다.**"""
    lines = [
        f"열어야 하는 Socket 연결: {topology.socket_count}개",
    ]
    for group in topology.groups:
        if group.missing_app_token:
            lines.append(
                f"  · (App Token 없음) {', '.join(group.workspaces)}"
                " — 앱 토큰을 등록해야 Socket 을 엽니다"
            )
            continue
        lines.append(f"  · 그룹 {group.index}: {', '.join(group.workspaces)}")
    if topology.shared_bot_tokens:
        lines.append("")
        lines.append("같은 Bot Token 을 쓰는 워크스페이스가 있습니다 — 설치를 확인하세요:")
        for shared in topology.shared_bot_tokens:
            lines.append(f"  · {', '.join(shared)}")
        lines.append(
            "  한 워크스페이스의 토큰이 다른 워크스페이스 연결에 들어가 있으면"
            " 남의 대화를 수집합니다."
        )
    return lines
