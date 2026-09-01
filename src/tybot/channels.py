"""채널 명명 규칙 — 수집 대상 판정과 조직 코드 추출.

## 규칙
`#<두문자>-<조직명>_<조직코드>-<업무>`  예) `#본사팀-전산_ABB110-주간회의`

두문자가 **본부·실·본사팀·현장·업무** 중 하나면 수집 대상이다(옛 이름 `팀`·`프로젝트` 도 인식). 아니면 수집하지 않는다.
잡담·개인·외부 협업 채널을 이름만으로 걸러내기 위한 장치다.

조직코드를 이름에 박는 이유는 **조직 개편·워크스페이스 통합에 대비**하기 위해서다.
조직명은 바뀌어도 코드는 유지되므로, 나중에 코드로 조직 트리에 붙일 수 있다.

구 형식(`#팀_자금(ABB540)_주간보고`)은 **폐기**됐다. 인식하지 않으므로 그런 이름의 채널은
수집 대상이 아니다. 이미 수집된 문서는 남지만 새 수집은 멈춘다 — 채널 이름을 신형식으로
바꾸면 `channel_rename` 이벤트로 그 순간부터 다시 수집된다.

## 이것이 여는 것
공개 채널은 봇이 **스스로 참여**(`conversations.join`)할 수 있으므로, 규칙에 맞는 채널은
사람이 `/invite` 하지 않아도 수집이 시작된다.

**비공개 채널은 봇 토큰으로 자가 참여가 불가능하다.** Slack 설계상 봇은 자기가 없는 비공개
채널을 목록 조회조차 못 한다. 사람이 `/invite` 하거나, 관리자 사용자 토큰으로 일괄 초대해야
한다(`scripts/invite_bot.py`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 새로 만들 때 고를 수 있는 두문자 → 조직 종류(조직 트리 연결용).
#
# 조직 구조: 본부 > 본사팀 > (현장), 또는 실 > 본사팀.
# `본사팀` 은 현장 조직과 헷갈리지 않게 `팀` 대신 쓴다.
# `업무` 는 조직이 아니라 **다른 팀과 협업하는 채널**이다. 주관 팀의 코드를 빌린다.
PREFIX_KINDS: dict[str, str] = {
    "본부": "hq",
    "실": "div",
    "본사팀": "team",
    "현장": "site",
    "업무": "task",
}
COLLECT_PREFIXES = frozenset(PREFIX_KINDS)

# 예전 두문자. **새로 만들 때는 고를 수 없지만 인식은 계속한다.**
# 인식을 끊으면 이미 만들어진 채널이 그 순간 수집 대상에서 빠진다 - 사람은 아무것도
# 하지 않았는데 기록이 멈추는 것이 가장 나쁜 실패다.
LEGACY_PREFIX_KINDS: dict[str, str] = {
    "팀": "team",
    "프로젝트": "task",
}
ALL_PREFIX_KINDS: dict[str, str] = {**PREFIX_KINDS, **LEGACY_PREFIX_KINDS}
PARSE_PREFIXES = frozenset(ALL_PREFIX_KINDS)

# 예전 두문자를 새 두문자로 옮길 때 쓴다(이름 변경 모달의 초기값 등).
PREFIX_ALIASES: dict[str, str] = {"팀": "본사팀", "프로젝트": "업무"}

# 형식: #본사팀-전산_ABB110-주간회의
# 긴 두문자를 먼저 시도해야 한다 - `본사팀` 이 `팀` 보다 앞이 아니면 영영 매치되지 않는다.
_PREFIX_ALT = "|".join(sorted(PARSE_PREFIXES, key=len, reverse=True))
CHANNEL_RE = re.compile(
    rf"^#?(?P<prefix>{_PREFIX_ALT})-(?P<org>[^_]+)_(?P<code>[0-9A-Za-z]+)"
    r"(?:-(?P<task>.*))?$"
)


@dataclass(frozen=True)
class ChannelSpec:
    """규칙에 맞는 채널에서 뽑아낸 것."""

    raw: str
    prefix: str  # 본부 | 실 | 본사팀 | 현장 | 업무 (옛 이름 팀·프로젝트도 인식)
    org_name: str
    org_code: str
    task: str

    @property
    def kind(self) -> str:
        """조직 종류(hq/div/team/site/task). 조직 트리 연결에 쓴다."""
        return ALL_PREFIX_KINDS[self.prefix]

    def label(self) -> str:
        return f"{self.prefix} {self.org_name}({self.org_code}) · {self.task or '-'}"


def parse(channel: str) -> ChannelSpec | None:
    """채널명을 해석한다. 규칙에 안 맞으면 None(= 수집 대상 아님)."""
    name = (channel or "").strip()
    if not name:
        return None
    m = CHANNEL_RE.match(name)
    if not m:
        return None
    org = (m.group("org") or "").strip()
    if not org:
        return None
    return ChannelSpec(
        raw=name if name.startswith("#") else f"#{name}",
        prefix=m.group("prefix"),
        org_name=org,
        org_code=m.group("code"),
        task=(m.group("task") or "").strip(),
    )


def should_collect(channel: str) -> bool:
    """이 채널을 수집할 것인가. 규칙에 맞으면 True."""
    return parse(channel) is not None


def explain(channel: str) -> str:
    """왜 수집 대상이 아닌지 사람에게 설명한다(운영 리포트·안내용)."""
    spec = parse(channel)
    if spec:
        return f"수집 대상: {spec.label()}"
    return (
        "수집 대상 아님 - 이름이 규칙과 다릅니다. "
        f"`#<{'|'.join(sorted(COLLECT_PREFIXES))}>-<조직명>_<조직코드>-<업무>` 형식이어야 합니다. "
        "예: #본사팀-전산_ABB110-주간회의"
    )
