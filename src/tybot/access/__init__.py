"""접근 제어(ACL) / 권한 필터.

구현 지침: `.claude/skills/access-control`.

## 권한 3층 (독립된 축이다 — 하나가 다른 것을 대신하지 않는다)

| 축 | 통제 대상 | 정하는 주체 |
|---|---|---|
| 채널 멤버십 | 같은 워크스페이스 안에서 **어느 채널**을 볼 수 있나 | Slack 초대 |
| `share_with` | 이 문서를 **어느 다른 워크스페이스**에 넘길지 | 자료 소유 쪽 사람 |
| root 워크스페이스 | 산하 자료를 취합·열람하는 상위 조직 | 서버 운영자(`ROOT_WORKSPACES`) |

핵심 규칙:
- **같은 워크스페이스라도 소속되지 않은 채널은 답하지 않는다.** 공개 채널이어도 마찬가지다.
  Slack 에서 그 채널에 들어가 있지 않은 사람은 봇을 통해 우회 열람할 수 없다.
- **동등(peer) 워크스페이스로는 문서에 명시된 것만 넘어간다**(`share_with`).
  화이트리스트(`CROSS_WS_READ`)는 '넘어갈 수 있는 후보'를 정하고, 무엇을 넘길지는 소유 쪽이 정한다.
- **root 워크스페이스**(임원용 최상위 워크스페이스)는 모든 워크스페이스 자료를
  문서 표시와 무관하게 열람하고,
  자기 워크스페이스 안에서 채널 멤버십 필터를 받지 않는다.
- `visibility: public` 은 **자기 워크스페이스 안에서만** 멤버십을 면제하는 표시다.
  크로스 워크스페이스 권한과는 무관하다(예전에는 이 하나가 둘 다 열어서 위험했다).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RequestContext:
    """질의 요청자의 권한 컨텍스트."""

    workspace: str
    channels: frozenset[str] = field(default_factory=frozenset)
    role: str = "member"  # member | exec (개인 단위 통합조회 화이트리스트)
    # 설정(CROSS_WS_READ)에서 명시한, 이 워크스페이스가 볼 수 있는 다른 워크스페이스.
    readable_workspaces: frozenset[str] = field(default_factory=frozenset)
    # 상위(root) 워크스페이스에서 온 요청인가 (ROOT_WORKSPACES).
    is_root: bool = False

    def may_reach(self, owner_workspace: str) -> bool:
        """워크스페이스 경계 판정. 답변 생성 이전 1차 필터."""
        if self.role == "exec" or self.is_root:
            return True
        return owner_workspace == self.workspace or owner_workspace in self.readable_workspaces


def can_access(
    ctx: RequestContext,
    *,
    visibility: str | None,
    acl: frozenset[str] | None,
    owner_workspace: str,
    share_with: frozenset[str] | None = None,
) -> bool:
    """막는 쪽이 기본값. 판정 순서를 바꾸지 말 것.

    1. 워크스페이스 경계 — exec/root 가 아니면 화이트리스트에 없을 때 여기서 끝.
    2. 다른 워크스페이스 자료: root 는 전량, 동등 워크스페이스는 `share_with` 명시분만.
    3. 자기 워크스페이스 자료: root 는 전량, 그 외는 **채널 멤버십**(또는 명시적 public).
    """
    if not ctx.may_reach(owner_workspace):
        return False
    if ctx.role == "exec":
        return True

    if owner_workspace != ctx.workspace:
        # 상위 조직은 산하 자료를 열람할 책임과 권한이 있다.
        if ctx.is_root:
            return True
        # 동등 워크스페이스끼리는 소유 쪽이 명시한 것만 넘어간다.
        return bool(share_with and ctx.workspace in share_with)

    # --- 자기 워크스페이스 ---
    if ctx.is_root:
        # 취합·열람 전담 워크스페이스는 채널 멤버십 필터를 받지 않는다.
        return True
    if visibility == "public":
        # 사람이 명시적으로 '워크스페이스 전체 공개'로 표시한 문서.
        return True
    # 채널 멤버십(또는 워크스페이스 단위 acl 항목)이 겹칠 때만 허용한다.
    return bool(acl and (ctx.workspace in acl or ctx.channels & acl))
