"""`/채널 상태` — 이 채널이 제대로 물려 있는가를 한 화면에서.

## 왜 한 화면인가

지금은 답을 알려면 명령을 세 개 쳐야 한다 — `/수집상태` 로 이름·초대, `/채널 검토자`
로 검토자, 서버 로그로 첨부. 그래서 아무도 다 확인하지 않는다.

넷 다 **틀려도 오류가 안 난다.** 이름이 규칙 밖이면 조용히 수집이 안 되고, 봇이
초대되지 않으면 조용히 비어 있고, 검토자가 없으면 조용히 요약이 반영되지 않고,
스캔 첨부는 조용히 안 읽힌다. 조용한 고장은 한 화면에 모아야 눈에 띈다.

## 판정을 여기서 새로 만들지 않는다

수집 여부는 `collection_status.diagnose` 가, 첨부는 `daily_review.blocked` 가,
이름은 `channels.parse` 가 이미 판정한다. 여기서 다시 세면 **화면과 실제가 갈린다** —
「수집 중」 이라 적혀 있는데 안 쌓이는 상태가 그렇게 생긴다.

## 모르는 것을 「없음」 이라 하지 않는다

검토자 DB 를 못 읽은 것과 검토자가 없는 것은 다른 문제이고 조치도 다르다.
못 읽었으면 그렇게 적는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .channels import parse
from .collection_status import (
    AUTOJOIN_OFF,
    COLLECTING,
    DM,
    NAME_MISMATCH,
    NOT_MEMBER_PRIVATE,
    ChannelFacts,
    diagnose,
)

OK = "🟢"
WARN = "🟡"
BAD = "🔴"
UNKNOWN = "⚪"

# 발송 시각은 KST 기준이다. 서버가 UTC 여도 사람이 보는 날짜는 KST 다.
KST = timezone(timedelta(hours=9))


def _today() -> str:
    return datetime.now(KST).date().isoformat()


@dataclass(frozen=True)
class Check:
    """검사 한 줄. `fix` 는 **사람이 지금 할 수 있는 행동**이어야 한다."""

    mark: str
    label: str
    detail: str
    fix: str = ""

    @property
    def healthy(self) -> bool:
        return self.mark == OK


@dataclass
class HealthFacts:
    """모은 사실. 판단은 하지 않는다 — 그래야 Slack 없이 전부 테스트한다."""

    channel: str = ""
    channel_id: str = ""
    is_private: bool = False
    is_member: bool = False
    is_dm: bool = False
    autojoin_enabled: bool = True
    realtime_enabled: bool = True
    bot_name: str = "tybot"
    raw_lines: int = 0
    last_ingested: str | None = None
    write_problems: dict[str, str] = field(default_factory=dict)
    # 검토자. `None` 은 **모른다**(DB 를 못 읽었다) — 「없음」 과 다르다.
    reviewers: list[str] | None = None
    send_at: str = ""
    # 사람이 봐야 원본을 읽는 첨부. `None` 이면 세지 못했다.
    waiting_attachments: int | None = None
    # 이 채널로 검토 DM 이 **실제로** 나간 마지막 날. `None` 이면 세지 못했다.
    #
    # 「검토자 있음」 만 보면 초록인데 DM 은 한 건도 안 가는 상태를 못 잡는다.
    # 2026-09-11 에 그 상태가 실제로 있었다 — 이력 표에 봇 권한이 없었고, 타이머는
    # enable 조차 안 돼 있었다. 두 고장이 다 이 틈으로 빠졌다. 원인은 둘이지만
    # **증상은 하나**여서, 증상을 사실로 들고 있으면 둘 다 잡힌다.
    last_digest: str | None = None
    # 검토자를 정한 날. 정한 직후에는 아직 안 가는 것이 정상이다.
    reviewer_since: str | None = None
    # 마지막 요약 검토 **회차**의 상태(B-50). `None` 이면 못 읽었다.
    #
    # 「검토 DM 이 나갔다」 만 보면 Canvas 가 계속 실패해 후보 DM 으로 폴백하는
    # 상태를 못 잡는다 — 사람은 링크가 없는 것을 보고도 그냥 그런가 보다 한다.
    review_canvas: str | None = None
    review_canvas_error: str = ""
    # **이 화면을 보는 사람**이 이 채널을 고칠 수 있는가.
    #
    # 「누군가는 고칠 수 있다」 를 보이면 안 된다 — 실제로 그렇게 만들었다가,
    # `/채널 수정` 은 거절되는데 같은 화면의 관리 항목은 초록으로 뜨는 상태가 됐다
    # (2026-09-08 실측). 화면이 자기 모순이면 사람은 화면을 안 믿는다.
    viewer_can_edit: bool = True
    # 고칠 수 있는 사람. TYBot 이 만든 채널은 **Slack 상 생성자가 봇**이라
    # 이 값이 비는 경우가 있다 — 그러면 아무도 못 고치는 채널이다.
    owner: str = ""
    admin_exists: bool = False


def _collection_facts(f: HealthFacts) -> ChannelFacts:
    return ChannelFacts(
        channel=f.channel,
        is_private=f.is_private,
        is_member=f.is_member,
        is_dm=f.is_dm,
        autojoin_enabled=f.autojoin_enabled,
        realtime_enabled=f.realtime_enabled,
        bot_name=f.bot_name,
        raw_lines=f.raw_lines,
        last_ingested=f.last_ingested,
        write_problems=f.write_problems,
    )


def check_name(f: HealthFacts) -> Check:
    """이름이 수집 여부를 정한다. 규칙 밖이면 **아무 일도 일어나지 않는다.**"""
    spec = parse(f.channel)
    if spec:
        return Check(OK, "이름 규칙", spec.label())
    return Check(
        BAD,
        "이름 규칙",
        "표준 형식이 아닙니다 — 이 채널은 수집되지 않습니다.",
        "`/채널 수정` 에서 조직·업무를 고르면 표준 이름으로 바꿉니다. "
        "형식: `#<본부|실|본사팀|현장|업무>-<조직명>_<조직코드>-<업무명>`",
    )


def check_membership(f: HealthFacts) -> Check:
    """봇이 채널에 있는가. 비공개 채널에는 **봇이 스스로 못 들어간다**(Slack 제약)."""
    state = diagnose(_collection_facts(f))
    if f.is_member:
        return Check(OK, "봇 참여", f"@{f.bot_name} 이 이 채널에 있습니다.")
    if state == NOT_MEMBER_PRIVATE:
        return Check(
            BAD, "봇 참여",
            "비공개 채널이라 봇이 스스로 들어갈 수 없습니다.",
            f"이 채널에서 `/invite @{f.bot_name}`",
        )
    if state == AUTOJOIN_OFF:
        return Check(
            WARN, "봇 참여",
            "자동 참여가 꺼져 있습니다(`AUTOJOIN_CHANNELS=0`).",
            f"`/invite @{f.bot_name}` 또는 관리자에게 자동 참여를 요청하세요.",
        )
    if state == NAME_MISMATCH:
        return Check(
            WARN, "봇 참여",
            "이름이 규칙 밖이라 자동 참여 대상이 아닙니다.",
            "이름을 먼저 고치세요.",
        )
    return Check(
        WARN, "봇 참여",
        "공개 채널이라 곧 자동으로 들어옵니다.",
        f"지금 시작하려면 `/invite @{f.bot_name}`",
    )


def check_collection(f: HealthFacts) -> Check:
    """쌓이고 있는가. 이름·참여가 맞아도 **쓰기가 막히면 저장되지 않는다.**"""
    state = diagnose(_collection_facts(f))
    if state != COLLECTING:
        return Check(
            BAD, "수집", "수집되지 않습니다.",
            "위의 이름·참여 항목을 먼저 해결하세요.",
        )
    if f.write_problems:
        why = " · ".join(f"{k}: {v}" for k, v in f.write_problems.items())
        return Check(
            BAD, "수집",
            f"쓰기가 막혀 저장되지 않습니다 — {why}",
            "서버 담당자에게 알려 주세요. 대화는 오가지만 남지 않습니다.",
        )
    if not f.raw_lines:
        return Check(
            WARN, "수집",
            "아직 쌓인 원문이 없습니다.",
            f"대화가 오가면 실시간으로 저장됩니다. 과거 대화는 `@{f.bot_name} 수집`.",
        )
    detail = f"원문 {f.raw_lines:,}줄 · 마지막 수집 {f.last_ingested or '-'}"
    if not f.realtime_enabled:
        return Check(
            WARN, "수집",
            detail + " · 실시간 수집이 꺼져 있습니다(`REALTIME_INGEST=0`).",
            "정기 백필로만 쌓입니다. 관리자에게 확인하세요.",
        )
    return Check(OK, "수집", detail)


def check_reviewer(f: HealthFacts) -> Check:
    """검토자가 없으면 **요약을 반영하지 않는다.** 자동 반영으로 물러서지 않는다."""
    if f.reviewers is None:
        return Check(
            UNKNOWN, "검토자",
            "확인하지 못했습니다(검토자 DB 를 읽지 못함).",
            "서버 담당자에게 알려 주세요. 확인 전까지 요약은 반영되지 않습니다.",
        )
    if not f.reviewers:
        return Check(
            BAD, "검토자",
            "없습니다 — 이 채널은 요약 후보를 보내거나 반영하지 않습니다.",
            "`/채널 수정` 에서 검토자와 보낼 시각을 정하세요.",
        )
    who = " ".join(f"<@{u}>" for u in f.reviewers)
    return Check(OK, "검토자", f"{who} · 매일 {f.send_at or '08:00'}")


def check_digest(f: HealthFacts) -> Check:
    """검토 DM 이 실제로 나가고 있는가.

    검토자 지정은 사람이 하는 일이고, 발송은 서버가 하는 일이다. 둘은 따로 고장난다.
    「검토자 있음」 초록 하나로 두 가지를 다 말하게 하면, 서버 쪽이 죽어 있을 때
    화면이 **정상이라고 거짓말한다.**

    여기서 원인까지 말하지는 않는다 — 권한인지 타이머인지 봇은 알 수 없다. 대신
    "나가지 않고 있다" 는 사실과, 서버에서 무엇을 볼지 한 줄을 준다.
    """
    if not f.reviewers:
        # 검토자가 없으면 안 가는 것이 당연하다. `check_reviewer` 가 이미 말했다.
        return Check(OK, "검토 DM", "검토자를 정하면 이 항목이 켜집니다.")
    if f.last_digest is None:
        return Check(UNKNOWN, "검토 DM", "확인하지 못했습니다(발송 이력을 읽지 못함).")
    if f.last_digest:
        return Check(OK, "검토 DM", f"마지막 발송 {f.last_digest}")
    if f.reviewer_since and f.reviewer_since >= _today():
        # 오늘 막 정했다. 아직 안 간 것이 정상이다.
        return Check(OK, "검토 DM", f"오늘 검토자를 정했습니다 — {f.send_at or '08:00'} 이후 발송")
    return Check(
        BAD, "검토 DM",
        "검토자는 있는데 **한 번도 나가지 않았습니다.** 발송 쪽이 멈춰 있습니다.",
        "서버 담당자에게 알려 주세요: `systemctl status tybot-review-dm.timer` 와 "
        "`journalctl -u tybot-review-dm` 을 보면 원인이 나옵니다.",
    )


def check_review_canvas(f: HealthFacts) -> Check:
    """요약 검토 Canvas 가 만들어지고 있는가(B-50).

    Canvas 실패는 **답변을 막지 않는다** — 후보 DM 으로 폴백한다. 그래서 조용히
    계속 실패할 수 있고, 그 상태에서는 검토자가 전체 맥락을 못 읽는다.

    `ambiguous` 는 따로 말한다. 그 회차는 **사람이 확인해야** 다음으로 간다 —
    Slack 이 이미 Canvas 를 만들었을 수 있어 자동 재생성을 하지 않는다.
    """
    if not f.reviewers:
        return Check(OK, "검토 Canvas", "검토자를 정하면 이 항목이 켜집니다.")
    if f.review_canvas is None:
        return Check(UNKNOWN, "검토 Canvas", "확인하지 못했습니다(회차 상태를 읽지 못함).")
    if f.review_canvas == "":
        return Check(OK, "검토 Canvas", "아직 회차가 없습니다.")
    if f.review_canvas == "ambiguous":
        return Check(
            BAD, "검토 Canvas",
            "지난 회차가 **만들어졌는지 확실하지 않아** 멈춰 있습니다.",
            "콘솔 `요약 검토 현황` 에서 그 회차를 확인하고 정리해 주세요. "
            "중복 Canvas 를 막기 위해 자동으로 다시 만들지 않습니다.",
        )
    if f.review_canvas == "failed":
        detail = "Canvas 를 만들지 못해 후보 DM 으로 보냈습니다."
        return Check(
            WARN, "검토 Canvas",
            f"{detail} 코드: `{f.review_canvas_error or '알 수 없음'}`",
            "`canvases:write` 권한과 워크스페이스의 Canvas 사용 여부를 확인하세요.",
        )
    return Check(OK, "검토 Canvas", f"마지막 회차 상태: {f.review_canvas}")


def check_attachments(f: HealthFacts) -> Check:
    """자동 변환에 실패했거나 지원하지 않아 운영 확인이 필요한 첨부."""
    if f.waiting_attachments is None:
        return Check(UNKNOWN, "첨부", "확인하지 못했습니다.")
    if not f.waiting_attachments:
        return Check(OK, "첨부", "자동 변환에 실패한 파일이 없습니다.")
    return Check(
        WARN, "첨부",
        f"자동 변환하지 못한 첨부 {f.waiting_attachments}건이 있습니다.",
        "파일 변환 상세는 검토자에게 DM으로 보내지 않습니다. "
        "관리 콘솔의 아카이브 진단에서 확인하세요.",
    )


def check_manager(f: HealthFacts) -> Check:
    """**이 화면을 보는 사람**이 고칠 수 있는가.

    「누군가는 고칠 수 있다」 를 보이면 화면이 자기 모순이 된다 — `/채널 수정` 은
    거절되는데 관리 항목만 초록으로 뜬다. 그러면 사람은 무엇이 맞는지 모른다.
    """
    if f.viewer_can_edit:
        return Check(OK, "관리", "당신이 이 채널을 수정할 수 있습니다.")
    if f.owner:
        return Check(
            WARN, "관리",
            f"당신은 수정 권한이 없습니다. 개설자는 <@{f.owner}> 입니다.",
            "그 사람에게 `/채널 수정` 을 요청하세요.",
        )
    if f.admin_exists:
        return Check(
            WARN, "관리",
            "당신은 수정 권한이 없고, 개설자 기록도 없습니다.",
            "TYBot 채널 관리자에게 요청하세요.",
        )
    return Check(
        BAD, "관리",
        "이 채널을 수정할 수 있는 사람이 없습니다 — 개설 기록이 없고 "
        "관리자도 지정되지 않았습니다.",
        "TYBot 이 만든 채널은 Slack 상 생성자가 봇이라 사람으로 되돌릴 수 "
        "없습니다. 서버 담당자에게 `CHANNEL_ADMIN_USERS` 등록을 요청하세요.",
    )


CHECKS = (
    check_name,
    check_membership,
    check_collection,
    check_reviewer,
    check_digest,
    check_review_canvas,
    check_attachments,
    check_manager,
)


def checks(f: HealthFacts) -> list[Check]:
    return [fn(f) for fn in CHECKS]


DM_NOTICE = (
    "여기는 DM 이라 채널 상태를 볼 수 없습니다. "
    "업무 채널에서 `/채널 상태` 를 실행하세요."
)


def report(f: HealthFacts) -> str:
    """사람이 읽고 바로 조치할 수 있는 문장.

    **머리글이 결론을 말한다.** 목록만 주면 사람이 초록·빨강을 세어야 하는데,
    바쁠 때는 안 센다.
    """
    if f.is_dm:
        return DM_NOTICE

    rows = checks(f)
    broken = [c for c in rows if c.mark == BAD]
    warned = [c for c in rows if c.mark == WARN]
    unknown = [c for c in rows if c.mark == UNKNOWN]

    if broken:
        head = f"{BAD} *{f.channel} — {len(broken)}가지가 막혀 있습니다*"
    elif warned or unknown:
        head = f"{WARN} *{f.channel} — 동작하지만 확인할 것이 있습니다*"
    else:
        head = f"{OK} *{f.channel} — 모두 정상입니다*"

    lines = [head, ""]
    for check in rows:
        lines.append(f"{check.mark} *{check.label}* — {check.detail}")
        if check.fix:
            lines.append(f"　└ {check.fix}")
    lines += ["", "고치기: `/채널 수정` · 만들기: `/채널 생성`"]
    return "\n".join(lines)


__all__ = [
    "BAD",
    "CHECKS",
    "DM",
    "DM_NOTICE",
    "OK",
    "UNKNOWN",
    "WARN",
    "Check",
    "HealthFacts",
    "checks",
    "report",
]
