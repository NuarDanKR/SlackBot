"""접근 제어(ACL) / 권한 필터.

구현 지침: `.claude/skills/access-control`.

## 권한 3층 (독립된 축이다 — 하나가 다른 것을 대신하지 않는다)

| 축 | 통제 대상 | 정하는 주체 |
|---|---|---|
| 채널 멤버십 | 같은 워크스페이스 안에서 **어느 채널**을 볼 수 있나 | Slack 초대 |
| `share_with` | 이 문서를 **어느 다른 워크스페이스**에 넘길지 | 자료 소유 쪽 사람 |
| root 워크스페이스 | 산하 자료를 취합·열람하는 상위 조직 | 서버 운영자(`ROOT_WORKSPACES`) |

핵심 규칙:
- **채널에서 받은 질문은 그 채널 자료만 사용한다.** 질문자가 다른 채널에도 속해
  있거나 exec/root 권한이 있어도 현재 대화에 다른 채널 내용을 섞지 않는다.
- 여러 채널을 통합해 묻는 경로는 TYBot 개인 DM뿐이다. DM에서도 아래 멤버십과
  워크스페이스 경계는 그대로 적용한다.
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
    # 비어 있으면 DM 통합조회다. 채널 요청이면 둘 중 확인 가능한 값을 채워 해당
    # 채널만 허용한다. ID가 우선이고, v1 문서는 ID가 없어 표시 이름으로 폴백한다.
    channel_id: str = ""
    channel: str = ""

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
    channel_id: str | None = None,
    channel: str | None = None,
) -> bool:
    """막는 쪽이 기본값. 판정 순서를 바꾸지 말 것.

    0. 채널에서 온 질문이면 현재 워크스페이스의 현재 채널만 허용한다.
    1. 워크스페이스 경계 — exec/root 가 아니면 화이트리스트에 없을 때 여기서 끝.
    2. 다른 워크스페이스 자료: root 는 전량, 동등 워크스페이스는 `share_with` 명시분만.
    3. 자기 워크스페이스 자료: root 는 전량, 그 외는 **채널 멤버십**(또는 명시적 public).
    """
    if ctx.channel_id or ctx.channel:
        if owner_workspace != ctx.workspace:
            return False
        # 양쪽에 실제 Slack ID가 있으면 이름보다 ID를 신뢰한다. 이름은 바뀔 수 있다.
        if ctx.channel_id and channel_id and not str(channel_id).startswith("legacy-"):
            if ctx.channel_id != channel_id:
                return False
        elif not (ctx.channel and channel and ctx.channel == channel):
            # ID를 모르는 v1 문서는 표시 이름까지 같을 때만 허용한다.
            return False

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
