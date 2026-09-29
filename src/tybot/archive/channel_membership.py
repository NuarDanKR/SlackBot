"""수집 대상은 **Archiving Bot 이 초대된 채널**이다.

결정: 2026-09-29 오너 지시 · 설계
`docs/design/archiver-shadow-console-operations-2026-09-28.md`

## 왜 손으로 적던 목록을 버리나

사람이 채널 ID 를 적어 넣는 방식은 두 가지를 못 막았다.

1. **초대는 했는데 등록을 잊은 채널.** 그 방 사람들은 초대했으니 수집되는 줄 안다.
   수집은 안 되고, 몇 달 뒤 「그 얘기 어디 갔냐」 로 드러난다
2. **초대를 푼 뒤에도 목록에 남은 채널.** 권한 오류만 쌓이고 오류는 로그에만 남는다

초대는 Slack 화면에서 그 방 사람들이 한다. 그게 이미 「여기 수집해도 된다」 는
의사표시다. 그 의사표시를 정본으로 쓰면 두 목록이 어긋날 일이 없다.

## 앱 설치 ≠ 수집

앱을 워크스페이스에 깔았다고 전 채널을 수집하지 않는다. **초대된 채널만** 본다
(`users.conversations` 는 봇이 참여한 대화만 돌려준다). 공개·비공개 둘 다 받고
DM 은 받지 않는다 — DM 은 개인 작업공간이고 별도 규칙이 있다(CLAUDE.md).

## 이름 규칙은 보지 않는다

`#팀_…` 규칙은 사람이 채널을 찾기 위한 것이다. 규칙에 안 맞는 채널이라도 봇을
초대했으면 그 방은 수집을 기대한다. 이름으로 거르면 **초대했는데 안 되는** 첫
번째 경우로 돌아간다.

## 나가면 멈춘다. 다시 들어와도 사람이 끈 것은 안 켠다

봇이 채널에서 빠지면 `membership='left'` 이고 수집을 멈춘다(`paused`). 다시
초대되면 그림자 수집으로 돌아온다 — 단 **사람이 명시적으로 끈 채널은 예외다**
(`operator_hold`). 그걸 구분하지 않으면 재초대 한 번에 끈 채널까지 되살아나고,
끈 사람은 그 사실을 모른다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("tybot.archive.channel_membership")

#: 한 번에 받아 오는 대화 수. Slack 기본은 100 이고 상한은 1000 이다.
PAGE_SIZE = 200

#: 받아 올 대화 종류. **DM 은 없다**(`im`·`mpim`).
CONVERSATION_TYPES = "public_channel,private_channel"

#: 자동 등록될 때의 모드. 그림자다 — 운영 원문은 계속 Master 가 쓴다.
DEFAULT_MODE = "shadow"

JOINED, LEFT, UNKNOWN = "joined", "left", "unknown"


@dataclass(frozen=True)
class Channel:
    """봇이 참여 중인 대화 하나. 이름은 **표시용**이고 키는 ID 다."""

    channel_id: str
    name: str = ""
    is_private: bool = False


@dataclass(frozen=True)
class Action:
    """동기화가 하려는 일 하나. **무엇을 왜** 바꾸는지 들고 있다."""

    kind: str  # register · rejoin · leave · touch
    channel_id: str
    mode: str
    membership: str
    reason: str
    name: str = ""
    is_private: bool = False


@dataclass
class SyncPlan:
    actions: list[Action] = field(default_factory=list)

    @property
    def registered(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "register"]

    @property
    def stopped(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "leave"]

    @property
    def resumed(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "rejoin"]


def _is_dm(row: dict) -> bool:
    """DM·그룹 DM 인가. **타입으로 막고 값으로 한 번 더 막는다.**

    `types` 파라미터를 믿고 거르지 않으면, Slack 이 언젠가 기본값을 바꾸거나
    호출부 하나가 파라미터를 빠뜨렸을 때 개인 대화가 조용히 들어온다.
    """
    return bool(row.get("is_im") or row.get("is_mpim"))


def fetch_joined(client, *, page_size: int = PAGE_SIZE) -> list[Channel]:
    """봇이 **참여 중인** 공개·비공개 채널 전부. 페이지를 끝까지 넘긴다.

    한 페이지만 읽고 끝내면 채널이 늘어난 어느 날부터 뒤쪽 채널이 조용히 빠진다.
    그건 오류가 아니라 「그 방만 수집이 안 되는」 상태로 나타난다.
    """
    out: list[Channel] = []
    cursor = ""
    seen: set[str] = set()
    while True:
        response = client.users_conversations(
            types=CONVERSATION_TYPES,
            exclude_archived=True,
            limit=page_size,
            **({"cursor": cursor} if cursor else {}),
        )
        for row in response.get("channels") or []:
            channel_id = str(row.get("id") or "")
            if not channel_id or _is_dm(row) or channel_id in seen:
                continue
            seen.add(channel_id)
            out.append(Channel(
                channel_id=channel_id,
                name=str(row.get("name") or ""),
                is_private=bool(row.get("is_private")),
            ))
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            break
    return out


def plan_sync(joined: list[Channel], known: list[dict]) -> SyncPlan:
    """지금 참여 중인 채널과 DB 를 맞춘다. **쓰지는 않는다.**

    판단을 쓰기와 분리해 두면 「무엇이 바뀔까」 를 먼저 세어 볼 수 있다.
    """
    by_id = {str(row.get("channel_id")): row for row in known}
    joined_ids = {channel.channel_id for channel in joined}
    plan = SyncPlan()

    for channel in joined:
        row = by_id.get(channel.channel_id)
        if row is None:
            plan.actions.append(Action(
                kind="register", channel_id=channel.channel_id, mode=DEFAULT_MODE,
                membership=JOINED, name=channel.name, is_private=channel.is_private,
                reason="봇이 채널에 초대돼 자동 등록",
            ))
            continue
        if bool(row.get("operator_hold")):
            # 사람이 끈 채널이다. 재초대는 그 결정을 뒤집지 않는다.
            plan.actions.append(Action(
                kind="touch", channel_id=channel.channel_id,
                mode=str(row.get("mode") or "paused"), membership=JOINED,
                name=channel.name, is_private=channel.is_private,
                reason="참여 확인(사람이 중지해 둔 채널)",
            ))
            continue
        if (
            str(row.get("writer_owner") or "master") == "archiver"
            and (
                str(row.get("membership") or UNKNOWN) != JOINED
                or str(row.get("mode")) == "paused"
            )
        ):
            # An active channel that lost the app still belongs to the Archiver.
            # Moving it back to shadow would also move live-writer ownership to
            # Master, which requires an explicit cutover coordinate.
            plan.actions.append(Action(
                kind="rejoin-blocked",
                channel_id=channel.channel_id,
                mode="paused",
                membership=JOINED,
                name=channel.name,
                is_private=channel.is_private,
                reason="운영 writer 인수 좌표 확인 전 자동 재개 차단",
            ))
            continue
        if (
            str(row.get("membership") or UNKNOWN) != JOINED
            or str(row.get("mode")) in {"off", "paused"}
        ):
            plan.actions.append(Action(
                kind="rejoin", channel_id=channel.channel_id, mode=DEFAULT_MODE,
                membership=JOINED, name=channel.name, is_private=channel.is_private,
                reason="봇이 다시 참여해 수집 재개",
            ))
            continue
        plan.actions.append(Action(
            kind="touch", channel_id=channel.channel_id, mode=str(row.get("mode")),
            membership=JOINED, name=channel.name, is_private=channel.is_private,
            reason="참여 확인",
        ))

    for channel_id, row in sorted(by_id.items()):
        if channel_id in joined_ids:
            continue
        if str(row.get("membership") or UNKNOWN) == LEFT:
            continue
        # **운영 수집 중인 채널도 멈춘다.** 봇이 없는 채널에서 수집을 이어 갈 방법은
        # 없고, 목록에만 남겨 두면 권한 오류가 쌓인다.
        plan.actions.append(Action(
            kind="leave", channel_id=channel_id, mode="paused", membership=LEFT,
            name=str(row.get("channel_name") or ""),
            is_private=bool(row.get("is_private")),
            reason="봇이 채널에서 빠져 수집 중단",
        ))
    return plan


def apply_plan(repo, workspace: str, plan: SyncPlan, *, actor: str = "membership-sync") -> dict:
    """계획을 DB 에 쓴다. **모드를 직접 켜지 않는다** — `shadow` 까지다.

    `active` 로는 절대 자동 전환하지 않는다(오너 지시 12). 운영 원문의 주인을
    바꾸는 일은 사람이 사유와 함께 하는 것이고, 동기화 잡이 할 일이 아니다.
    """
    counts = {"registered": 0, "resumed": 0, "stopped": 0, "checked": 0}
    for action in plan.actions:
        if action.mode == "active" and action.kind in ("register", "rejoin"):
            # 있을 수 없는 조합이지만, 있으면 조용히 통과시키지 않는다.
            raise ValueError("동기화는 운영 수집으로 전환하지 않습니다.")
        repo.save_membership({
            "workspace": workspace,
            "channel_id": action.channel_id,
            "mode": action.mode,
            "membership": action.membership,
            "channel_name": action.name,
            "is_private": action.is_private,
            "updated_by": actor,
            "audit": action.kind != "touch",
            "reason": action.reason,
        })
        if action.kind == "register":
            counts["registered"] += 1
        elif action.kind == "rejoin":
            counts["resumed"] += 1
        elif action.kind == "leave":
            counts["stopped"] += 1
        else:
            counts["checked"] += 1
    return counts


def sync(client, repo, workspace: str, *, actor: str = "membership-sync") -> dict:
    """한 워크스페이스의 멤버십을 맞춘다. 시작 시점과 주기적으로 부른다."""
    joined = fetch_joined(client)
    plan = plan_sync(joined, repo.channels(workspace))
    result = apply_plan(repo, workspace, plan, actor=actor)
    log.info(
        "채널 멤버십 동기화 ws=%s 참여=%d 등록=%d 재개=%d 중단=%d",
        workspace, len(joined), result["registered"], result["resumed"],
        result["stopped"],
    )
    return {**result, "joined": len(joined)}


def channel_row(repo, workspace: str, channel_id: str) -> dict | None:
    """Return the current collection row for one channel."""
    return next(
        (
            row
            for row in repo.channels(workspace)
            if str(row.get("channel_id")) == channel_id
        ),
        None,
    )


def is_collectible(row: dict | None) -> bool:
    """Whether an event may be written by the Archiving Bot right now."""
    return bool(
        row
        and str(row.get("membership") or UNKNOWN) == JOINED
        and not bool(row.get("operator_hold"))
        and str(row.get("mode") or "") in {"shadow", "active"}
    )


def ensure_registered(client, repo, workspace: str, channel_id: str) -> bool:
    """이벤트가 **동기화보다 먼저** 온 채널을 받아들인다.

    초대 직후 첫 메시지가 다음 동기화 전에 올 수 있다. 그때 모르는 채널이라고
    버리면 그 대화는 영영 안 들어온다 — Slack 백필은 분당 1회라 되찾기 어렵다.

    그렇다고 이벤트만 보고 등록하지 않는다. **멤버십을 한 번 확인한다** — 확인
    없이 등록하면 초대 없는 채널이 이벤트 하나로 수집 대상이 된다.
    """
    existing = channel_row(repo, workspace, channel_id)
    if existing and str(existing.get("membership") or UNKNOWN) == JOINED:
        return False
    joined = fetch_joined(client)
    target = next((channel for channel in joined if channel.channel_id == channel_id), None)
    if target is None:
        log.info("참여하지 않은 채널의 이벤트라 등록하지 않는다 ch=%s", channel_id)
        return False
    # Event discovery is deliberately scoped to one channel. Passing every known
    # row to plan_sync() here would mark unrelated channels as left merely because
    # this event only asked us to verify one coordinate.
    plan = plan_sync([target], [existing] if existing else [])
    apply_plan(repo, workspace, plan, actor="event-discovery")
    return True
