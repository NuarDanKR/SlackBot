"""콘솔 사용자 식별.

VPN 안에서만 열리지만 **누가 승인했는지 남겨야 하므로** 사용자 식별은 필요하다.
"VPN 안이니 아무나"로 두면 승인 기록에 이름을 쓸 수 없고, 원문 열람 기록도 의미가 없어진다.

## 두 가지 방식
| `CONSOLE_AUTH` | 식별 방법 | 언제 |
|---|---|---|
| `token` (기본) | `Authorization: Bearer <토큰>` | 프록시가 없을 때. 파일럿 기본값 |
| `proxy` | 프록시가 넣어 주는 `X-Forwarded-Email` 헤더 | VPN 앞단에 인증 프록시를 둘 때 |

`proxy` 는 **프록시를 반드시 거치는 구성에서만** 안전하다. 헤더는 누구나 만들 수 있으므로,
콘솔을 직접 노출한 채로 이 모드를 쓰면 아무나 관리자가 된다. 그래서 기본값은 `token` 이다.

## 설정 형식
    CONSOLE_USERS=토큰:이메일:역할:워크스페이스|워크스페이스, ...
    예) CONSOLE_USERS=s3cr3t:dan@taeyoung.com:owner:*, tok2:sh.kim@taeyoung.com:member:fin

역할은 `owner` 또는 `member`. `owner` 만 승인·시크릿·원문 열람을 할 수 있다.
워크스페이스는 `member` 가 다룰 범위이고 `*` 는 전체다(owner 는 어차피 전체).

토큰은 비교할 때 `secrets.compare_digest` 를 쓴다 — 앞자리부터 하나씩 맞춰 보는 공격을 막는다.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

logger = logging.getLogger("tybot.console.auth")

OWNER = "owner"
MEMBER = "member"


class AuthError(RuntimeError):
    """인증 실패. 라우터가 401 로 바꾼다."""


@dataclass(frozen=True)
class ConsoleUser:
    email: str
    name: str
    role: str
    workspaces: frozenset[str] = field(default_factory=frozenset)
    # 전체 워크스페이스를 다룰 수 있는가 (owner 이거나 `*` 로 지정된 경우)
    all_workspaces: bool = False

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    def may_see(self, workspace: str) -> bool:
        return self.all_workspaces or workspace in self.workspaces

    def display(self) -> str:
        return self.name or self.email.split("@")[0]


@dataclass(frozen=True)
class _Entry:
    token: str
    user: ConsoleUser


def _name_from_email(email: str) -> str:
    return email.split("@")[0]


def parse_users(spec: str | None) -> list[_Entry]:
    """`CONSOLE_USERS` 파싱. 형식이 틀리면 그 항목만 버리고 경고한다.

    한 줄이 잘못됐다고 콘솔 전체가 안 뜨면 곤란하지만, **아무도 못 들어가는 상태**도 위험하므로
    버려진 항목은 반드시 로그에 남긴다.
    """
    out: list[_Entry] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 3:
            logger.warning("CONSOLE_USERS 항목 형식 오류(무시): %r — 토큰:이메일:역할[:워크스페이스]", chunk)
            continue
        token, email, role = parts[0].strip(), parts[1].strip(), parts[2].strip()
        ws_spec = parts[3].strip() if len(parts) > 3 else ""
        if not token or "@" not in email or role not in (OWNER, MEMBER):
            logger.warning("CONSOLE_USERS 항목 값 오류(무시): %r", chunk)
            continue
        all_ws = role == OWNER or ws_spec == "*"
        workspaces = frozenset(
            w.strip() for w in ws_spec.split("|") if w.strip() and w.strip() != "*"
        )
        out.append(
            _Entry(
                token=token,
                user=ConsoleUser(
                    email=email,
                    name=_name_from_email(email),
                    role=role,
                    workspaces=workspaces,
                    all_workspaces=all_ws,
                ),
            )
        )
    return out


class Authenticator:
    """요청 헤더에서 사용자를 찾아낸다."""

    def __init__(self, *, mode: str | None = None, users_spec: str | None = None) -> None:
        self.mode = (mode or os.getenv("CONSOLE_AUTH", "token")).strip().lower()
        self.entries = parse_users(
            users_spec if users_spec is not None else os.getenv("CONSOLE_USERS")
        )
        if not self.entries:
            logger.warning(
                "CONSOLE_USERS 가 비어 있습니다. 콘솔 API 는 모든 요청을 거절합니다. "
                "형식: 토큰:이메일:역할[:워크스페이스|워크스페이스]"
            )

    def by_email(self, email: str) -> ConsoleUser | None:
        for e in self.entries:
            if e.user.email.lower() == email.lower():
                return e.user
        return None

    def identify(self, *, authorization: str | None, forwarded_email: str | None) -> ConsoleUser:
        if self.mode == "proxy":
            if not forwarded_email:
                raise AuthError("인증 프록시가 사용자 정보를 넘기지 않았습니다.")
            user = self.by_email(forwarded_email)
            if user is None:
                raise AuthError(f"{forwarded_email} 는 콘솔 사용자로 등록되어 있지 않습니다.")
            return user

        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("인증 토큰이 없습니다. Authorization: Bearer <토큰> 을 보내세요.")
        token = authorization[7:].strip()
        for e in self.entries:
            # 길이·내용이 조금씩 맞아 가는지 알 수 없게 상수 시간으로 비교한다
            if secrets.compare_digest(e.token, token):
                return e.user
        raise AuthError("등록되지 않은 토큰입니다.")
