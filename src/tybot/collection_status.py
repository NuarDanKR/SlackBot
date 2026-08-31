"""`/수집상태` — 이 채널이 수집되는지, 아니면 왜 안 되는지 그 자리에서 답한다.

## 왜 필요한가
수집 여부는 **채널 이름**이 정하고(`channels.should_collect`), 비공개 채널은 봇이 스스로
들어갈 수 없다. 두 조건이 겹쳐서 "왜 우리 채널은 수집이 안 되지?" 가 가장 흔한 질문이 된다.
지금은 그 답을 알려면 서버 로그를 봐야 한다.

## 설계
사실 수집(Slack 호출)과 문장 만들기를 분리한다. 여기 있는 것은 **순수 함수**라 테스트가
Slack 없이 모든 경우를 고정할 수 있다. 각 상태마다 **조치까지** 적는다 - 원인만 알려주고
방법을 안 알려주면 결국 담당자에게 다시 묻게 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .channels import parse

# 수집이 되는 상태 / 안 되는 이유
COLLECTING = "collecting"
NAME_MISMATCH = "name_mismatch"
NOT_MEMBER_PRIVATE = "not_member_private"
NOT_MEMBER_PUBLIC = "not_member_public"
AUTOJOIN_OFF = "autojoin_off"
DM = "dm"


@dataclass
class ChannelFacts:
    """Slack·아카이브에서 모은 사실. 판단은 하지 않는다."""

    channel: str = ""
    is_private: bool = False
    is_member: bool = False
    is_dm: bool = False
    autojoin_enabled: bool = True
    realtime_enabled: bool = True
    bot_name: str = "tybot"
    # 아카이브 통계 - 없으면 아직 한 줄도 안 쌓인 것이다.
    raw_lines: int = 0
    last_ingested: str | None = None
    write_problems: dict[str, str] = field(default_factory=dict)


def diagnose(f: ChannelFacts) -> str:
    """수집 여부 판정. 상태 코드 하나를 돌려준다."""
    if f.is_dm:
        return DM
    if parse(f.channel) is None:
        return NAME_MISMATCH
    if f.is_member:
        return COLLECTING
    if f.is_private:
        return NOT_MEMBER_PRIVATE
    if not f.autojoin_enabled:
        return AUTOJOIN_OFF
    return NOT_MEMBER_PUBLIC


def report(f: ChannelFacts) -> str:
    """사람이 읽고 바로 조치할 수 있는 문장. Slack ephemeral 로 보낸다."""
    state = diagnose(f)
    spec = parse(f.channel)

    if state == DM:
        return (
            "여기는 DM 이라 수집 대상이 아닙니다. "
            "업무 채널에서 `/수집상태` 를 실행하면 그 채널의 수집 여부를 알려 드립니다."
        )

    if state == NAME_MISMATCH:
        return "\n".join([
            f"🔴 *{f.channel} — 수집되지 않습니다*",
            "이름이 표준 규칙과 다릅니다. 수집 여부는 채널 이름이 정합니다.",
            "",
            "*형식* `#<본부|실|팀|현장|프로젝트>-<조직명>_<조직코드>-<업무>`",
            "*예시* `#팀-전산_ABB110-주간회의`",
            "",
            "*조치* `/채널 이름변경` 으로 표준 이름으로 바꾸면 그 시점부터 수집됩니다. "
            "이전 대화는 소급 수집되지 않습니다.",
        ])

    if state == NOT_MEMBER_PRIVATE:
        return "\n".join([
            f"🔴 *{f.channel} — 아직 수집되지 않습니다*",
            f"이름은 규칙에 맞습니다({spec.label() if spec else '-'}). "
            "다만 **비공개 채널에는 봇이 스스로 들어갈 수 없습니다**(Slack 제약).",
            "",
            f"*조치* 이 채널에서 `/invite @{f.bot_name}` 을 실행하세요. "
            "초대 직후부터 대화가 쌓입니다.",
        ])

    if state == AUTOJOIN_OFF:
        return "\n".join([
            f"🟡 *{f.channel} — 규칙에는 맞지만 봇이 아직 참여하지 않았습니다*",
            "자동 참여가 꺼져 있습니다(`AUTOJOIN_CHANNELS=0`).",
            "",
            f"*조치* `/invite @{f.bot_name}` 으로 직접 초대하거나 관리자에게 "
            "자동 참여를 켜 달라고 요청하세요.",
        ])

    if state == NOT_MEMBER_PUBLIC:
        return "\n".join([
            f"🟡 *{f.channel} — 곧 수집됩니다*",
            "이름이 규칙에 맞는 공개 채널이라 봇이 스스로 참여합니다. "
            "새로 만든 채널이면 잠시 뒤 자동으로 들어옵니다.",
            "",
            f"*지금 바로* 시작하려면 `/invite @{f.bot_name}` 을 실행하세요.",
        ])

    lines = [
        f"🟢 *{f.channel} — 수집 중입니다*",
        f"조직 분류: {spec.label() if spec else '-'}",
    ]
    if f.raw_lines:
        last = f.last_ingested or "-"
        lines.append(f"아카이브 원문 {f.raw_lines}줄 · 마지막 수집 {last}")
    else:
        lines.append(
            "아직 쌓인 원문이 없습니다. 대화가 오가면 실시간으로 저장되고, "
            f"`@{f.bot_name} 수집` 으로 과거 대화를 불러올 수도 있습니다."
        )
    if not f.realtime_enabled:
        lines.append(
            "⚠️ 실시간 수집이 꺼져 있습니다. 정기 백필로만 쌓입니다(`REALTIME_INGEST=0`)."
        )
    for label, why in f.write_problems.items():
        lines.append(f"🛑 *{label} 쓰기 불가* — {why}. 수집이 저장되지 않습니다.")

    lines += [
        "",
        "수집 대상: 대화 · 스레드 답글 · 첨부 본문 · 채널 캔버스. 봇 자신의 발언은 제외합니다.",
        "이름을 규칙 밖으로 바꾸면 그 시점부터 멈춥니다.",
    ]
    return "\n".join(lines)
