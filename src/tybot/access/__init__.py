"""접근 제어(ACL) / 권한 필터 — 뼈대.

구현 지침: `.claude/skills/access-control`.
- 답변 생성 이전에 요청자 워크스페이스/채널 멤버십으로 검색 범위 축소.
- visibility 미설정 → 비공개 폴백. 권한 없음은 채널명도 숨김.
- 크로스 워크스페이스는 화이트리스트만.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RequestContext:
    """질의 요청자의 권한 컨텍스트."""

    workspace: str
    channels: frozenset[str] = field(default_factory=frozenset)
    role: str = "member"  # member | exec (통합조회 화이트리스트)


def can_access(
    ctx: RequestContext,
    *,
    visibility: str | None,
    acl: frozenset[str] | None,
    owner_workspace: str,
) -> bool:
    """막는 쪽이 기본값. 공개거나, ACL 매칭이거나, exec 화이트리스트일 때만 허용."""
    if visibility == "public":
        return True
    if ctx.role == "exec":
        return True
    if ctx.workspace != owner_workspace:
        return False  # 크로스 워크스페이스는 exec 외 차단
    if acl and (ctx.workspace in acl or ctx.channels & acl):
        return True
    return False
