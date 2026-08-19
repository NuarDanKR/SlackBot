"""접근 제어(ACL) / 권한 필터.

구현 지침: `.claude/skills/access-control`.
- 답변 생성 이전에 요청자 워크스페이스/채널 멤버십으로 검색 범위 축소.
- visibility 미설정 → 비공개 폴백. 권한 없음은 채널명도 숨김.
- 크로스 워크스페이스는 화이트리스트만, 그중에서도 **공개 표시된 문서만**.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RequestContext:
    """질의 요청자의 권한 컨텍스트."""

    workspace: str
    channels: frozenset[str] = field(default_factory=frozenset)
    role: str = "member"  # member | exec (통합조회 화이트리스트)
    # 이 요청자가 자기 워크스페이스 외에 **추가로** 볼 수 있는 워크스페이스.
    # 설정(CROSS_WS_READ)에서 명시한 것만 채워진다. 기본은 비어 있다.
    readable_workspaces: frozenset[str] = field(default_factory=frozenset)

    def may_reach(self, owner_workspace: str) -> bool:
        """워크스페이스 경계 판정. 답변 생성 이전 1차 필터."""
        if self.role == "exec":
            return True
        return owner_workspace == self.workspace or owner_workspace in self.readable_workspaces


def can_access(
    ctx: RequestContext,
    *,
    visibility: str | None,
    acl: frozenset[str] | None,
    owner_workspace: str,
) -> bool:
    """막는 쪽이 기본값.

    판정 순서(바꾸지 말 것):
    1. 워크스페이스 경계 — 화이트리스트에 없는 워크스페이스는 여기서 끝.
    2. 크로스 워크스페이스는 `visibility: public` 문서만. 화이트리스트는 '볼 수 있는 후보'를
       넓힐 뿐이고, 무엇을 공개할지는 문서 소유 쪽이 정한다(원칙 3·4).
    3. 자기 워크스페이스 안에서는 공개 문서이거나 채널 멤버십이 ACL 과 겹칠 때만.
    """
    if not ctx.may_reach(owner_workspace):
        return False
    if ctx.role == "exec":
        return True

    if owner_workspace != ctx.workspace:
        # 크로스 워크스페이스는 관문 두 개를 모두 통과해야 한다:
        #   (1) 설정의 화이트리스트 — 위 may_reach 에서 이미 확인
        #   (2) 문서에 `visibility: public` 표시 — 사람이 명시적으로 공개한 것만
        # acl 은 '소유 워크스페이스 안의 채널 목록'이라 크로스 판정에는 쓰지 않는다.
        # (수집기가 acl=[채널명] 을 항상 넣기 때문에, 여기에 acl 을 걸면 공개 표시가 무력해진다.)
        return visibility == "public"

    if visibility == "public":
        return True
    if acl and (ctx.workspace in acl or ctx.channels & acl):
        return True
    return False
