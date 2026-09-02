"""`/권한` — "내가 받는 답은 무엇을 근거로 하나" 를 사용자가 직접 확인한다.

## 왜
사내 피드백: "슬랙 내 어떤 콘텐츠 한도 안에서 답변되는지 알 수 없다."

`/수집상태` 는 **채널 하나**가 수집되는지 답한다. 그것만으로는 "내가 질문했을 때
어디까지가 근거가 되는가" 를 알 수 없다. 권한은 세 축으로 결정되는데
(채널 멤버십 / 문서 공유 표시 / 워크스페이스 등급) 사용자에게는 어느 축도 안 보인다.

## 무엇을 보여주고 무엇을 감추나
보여주는 것은 **범위와 건수**다. 채널 이름 목록은 보여주지 않는다 — 채널명에 조직·업무·
현장이 들어 있어 그 자체가 노출이고, 여기서 필요한 것은 "얼마나 넓은가" 이지
"무엇이 있나" 가 아니다. 무엇이 있는지는 그 채널에서 `/수집상태` 로 확인한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScopeFacts:
    """코드가 모은 사실. 판단·문장은 아래 함수가 만든다."""

    workspace_label: str = ""
    workspace_key: str = ""
    is_root: bool = False
    is_exec: bool = False
    # 이 사람이 멤버인 채널 중 수집 규칙에 맞는 것 / 규칙 밖인 것
    collected_channels: int = 0
    uncollected_channels: int = 0
    # 크로스 열람이 허용된 다른 워크스페이스
    readable: list[str] = field(default_factory=list)
    # 이 사람 기준으로 실제 답변 근거가 될 수 있는 문서·원문 줄
    visible_docs: int = 0
    visible_lines: int = 0
    lookup_failed: bool = False


HEAD = "*내 질문이 근거로 삼는 범위*"

# 사람이 오해하기 쉬운 두 가지를 못 박는다.
# 1) 봇은 학습하지 않는다 - 매 질문마다 원문을 다시 찾는다
# 2) 내가 못 보는 채널은 봇도 나에게 안 보여준다
COMMON_FOOTER = (
    "• 답변은 *매번 아카이브 원문을 다시 찾아* 만듭니다. 이전 대화나 제 답변을 "
    "학습하거나 근거로 쓰지 않습니다.\n"
    "• 특정 채널이 왜 수집되지 않는지는 그 채널에서 `/수집상태` 로 확인하세요."
)


def _footer(f: ScopeFacts) -> str:
    if f.is_exec or f.is_root:
        access = "• 임원용 최상위 권한으로 수집된 모든 워크스페이스 자료를 조회합니다.\n"
    else:
        access = "• 회원님이 멤버가 아닌 채널의 내용은 답변에 쓰이지 않습니다.\n"
    return COMMON_FOOTER.replace("• 특정", access + "• 특정")


def report(f: ScopeFacts) -> str:
    """사용자에게 보낼 문장(ephemeral)."""
    if f.lookup_failed:
        return (
            f"{HEAD}\n"
            "채널 목록을 조회하지 못해 정확한 건수를 보여드리지 못했습니다.\n\n"
            f"{_footer(f)}"
        )

    role = "일반 — 내가 속한 채널만"
    if f.is_exec:
        role = "임원 — 권한 범위 전체"
    elif f.is_root:
        role = "상위(root) — 산하 워크스페이스 자료까지"

    lines = [
        HEAD,
        f"*워크스페이스*: {f.workspace_label} (`{f.workspace_key}`)",
        f"*내 등급*: {role}",
        f"*내가 있는 수집 대상 채널*: {f.collected_channels}개",
    ]
    if f.uncollected_channels:
        lines.append(
            f"*수집되지 않는 채널*: {f.uncollected_channels}개 "
            "(이름이 표준 규칙과 달라 아카이브에 쌓이지 않습니다)"
        )
    if f.is_exec or f.is_root:
        lines.append("*함께 열람되는 워크스페이스*: 모든 등록 워크스페이스")
    elif f.readable:
        lines.append(f"*함께 열람되는 워크스페이스*: {', '.join(f.readable)}")
    else:
        lines.append("*함께 열람되는 워크스페이스*: 없음 (이 워크스페이스 자료만)")

    if f.visible_docs:
        lines.append(
            f"*지금 근거가 될 수 있는 자료*: 문서 {f.visible_docs}건 · 원문 {f.visible_lines}줄"
        )
    else:
        reason = "아직 수집된 원문이 없습니다" if (f.is_exec or f.is_root) else (
            "아직 수집된 원문이 없거나 회원님이 그 채널의 멤버가 아닙니다"
        )
        lines.append(f"*지금 근거가 될 수 있는 자료*: 없음 — {reason}")

    lines += ["", _footer(f)]
    return "\n".join(lines)

# --- 첫 사용 안내 --------------------------------------------------------------
#
# 처음 만나는 사람에게 필요한 건 명령어 목록이 아니라 **예시**다. `도움말` 은 이미
# 명령을 나열하고 있고, 그것만으로는 "내 업무에 이게 왜 쓸모 있나" 가 답되지 않는다.
#
# 문구가 길면 안 읽는다. 예시 3개와 한계 1줄, 그리고 다음 행동 하나로 끝낸다.
WELCOME = (
    "안녕하세요. 저는 우리 채널에 쌓인 *기록을 근거로만* 답하는 봇입니다.\n"
    "\n"
    "이런 질문에 가장 잘 맞습니다 — 답이 우리 기록 어딘가에 있는데 어디 있는지 "
    "모르겠을 때입니다.\n"
    "• `@{bot} 김해외동 기성금 얼마야` — 숫자·날짜를 원문에서 찾아 출처와 함께\n"
    "• `@{bot} 이번주 우리 팀 진행상황 정리해줘` — 여러 채널을 한 번에\n"
    "• `@{bot} 채널을 팀별로 쪼개는 게 나을까` — 판단이 필요한 질문(근거 유무를 밝힙니다)\n"
    "\n"
    "*근거를 못 찾으면 답하지 않습니다.* 지어내는 것보다 모른다고 하는 편이 낫기 "
    "때문입니다. 일반 상식을 묻는 데는 맞지 않습니다.\n"
    "\n"
    "지금 `/권한` 을 눌러 보세요 — 제가 회원님 질문에 무엇을 근거로 삼는지 보여드립니다."
)
