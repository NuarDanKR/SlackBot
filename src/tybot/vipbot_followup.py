"""VIPBot 후속 질문: **좌표만 되살리고 넓히지 않는다** (B-83).

30일 기록(`vipbot_history`)이 주는 것은 **질문과 좌표**다. 지난 답변 문장은
애초에 거기 없다 — 봇이 만든 문장을 다음 답의 근거로 쓰면 한 번 잘못 말한
숫자가 그대로 굳는다(원칙 1).

이 모듈이 하는 일은 셋이다.

1. **지칭이 있는지**를 결정론으로 본다(`intent.followup_hint`). 없으면 아무것도
   하지 않고, 호출자는 기존 `engine.answer()` 경로를 그대로 쓴다
2. 지칭이 있으면 그 좌표를 **지금 권한으로** 다시 연다
   (`ThreadFollowupResolver` → `ArchiveStore.resolve_refs`). VIP 요청의 권한은
   관리형 근거 채널 집합(`bot_source_channel`)이고, 지난번에 보였다는 사실은
   지금 보여도 된다는 뜻이 아니다
3. 못 열면 **못 열었다고 돌려준다.** 채널 전체 검색으로 되돌아가지 않는다

## 왜 넓히지 않는가

사용자는 좁게 물었는데 넓은 답을 받으면, 그 답이 자기 질문의 답이 **아니라는
사실을 알 방법이 없다.** 권한이 바뀌어 좌표가 닫힌 경우는 더 나쁘다 — 넓힌
검색이 그 사람이 지금 볼 수 있는 **다른** 자료를 끌어와, 닫힌 자료 대신 답이
되어 버린다. 사람은 자기가 물은 것에 답을 받았다고 읽는다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from .intent import Intent, followup_hint, topic_terms_of

log = logging.getLogger("tybot.vipbot_followup")

#: 이 모듈이 낼 수 있는 전부. 고정 낱말이라 그대로 세고 로그에 남길 수 있다.
PLAN_REASONS = frozenset(
    {
        "not_followup",
        "no_history",
        "resolved",
        "coordinates_unavailable",
    }
)


@dataclass(frozen=True)
class FollowupPlan:
    """후속 질문이 실제로 쓸 범위. **질문도 답변 문장도 들지 않는다.**"""

    reason: str
    hits: tuple = field(default_factory=tuple)
    parent_record_ids: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reason not in PLAN_REASONS:
            raise ValueError("unknown followup reason")
        if (self.reason == "resolved") != bool(self.hits):
            raise ValueError("resolved requires reopened coordinates")

    @property
    def applied(self) -> bool:
        """이 계획이 답변 범위를 정하는가.

        `False` 면 호출자는 **기존 경로를 그대로** 쓴다 — 후속 질문이 아니었거나
        이어 갈 기록이 없었다는 뜻이고, 둘 다 평범한 새 질문이다.
        """
        return self.reason in {"resolved", "coordinates_unavailable"}

    @property
    def widened(self) -> bool:
        """**항상 거짓이다.** 넓히는 길이 없다는 것을 계약으로 적어 둔다."""
        return False


def plan_followup(
    question: str,
    ctx,
    *,
    history: Sequence[dict],
    resolver,
) -> FollowupPlan:
    """지칭이 있으면 좌표를 지금 권한으로 되살린다. 없으면 아무것도 안 한다.

    `history` 는 `vipbot_history.load_vip_history()` 가 준 그대로다 — 질문과
    좌표만 들어 있고 **지난 답변 문장은 없다.**
    """
    text = str(question or "")
    mode, include_attachments = followup_hint(text)
    if mode == "none":
        return FollowupPlan("not_followup")

    turns = [row for row in history or () if isinstance(row, dict)]
    if not turns:
        # 이어 갈 기록이 없다. 그러면 이것은 그냥 새 질문이다 — 지칭 표현이
        # 있었다는 이유로 답을 막지 않는다.
        return FollowupPlan("no_history")

    # `kind` 는 해석기가 보지 않는다. 여기서 분류를 다시 하지 않는 이유는, 이
    # 모듈의 일이 **범위를 좁히는 것**이지 무엇을 묻는지 정하는 것이 아니기
    # 때문이다 — 그건 엔진이 질문 그대로 받아서 한다.
    intent = Intent(
        kind="search",
        question=text,
        reference_mode=mode,
        include_attachment_status=include_attachments,
        topic_terms=topic_terms_of(text),
    )
    # **채널로 좁히지 않는다.** VIP 의 범위는 묻는 채널이 아니라 관리형 근거
    # 채널 집합이고, 그 집합은 `resolve_refs` 가 `evidence_sources` 로 강제한다.
    resolved = resolver.resolve(intent, ctx, turns=turns, channel_id="")

    hits = tuple(getattr(resolved, "evidence_hits", ()) or ())
    dropped = tuple(getattr(resolved, "dropped_codes", ()) or ())
    parents = tuple(getattr(resolved, "parent_record_ids", ()) or ())
    log.info(
        "VIP 후속 좌표 mode=%s 요청=%d 복원=%d 제외=%s",
        mode,
        getattr(resolved, "refs_requested", 0),
        len(hits),
        "|".join(dropped) or "-",
    )
    if not hits:
        # 권한이 바뀌었거나, 지워졌거나, 좌표가 안 맞는다. 셋 다 **못 열었다**
        # 로 끝난다 — 어느 쪽인지 구분해 주면 벡터·아카이브가 존재 여부를 묻는
        # 창구가 된다(원칙 3).
        return FollowupPlan(
            "coordinates_unavailable", parent_record_ids=parents, dropped=dropped
        )
    return FollowupPlan(
        "resolved", hits=hits, parent_record_ids=parents, dropped=dropped
    )


__all__ = ["PLAN_REASONS", "FollowupPlan", "plan_followup"]
