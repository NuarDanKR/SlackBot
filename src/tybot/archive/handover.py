"""채널 하나를 Master 에서 Archiver 로 **넘기는 순서.**

설계: `docs/design/master-collection-handover.md` §5
문지기: `write_owner` · 모드 전이: `archiving_state.plan_mode_change`

## 왜 바로 넘기지 않나

`shadow → active` 로 한 번에 가면, Master 의 판정 캐시(`write_owner.CACHE_SECONDS`)
가 만료되기 전까지 **두 봇이 같은 메시지를 각자 쓴다.** 그 중복이 한 줄로 합쳐지는
것은 `writer.dedupe_line()` 덕분인데, 거기에는 조건이 붙는다 — 두 봇이 만드는 줄
문자열이 글자 단위로 같아야 하고, 그러려면 화자 이름이 같아야 하고, 그러려면 두
앱이 같은 `users:read` 결과를 봐야 한다.

조건이 맞아도 **겹친 구간은 경합**이다. 무엇이 언제 쓰였는지 나중에 재구성할 수
없고, 틀어졌을 때 범위를 모른다.

## 그래서 비우고 넘긴다

가운데에 `paused` 를 넣는다. 그 동안 **양쪽 다 운영 원문에 쓰지 않는다.**

```text
shadow ──pause──▶ paused ──settle──▶ active ──▶ 소급으로 빈 구간을 메운다
   ▲                  │
   └──────rollback────┘
```

겹치는 구간 대신 **비는 구간**이 생긴다. 둘 다 사고지만 성질이 다르다.

| | 겹침 | 빔 |
|---|---|---|
| 언제 생기나 | 경합. 정확한 범위를 모른다 | 사람이 누른 두 시각 사이. **범위를 안다** |
| 고치는 법 | 줄을 찾아 지운다 | 그 범위를 소급으로 읽는다 |
| 쓰는 주체 | 두 봇 | 소급 하나 — 그래서 화자 문자열이 저절로 같다 |

비는 구간은 범위를 알기 때문에 고칠 수 있다. 그것이 이 순서를 고른 이유다.

## 비는 동안에는 그림자도 멈춘다

`channel_membership.is_collectible()` 은 `shadow` 와 `active` 에서만 참이다. 즉
`paused` 동안에는 Archiver 의 그림자 수집도 멈춘다. 그 구간은 **어디에도 안 남고**
Slack 에만 있다.

그래서 이 모듈은 소급 작업이 만들어지기 전에는 인수를 **끝났다고 말하지 않는다**
(`Stage.ACTIVE` 가 아니라 `Step.action == "backfill"` 로 남는다). 소급을 안 걸면
그 구간은 Slack 의 보존 기간이 지나는 날 사라진다.

## 여기서 하지 않는 것

표를 고치지 않고 Slack 을 읽지 않는다. **다음에 무엇을 해야 하나**를 값으로
돌려줄 뿐이고, 실제 전환은 콘솔(`archiving_admin.set_channel_mode`)이, 소급은
`supervisor_admin.request_backfill` 이 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .archiving_state import ChannelMode, ChannelState, WriterOwner
from .write_owner import CACHE_SECONDS

#: `paused` 로 내린 뒤 **이만큼 기다렸다가** `active` 로 올린다.
#:
#: Master 는 판정을 `CACHE_SECONDS` 만큼 들고 있으므로, 누른 직후에는 아직 쓰고
#: 있을 수 있다. 그 사이에 Archiver 를 올리면 결국 겹친다 — 비우려고 한 단계가
#: 아무 일도 안 한 셈이 된다. 두 배로 두는 이유는 시계가 정확히 맞지 않기 때문이다.
DRAIN_SETTLE_SECONDS = CACHE_SECONDS * 2

#: 소급 범위를 `paused` 시각보다 **이만큼 앞에서** 시작한다.
#:
#: Master 가 언제 마지막으로 썼는지 정확히 모른다(캐시 때문에 누른 시각보다 뒤다).
#: 앞에서 시작하면 이미 쓴 구간을 다시 읽는데, 그건 `dedupe_line` 이 거른다.
#: 반대로 뒤에서 시작하면 **한 건도 못 찾는 구간**이 남고 그건 안 보인다.
BACKFILL_OVERLAP_SECONDS = 120.0


class Stage(StrEnum):
    """인수가 지금 어디까지 왔나. `archive_channel_mode` 의 모드에서 읽는다."""

    #: 아직 시작하지 않았다. Master 가 운영 원문을 쓴다.
    BEFORE = "before"
    #: 비우는 중. **양쪽 다 운영 원문에 쓰지 않는다.**
    DRAINING = "draining"
    #: Archiver 가 운영 writer 다. 빈 구간은 아직 안 메웠을 수 있다.
    HANDED_OVER = "handed-over"
    #: 이 채널은 인수 대상이 아니다(`off`).
    NOT_IN_SCOPE = "not-in-scope"


def stage_of(state: ChannelState | None) -> Stage:
    """표 한 행이 말하는 단계. **모드 하나에서 읽는다** — 값을 따로 두지 않는다.

    별도 칼럼을 만들면 모드와 단계가 어긋나는 상태가 생기고, 그때 화면은 어느
    쪽을 보여 줄지 정할 수 없다.
    """
    if state is None:
        return Stage.BEFORE
    if state.mode == ChannelMode.PAUSED:
        return Stage.DRAINING
    if state.mode == ChannelMode.ACTIVE:
        return Stage.HANDED_OVER
    if state.mode == ChannelMode.OFF:
        return Stage.NOT_IN_SCOPE
    return Stage.BEFORE


@dataclass(frozen=True)
class Check:
    """인수 전에 확인해야 하는 것 하나. **사람이 읽을 문장을 들고 있다.**"""

    name: str
    met: bool
    detail: str


@dataclass(frozen=True)
class Preconditions:
    """인수를 시작해도 되나. 값으로 받는다 — 여기서 확인하지 않는다.

    확인은 각각 다른 곳이 한다(콘솔 대조, 격리 DB 검증, Slack 조회). 이 모듈이
    직접 확인하면 시험이 그 셋을 전부 흉내 내야 하고, 흉내는 진짜와 달라진다.
    """

    #: 같은 기간의 그림자 수집본과 운영 원문을 대조해 차이가 없다.
    shadow_compared: bool = False
    #: 두 앱이 **같은 화자 문자열**을 만든다(둘 다 `users:read` 가 되고 결과가 같다).
    #:
    #: 이것이 깨지면 소급이 메운 구간에서 같은 사람이 두 이름으로 남는다.
    #: 오류가 아니라 **한 사람이 두 사람처럼 보이는** 모양이라 눈에 잘 안 띈다.
    speaker_parity: bool = False
    #: 전역 스위치 `archiver_writes_live` 가 켜져 있다.
    live_switch: bool = False
    #: Archiver 가 그 채널에 실제로 참여해 있다. 초대가 없으면 인수 후 아무도 안 쓴다.
    archiver_joined: bool = False
    #: release gate(격리 DB 검증)가 열려 있다.
    schema_gate_open: bool = False

    def checks(self) -> list[Check]:
        return [
            Check("shadow_compared", self.shadow_compared,
                  "같은 기간의 그림자 수집본과 운영 원문을 대조했습니다."),
            Check("speaker_parity", self.speaker_parity,
                  "두 앱이 같은 화자 이름을 만듭니다. 다르면 같은 사람이 두 이름으로 남습니다."),
            Check("live_switch", self.live_switch,
                  "전역 스위치 archiver_writes_live 가 켜져 있습니다."),
            Check("archiver_joined", self.archiver_joined,
                  "Archiving Bot 이 그 채널에 참여해 있습니다."),
            Check("schema_gate_open", self.schema_gate_open,
                  "격리 DB 검증(release gate)이 통과했습니다."),
        ]

    def unmet(self) -> list[Check]:
        return [check for check in self.checks() if not check.met]


@dataclass(frozen=True)
class Step:
    """다음에 할 일 하나. **막혔으면 왜 막혔는지 들고 있다.**"""

    action: str
    reason: str
    blocked: bool = False
    #: `backfill` 일 때만 찬다. `(from_ts, to_ts)` — `to_ts` 가 빈 값이면 지금까지.
    window: tuple[str, str] | None = None

    def __bool__(self) -> bool:
        return not self.blocked


def backfill_window(
    paused_at_ts: str, *, overlap_seconds: float = BACKFILL_OVERLAP_SECONDS,
) -> tuple[str, str]:
    """빈 구간을 메울 소급 범위. **넉넉하게 잡고 멱등에 맡긴다.**

    시작은 `paused` 를 누른 시각보다 `overlap_seconds` 앞이다. Master 가 캐시 때문에
    그 뒤로도 잠깐 더 썼을 수 있는데, 다시 읽어도 `dedupe_line` 이 거른다.

    끝은 **비워 둔다**(지금까지). 끝을 박으면 인수 직후 Archiver 가 실시간으로 쓰기
    시작한 지점과 사이가 벌어질 수 있고, 그 틈은 아무 오류도 내지 않는다. 끝까지
    읽어서 이미 쓴 것을 다시 거르는 쪽이 싸다.
    """
    try:
        start = max(0.0, float(paused_at_ts) - overlap_seconds)
    except (TypeError, ValueError):
        # 좌표를 못 읽으면 범위를 **좁히지 않는다.** 좁은 범위로 「메웠다」 고
        # 말하는 것이 못 메운 것보다 나쁘다.
        return "", ""
    return f"{start:.6f}", ""


def plan(
    state: ChannelState | None,
    *,
    ready: Preconditions,
    drained_seconds: float | None = None,
    paused_at_ts: str = "",
    backfill_requested: bool = False,
    settle_seconds: float = DRAIN_SETTLE_SECONDS,
) -> Step:
    """다음에 무엇을 해야 하나. **아무것도 바꾸지 않는다.**

    `drained_seconds` 는 `paused` 로 내린 뒤 흐른 시간이다. 모르면 `None` 을 넘긴다 —
    그때는 기다리라고 답한다. 「모르니까 됐다고 치자」 가 곧 겹침이다.
    """
    stage = stage_of(state)

    if stage == Stage.NOT_IN_SCOPE:
        return Step("none", "꺼진 채널입니다. 먼저 shadow 로 켜서 대조부터 하세요.",
                    blocked=True)

    if stage == Stage.HANDED_OVER:
        if not backfill_requested:
            return Step(
                "backfill",
                "인수는 끝났지만 비운 구간을 아직 안 메웠습니다. 그 구간은 그림자에도"
                " 없고 Slack 에만 있습니다.",
                window=backfill_window(paused_at_ts),
            )
        return Step("done", "인수가 끝났고 빈 구간도 메웠습니다.")

    unmet = ready.unmet()
    if unmet:
        return Step(
            "none",
            "인수 전 확인이 남았습니다: " + " · ".join(check.name for check in unmet),
            blocked=True,
        )

    if stage == Stage.BEFORE:
        if state is not None and state.writer_owner != WriterOwner.MASTER:
            # 표가 모순이다. 추측해서 넘기면 그 추측이 조용히 틀린다.
            return Step(
                "none",
                f"{state.mode} 인데 운영 writer 가 {state.writer_owner} 입니다."
                " 표를 먼저 바로잡으세요.",
                blocked=True,
            )
        return Step(
            "pause",
            "먼저 비웁니다. 이 동안 양쪽 다 운영 원문에 쓰지 않습니다.",
        )

    # DRAINING
    if drained_seconds is None:
        return Step(
            "wait",
            "비운 지 얼마나 됐는지 모릅니다. 확인 전에는 올리지 않습니다.",
            blocked=True,
        )
    if drained_seconds < settle_seconds:
        remaining = settle_seconds - drained_seconds
        return Step(
            "wait",
            f"Master 의 판정 캐시가 아직 남아 있을 수 있습니다. {remaining:.0f}초 더"
            " 기다리세요 — 지금 올리면 비우려던 단계가 겹침이 됩니다.",
        )
    return Step(
        "activate",
        "비우기가 끝났습니다. 인수 좌표와 함께 active 로 올리고, 이어서 빈 구간을"
        " 소급으로 메우세요.",
    )
