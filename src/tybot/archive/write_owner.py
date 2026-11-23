"""이 채널의 **운영 원문을 지금 누가 쓰는가.**

설계: `docs/design/master-collection-handover.md`
순수 전이: `archiving_state.plan_mode_change` · `archiving_state.owns_write`
표: `archive_channel_mode` (`deploy/sql/archiving_schema.sql`)

## 무엇이 문제였나

`owns_write()` 는 예전부터 있었다. 그런데 **부르는 곳이 시험뿐이었다.** 실제
수집 경로 셋(Master 실시간·Master 취합·Master 정시 잡)은 표를 읽지 않고 그냥
썼다. 그래서 콘솔에서 채널을 `active` 로 넘겨도 Master 는 계속 쓴다 — 아무
오류도 나지 않고, 같은 채널에 두 봇이 쓰는 상태가 된다.

이 모듈은 그 판정을 **한 자리**에 두고, 수집 경로가 쓰기 전에 지나게 한다.

## 둘이 동시에 쓰지 않는 이유

「주인이 하나」 라는 값만으로는 부족하다. 표를 못 읽는 순간이 오기 때문이다.
그때 두 봇이 각자 「아마 내 것」 이라고 판단하면 둘 다 쓴다.

그래서 **모르는 상태에서는 둘 다 쓰지 않는다**(B-99, 2026-11-20 — fail-closed).

| 상황 | Master | Archiver |
|---|---|---|
| 표에 행이 없다 | **안 쓴다** | 안 쓴다 |
| DB 를 못 읽는다 | **안 쓴다**(degraded) | 안 쓴다 |
| `off` · `shadow` | 쓴다 | 안 쓴다(그림자에만) |
| `active` | 안 쓴다 | 쓴다 |
| `paused` | 안 쓴다 | 안 쓴다 |

예전에는 행이 없거나 DB 를 못 읽으면 Master 가 썼다(「전환 전 상태가 그것」).
2026-11-18 에 전 채널이 Archiver 소유가 된 뒤로 그 기본값은 **옛 배치
`workspaces/<ws>/channels` 를 운영 루트에 다시 만드는 문**이 됐고, Archiver 는 그
디렉터리를 보면 기동을 거부한다(2026-11-20 장애). 놓친 대화는 Archiver 소급이
메우지만, 운영 루트에 생긴 옛 배치는 다음 재시작에서 수집 전체를 멈춘다.

Master 는 표에 **자기 몫이라는 긍정 응답**이 있을 때만 쓴다. Archiver 도 같다 —
그래서 어느 순간에도 둘이 동시에 참이 되지 않는다.

로그에는 사람이 읽는 `reason` 과 함께 **코드**(`Decision.code`)를 남긴다. 코드는
채널 이름·메시지 같은 원문을 담지 않는 고정 문자열이다.

## cutover_ts 로 메시지를 가르지 않는다 — 단, 미래 좌표는 「아직 아님」 이다

인수 좌표는 **대조용 기록**이다. 런타임에서 메시지마다 ts 로 주인을 가르면 같은
채널에 대해 두 가지 판정 기준이 생기고, 경계에서 어느 쪽도 안 쓰는 구간이 만들어
지기 쉽다. 판정은 채널 단위 하나로 두고, 인수 전후 대조는 좌표로 나중에 한다.

예외 하나(2026-11-18, shadow 폐기). 초대·재초대로 Master 몫이 Archiver 로 넘어갈 때
DB 는 좌표를 **등록 시각 + 정착 시간**(미래)으로 박는다. 그 시각이 오기 전에는
Archiver 가 채널 단위로 **쓰지 않는다** — Master 가 캐시(`CACHE_SECONDS`) 때문에 아직
쓰고 있을 수 있어서다. 메시지 ts 가 아니라 **지금 시각**과 비교하므로 판정 기준은
여전히 하나다. 좌표가 지나면 평소대로 채널 단위로 판정한다.

## 여기서 하지 않는 것

쓰기를 하지 않는다. 표를 고치지 않는다. 모드를 바꾸지 않는다 — 그것은 콘솔
(`archiving_admin.set_channel_mode`)이 한다.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from .archiving_state import ChannelMode, ChannelState, WriterOwner, owns_write

log = logging.getLogger("tybot.archive.write_owner")

#: 판정을 얼마나 들고 있나(초). 메시지마다 DB 를 물으면 실시간 수집이 DB 응답
#: 시간에 묶인다. 반대로 오래 들고 있으면 인수 직후에도 옛 주인이 계속 쓴다.
CACHE_SECONDS = 30.0


@dataclass(frozen=True)
class Decision:
    """써도 되나, 그리고 **무엇을 근거로.**

    `allowed` 만 돌려주지 않는 이유: 거절이 「인수됐다」 인지 「DB 를 못 읽었다」
    인지에 따라 사람이 할 일이 다르다. 로그에 사유가 없으면 둘이 같아 보인다.
    """

    allowed: bool
    actor: WriterOwner
    reason: str
    mode: ChannelMode = ChannelMode.OFF
    owner: WriterOwner = WriterOwner.MASTER
    cutover_ts: str = ""
    #: 표를 못 읽고 기본값으로 판단했나. 이 값이 오래 참이면 인수가 반영되지 않는다.
    degraded: bool = False
    #: 원문이 없는 고정 사유 코드. 로그·집계는 이 값으로 가른다.
    code: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def decide(
    state: ChannelState | None,
    actor: WriterOwner,
    *,
    archiver_flag: bool = False,
    degraded: bool = False,
    now: float | None = None,
) -> Decision:
    """순수 판정. **표를 읽지 않는다** — 읽은 결과를 받는다.

    `state` 가 `None` 이면 표에 행이 없거나(또는 읽을 수 없는 행이거나) 표를 못
    읽은 것이다. **누구도 쓰지 않는다**(B-99 fail-closed) — 모르는 채널에 Master 가
    쓰면 운영 루트에 옛 배치가 생기고 Archiver 가 기동을 거부한다.

    Archiver 는 전역 스위치(`archiver_writes_live`)까지 켜져야 쓴다. 채널만 보면
    사고 때 한 번에 내릴 차단기가 없고, 전역만 보면 채널 하나 문제가 전체를 멈춘다.
    """
    if state is None:
        if degraded:
            return Decision(
                allowed=False, actor=actor,
                reason="채널 주인 표를 읽지 못해 운영 원문에 쓰지 않습니다(fail-closed).",
                degraded=True, code="owner-lookup-failed",
            )
        return Decision(
            allowed=False, actor=actor,
            reason="채널 주인 기록이 없어 운영 원문에 쓰지 않습니다(fail-closed).",
            code="no-owner-row",
        )

    mine = owns_write(state, actor)
    if actor == WriterOwner.ARCHIVER and mine and not archiver_flag:
        return Decision(
            allowed=False, actor=actor,
            reason="채널은 인수됐지만 전역 스위치(archiver_writes_live)가 꺼져 있습니다.",
            mode=state.mode, owner=state.writer_owner, cutover_ts=state.cutover_ts,
            code="archiver-switch-off",
        )
    if actor == WriterOwner.ARCHIVER and mine:
        settle_until = _cutover_epoch(state.cutover_ts)
        current = time.time() if now is None else now
        if settle_until is not None and current < settle_until:
            # Master 가 아직 캐시로 쓰고 있을 수 있다. 쓰면 두 writer 다.
            return Decision(
                allowed=False, actor=actor,
                reason=(
                    f"인수 정착 대기 중입니다({state.cutover_ts} 이후 Archiver 가 씁니다)."
                ),
                mode=state.mode, owner=state.writer_owner, cutover_ts=state.cutover_ts,
                code="cutover-settling",
            )
    if mine:
        reason = f"{state.mode} 채널의 운영 writer 입니다."
        code = "owner"
    elif state.mode == ChannelMode.PAUSED:
        reason = "일시 중지된 채널이라 양쪽 모두 운영 원문에 쓰지 않습니다."
        code = "paused"
    else:
        reason = (
            f"{state.cutover_ts or '인수 좌표 없음'} 이후 이 채널의 운영 writer 는"
            f" {state.writer_owner} 입니다."
        )
        code = "not-owner"
    return Decision(
        allowed=mine, actor=actor, reason=reason,
        mode=state.mode, owner=state.writer_owner, cutover_ts=state.cutover_ts,
        code=code,
    )


def _cutover_epoch(cutover_ts: str) -> float | None:
    """Slack ts 형식 좌표를 초로. 읽을 수 없으면 None(정착 판정을 하지 않는다)."""
    try:
        return float(cutover_ts) if cutover_ts else None
    except ValueError:
        return None


def state_from_row(workspace: str, channel_id: str, row: dict | None) -> ChannelState | None:
    """표 한 행을 판정이 아는 값으로. **모르는 값은 통과시키지 않는다.**

    `mode` 나 `writer_owner` 에 모르는 문자열이 있으면 `None` 을 돌려준다. 추측해서
    읽으면 「알 수 없는 모드라서 아무나 쓴다」 가 되고, 그건 두 writer 를 부른다.
    """
    if not row:
        return None
    try:
        return ChannelState(
            workspace=workspace,
            channel_id=channel_id,
            mode=ChannelMode(str(row["mode"])),
            writer_owner=WriterOwner(str(row["writer_owner"])),
            cutover_ts=str(row.get("cutover_ts") or ""),
        )
    except (KeyError, ValueError) as exc:
        # 행 값을 로그에 그대로 싣지 않는다 — 예외 종류만.
        log.warning(
            "채널 모드 행을 읽지 못해 쓰지 않는 쪽으로 판단한다 code=owner-row-invalid"
            " ws=%s ch=%s err=%s",
            workspace, channel_id, type(exc).__name__,
        )
        return None


class OwnerLookup:
    """표를 읽어 판정한다. **읽지 못해도 예외를 올리지 않는다.**

    대신 `degraded` 를 세우고 **쓰지 않는 쪽**으로 판단한다(B-99). 예전에는 Master 가
    썼는데, 그 쓰기가 운영 루트에 옛 배치를 만들어 Archiver 를 멈췄다. 놓친 구간은
    Archiver 가 소급으로 메운다 — 멈춘 Archiver 는 아무것도 메우지 못한다.
    """

    def __init__(self, workspace: str, *, connect=None, cache_seconds: float = CACHE_SECONDS,
                 clock=time.monotonic) -> None:
        self.workspace = workspace
        self._connect = connect
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, ChannelState | None, bool]] = {}

    def _open(self):
        if self._connect is not None:
            return self._connect()
        from .. import db

        return db.connect()

    def _fetch(self, channel_id: str) -> tuple[ChannelState | None, bool]:
        """`(상태, 못 읽었나)`. 못 읽은 것과 행이 없는 것을 **구분해서** 돌려준다."""
        try:
            with self._open() as conn:
                if conn is None:
                    return None, True
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT mode, writer_owner, cutover_ts
                          FROM archive_channel_mode
                         WHERE workspace = %s AND channel_id = %s
                        """,
                        (self.workspace, channel_id),
                    )
                    row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - DB 장애로 수집이 죽지 않는다
            # 예외 본문은 SQL 값·접속 정보를 담을 수 있어 종류만 남긴다.
            log.warning(
                "채널 주인 조회 실패라 쓰지 않는 쪽으로 판단한다 code=owner-lookup-failed"
                " ws=%s ch=%s err=%s",
                self.workspace, channel_id, type(exc).__name__,
            )
            return None, True
        return state_from_row(self.workspace, channel_id, row), False

    def state(self, channel_id: str) -> tuple[ChannelState | None, bool]:
        now = self._clock()
        cached = self._cache.get(channel_id)
        if cached and now - cached[0] < self._cache_seconds:
            return cached[1], cached[2]
        state, degraded = self._fetch(channel_id)
        self._cache[channel_id] = (now, state, degraded)
        return state, degraded

    def forget(self, channel_id: str = "") -> None:
        """다음 판정에서 표를 다시 읽게 한다. 인수 직후 기다리지 않아도 되게."""
        if channel_id:
            self._cache.pop(channel_id, None)
        else:
            self._cache.clear()

    def decide(self, channel_id: str, actor: WriterOwner, *,
               archiver_flag: bool = False) -> Decision:
        state, degraded = self.state(channel_id)
        return decide(state, actor, archiver_flag=archiver_flag, degraded=degraded)

    # -- 부르는 쪽이 읽기 쉬운 이름 -------------------------------------------
    def master_may_write(self, channel_id: str) -> Decision:
        if os.getenv("TYBOT_MASTER_CHANNEL_WRITES_ENABLED", "1").strip().lower() in {
            "0", "false", "no", "off",
        }:
            return Decision(
                allowed=False, actor=WriterOwner.MASTER,
                reason="Master channel archive writes disabled by operator",
                code="operator-disabled",
            )
        return self.decide(channel_id, WriterOwner.MASTER)

    def archiver_may_write_live(self, channel_id: str, *, archiver_flag: bool) -> Decision:
        return self.decide(channel_id, WriterOwner.ARCHIVER, archiver_flag=archiver_flag)

    def archiver_live_flag(self) -> bool:
        """Read the global kill switch without a row lock or write privilege."""
        try:
            with self._open() as conn:
                if conn is None:
                    return False
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT enabled FROM archive_feature_flag
                         WHERE name = %s AND scope = 'global' AND scope_key = ''
                        """,
                        ("archiver_writes_live",),
                    )
                    row = cur.fetchone()
            return bool(row and row["enabled"] is True)
        except Exception as exc:  # noqa: BLE001 - DB failure must close the live gate
            log.warning("Archiver live switch lookup failed ws=%s: %s", self.workspace, exc)
            return False
