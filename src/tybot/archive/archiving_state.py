"""Archiving Bot 의 상태 전이 — **한 자리에서 판정한다.**

결정: 2026-09-25 오너 확정. 스키마: `deploy/sql/archiving_schema.sql`.

## 왜 코드에도 두는가

스키마의 `CHECK` 는 **저장된 값**이 모순되지 않게 한다. 그런데 「received 에서
바로 ready 로 뛰었다」 는 두 값 다 합법이라 `CHECK` 로는 못 잡는다. 전이는
**두 상태의 관계**라서 한 행만 보고는 판정할 수 없다.

그래서 둘 다 둔다. 표는 결과를, 여기는 경로를 지킨다.

## 이 파일이 막는 것

1. **첨부가 안 끝났는데 「검색된다」 고 말하는 것** — 오너 결정 §6.
   `ready` 로 가려면 첨부 수가 맞아야 한다
2. **두 writer 가 같은 채널에 동시에 쓰는 것** — 인수는 `shadow → active` 한 길뿐이고
   그때 `writer_owner` 와 `cutover_ts` 가 같이 움직인다
3. **끝난 것을 되돌리는 것** — `ready` 다음은 없다. 새 사실이 생기면 그건 새
   메시지이거나 새 revision 이다
4. **운영값 없이 production 으로 가는 것** — 보존 정책이 비어 있으면 막는다

거부는 **예외가 아니라 값**이다. 수집 한 건이 막혔다고 프로세스가 죽으면 안 되고,
왜 막혔는지는 기록에 남아야 한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# 채널 모드와 writer 주인
# ---------------------------------------------------------------------------


class ChannelMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"
    PAUSED = "paused"


class WriterOwner(StrEnum):
    MASTER = "master"
    ARCHIVER = "archiver"


# 갈 수 있는 길. **`off → active` 가 없는 것이 요점이다.**
#
# 그림자를 건너뛰면 「누락 0·권한 유출 0·첨부 손실 0」 을 확인할 기회가 없다
# (분리 설계 §4-C). 급할 때 건너뛰고 싶어지는 단계라 코드로 막는다.
_MODE_EDGES: dict[ChannelMode, frozenset[ChannelMode]] = {
    ChannelMode.OFF: frozenset({ChannelMode.SHADOW}),
    ChannelMode.SHADOW: frozenset({ChannelMode.OFF, ChannelMode.ACTIVE, ChannelMode.PAUSED}),
    ChannelMode.ACTIVE: frozenset({ChannelMode.PAUSED}),
    # 멈춘 채널은 원래 모드로만 돌아간다. paused 에서 off 로 가면 그 채널이
    # 인수됐다는 사실이 사라지고, 다음 사람이 다시 shadow 부터 시작한다.
    ChannelMode.PAUSED: frozenset({ChannelMode.SHADOW, ChannelMode.ACTIVE}),
}


class TransitionRefused(ValueError):
    """갈 수 없는 길. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class ChannelState:
    workspace: str
    channel_id: str
    mode: ChannelMode
    writer_owner: WriterOwner
    cutover_ts: str = ""


def plan_mode_change(
    current: ChannelState, target: ChannelMode, *, cutover_ts: str = ""
) -> ChannelState:
    """모드를 바꾼 뒤의 상태. 못 바꾸면 `TransitionRefused`.

    `writer_owner` 를 호출부가 고르게 두지 않는다. 모드와 주인이 따로 움직이면
    「active 인데 주인은 master」 가 만들어지고, 그 상태에서는 두 writer 가
    같은 파일에 쓴다. 줄이 섞이고 `doc_count` 갱신이 유실된다.
    """
    if target == current.mode:
        return current
    allowed = _MODE_EDGES[current.mode]
    if target not in allowed:
        raise TransitionRefused(
            f"{current.mode} → {target} 로는 갈 수 없습니다. "
            f"갈 수 있는 곳: {', '.join(sorted(allowed)) or '없음'}"
        )

    if target == ChannelMode.ACTIVE:
        # 인수 시점을 **반드시** 받는다. 없으면 「언제부터 이 봇 몫인가」 를
        # 나중에 아무도 모르고, 인수 전후 중복·누락을 대조할 수 없다.
        ts = cutover_ts or current.cutover_ts
        if not ts:
            raise TransitionRefused(
                "active 로 바꾸려면 인수 좌표(cutover_ts)가 필요합니다. "
                "이 ts 이후가 아카이빙 봇 몫입니다"
            )
        if current.cutover_ts and ts < current.cutover_ts:
            # 되돌리면 이미 넘긴 구간을 다시 넘기게 된다 — 그 구간은 두 writer 가
            # 다 썼다고 생각한다.
            raise TransitionRefused(
                f"인수 좌표는 뒤로 갈 수 없습니다: {current.cutover_ts} → {ts}"
            )
        return ChannelState(
            current.workspace, current.channel_id, target, WriterOwner.ARCHIVER, ts
        )

    if target == ChannelMode.SHADOW:
        # 첫 파일럿은 master 가 운영 원문을 계속 쓰고 archiver 는 그림자에만 쓴다.
        # active 이후 shadow 로 되돌리는 것은 운영 writer 를 master 로 넘기는
        # 역인수다. 새 좌표 없이 소유권만 바꾸면 그 경계에서 중복·누락이 생긴다.
        if current.writer_owner == WriterOwner.ARCHIVER:
            if not cutover_ts:
                raise TransitionRefused(
                    "active 이후 shadow 로 돌아가려면 역인수 좌표(cutover_ts)가 "
                    "필요합니다. 이 ts 이후가 master 몫입니다"
                )
            if current.cutover_ts and cutover_ts < current.cutover_ts:
                raise TransitionRefused(
                    f"역인수 좌표는 기존 인수 좌표보다 앞설 수 없습니다: "
                    f"{current.cutover_ts} → {cutover_ts}"
                )
            return ChannelState(
                current.workspace,
                current.channel_id,
                target,
                WriterOwner.MASTER,
                cutover_ts,
            )
        return ChannelState(
            current.workspace,
            current.channel_id,
            target,
            WriterOwner.MASTER,
            current.cutover_ts,
        )

    if target == ChannelMode.OFF:
        # 꺼진 채널의 주인은 master 로 돌아간다. archiver 가 주인인 채로 꺼지면
        # **아무도 안 쓰는 구간**이 생기고, 그건 누락인데 오류가 안 난다.
        return ChannelState(
            current.workspace, current.channel_id, target, WriterOwner.MASTER, ""
        )

    # paused 는 이전 주인을 유지해 어느 모드로 재개할지 판정한다. 다만 paused
    # 동안에는 어느 쪽도 운영 원문을 쓰지 않는다(`owns_write`).
    return ChannelState(
        current.workspace, current.channel_id, target,
        current.writer_owner, current.cutover_ts,
    )


def may_write_live(state: ChannelState, *, archiver_flag: bool) -> bool:
    """아카이빙 봇이 이 채널의 **운영** 원문에 써도 되는가.

    두 개가 다 켜져야 한다 — 전역 스위치(`archiver_writes_live`)와 채널 주인.
    전역만 보면 채널 하나 문제가 전체를 멈추게 하고, 채널만 보면 사고 때
    한 번에 내릴 차단기가 없다.
    """
    return (
        archiver_flag
        and state.mode == ChannelMode.ACTIVE
        and state.writer_owner == WriterOwner.ARCHIVER
    )


def owns_write(state: ChannelState, actor: WriterOwner) -> bool:
    """이 actor 가 지금 이 채널을 쓸 주인인가. **둘이 동시에 참이 되지 않는다.**"""
    if state.mode == ChannelMode.PAUSED:
        return False
    if state.mode in (ChannelMode.OFF, ChannelMode.SHADOW):
        return actor == WriterOwner.MASTER
    return actor == WriterOwner.ARCHIVER


# ---------------------------------------------------------------------------
# 수집 상태 (ACK)
# ---------------------------------------------------------------------------


class IngestState(StrEnum):
    RECEIVED = "received"
    RAW_WRITTEN = "raw_written"
    ATTACHMENT_PENDING = "attachment_pending"
    READY = "ready"
    PARTIAL = "partial"
    REFUSED = "refused"
    FAILED = "failed"


#: 더 갈 곳이 없는 상태. 여기 도달하면 같은 메시지로는 끝이다.
TERMINAL: frozenset[IngestState] = frozenset({
    IngestState.READY, IngestState.REFUSED, IngestState.FAILED,
})

_INGEST_EDGES: dict[IngestState, frozenset[IngestState]] = {
    IngestState.RECEIVED: frozenset({
        IngestState.RAW_WRITTEN, IngestState.REFUSED, IngestState.FAILED,
    }),
    IngestState.RAW_WRITTEN: frozenset({
        IngestState.ATTACHMENT_PENDING, IngestState.READY,
        IngestState.PARTIAL, IngestState.FAILED,
    }),
    IngestState.ATTACHMENT_PENDING: frozenset({
        IngestState.READY, IngestState.PARTIAL, IngestState.FAILED,
    }),
    # 일부만 된 것은 나중에 다 될 수 있다. 되돌아갈 곳은 없다.
    IngestState.PARTIAL: frozenset({IngestState.READY, IngestState.FAILED}),
    IngestState.READY: frozenset(),
    IngestState.REFUSED: frozenset(),
    IngestState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class IngestProgress:
    state: IngestState
    attachment_total: int = 0
    attachment_ready: int = 0

    @property
    def attachments_done(self) -> bool:
        return self.attachment_ready >= self.attachment_total


def plan_ingest_change(current: IngestProgress, target: IngestState) -> IngestProgress:
    """다음 수집 상태. 못 가면 `TransitionRefused`.

    **`ready` 는 첨부가 다 끝나야 한다.** 이것이 오너 결정 §6 의 「attachment
    ready 전에는 검색 가능 또는 변환 완료라고 말하지 않는다」 를 코드에서 지키는
    자리다. 표의 `CHECK` 도 같은 것을 막지만, 표는 **저장될 때**만 본다 —
    사람에게 말하기 전에 여기서 막는 편이 빠르다.
    """
    if target == current.state:
        return current
    if current.state in TERMINAL:
        raise TransitionRefused(
            f"{current.state} 는 끝난 상태라 {target} 로 바꿀 수 없습니다. "
            "새 사실이 생겼다면 그건 새 메시지이거나 새 revision 입니다"
        )
    allowed = _INGEST_EDGES[current.state]
    if target not in allowed:
        raise TransitionRefused(
            f"{current.state} → {target} 로는 갈 수 없습니다. "
            f"갈 수 있는 곳: {', '.join(sorted(allowed)) or '없음'}"
        )
    if target == IngestState.READY and not current.attachments_done:
        raise TransitionRefused(
            f"첨부 {current.attachment_ready}/{current.attachment_total} 이라 "
            "ready 가 아닙니다. 검색 가능하다고 말하지 않습니다"
        )
    return IngestProgress(target, current.attachment_total, current.attachment_ready)


def searchable_claim(progress: IngestProgress, *, require_ack: bool) -> str:
    """사람에게 **뭐라고 말해도 되는가.**

    문장을 여기서 만드는 이유: 호출부마다 문구를 쓰면 한 군데가 「올렸습니다」 를
    「검색됩니다」 로 적고, 그 경로만 거짓말한다. 그리고 그게 제일 안 들킨다.
    """
    if not require_ack:
        # 스위치가 꺼져 있어도 **없는 사실을 만들지는 않는다.** 원문이 들어갔다는
        # 것까지만 말한다.
        return "원문은 기록했습니다" if progress.state != IngestState.RECEIVED else "받았습니다"
    match progress.state:
        case IngestState.READY:
            return "기록했고 검색할 수 있습니다"
        case IngestState.ATTACHMENT_PENDING | IngestState.PARTIAL:
            done, total = progress.attachment_ready, progress.attachment_total
            return f"원문은 기록했습니다. 첨부 변환 {done}/{total} — 아직 검색에는 안 잡힙니다"
        case IngestState.RAW_WRITTEN:
            return "원문은 기록했습니다"
        case IngestState.REFUSED:
            return "수집 규칙에 걸려 기록하지 않았습니다"
        case IngestState.FAILED:
            return "기록하지 못했습니다"
        case _:
            return "받았습니다. 아직 기록 전입니다"


# ---------------------------------------------------------------------------
# 메시지 revision
# ---------------------------------------------------------------------------


class RevisionKind(StrEnum):
    CREATE = "create"
    CHANGE = "change"
    DELETE = "delete"
    REDACT = "redact"


#: 검색이 **보지 않는** 종류. 이걸 한 자리에 두는 이유는, 조회하는 쪽마다
#: 목록을 쓰면 한 군데가 `redact` 를 빼먹고 지워진 것을 보여 주기 때문이다.
HIDDEN_FROM_SEARCH: frozenset[RevisionKind] = frozenset({
    RevisionKind.DELETE, RevisionKind.REDACT,
})


@dataclass(frozen=True)
class Revision:
    kind: RevisionKind
    revision_no: int
    body_sha256: str = ""
    reason_code: str = ""


def next_revision(history: list[Revision], kind: RevisionKind, **fields) -> Revision:
    """다음 revision 을 만든다. 앞의 것을 **고치지 않는다.**

    원문은 안 고친다(절대 원칙 1). 그런데 사람이 고친 문장도 원문이다. 둘을 함께
    지키는 길은 쌓는 것뿐이다 — 검색은 최신만 보고, 감사는 전부 본다.
    """
    if not history:
        if kind != RevisionKind.CREATE:
            raise TransitionRefused(
                f"첫 revision 은 create 여야 합니다({kind} 를 받았습니다). "
                "원본 없이 수정만 쌓으면 무엇이 무엇으로 바뀌었는지 말할 수 없습니다"
            )
        return Revision(kind, 1, **fields)

    last = history[-1]
    if last.kind == RevisionKind.REDACT:
        # 지운 본문 위에 다시 쌓으면 삭제가 무의미해진다.
        raise TransitionRefused("redact 된 메시지에는 더 쌓지 않습니다")
    if kind == RevisionKind.CREATE:
        raise TransitionRefused("이미 있는 메시지에 create 를 다시 쌓을 수 없습니다")
    if kind == RevisionKind.REDACT:
        # 본문 해시를 남기지 않는다. 짧은 본문은 사전 대입으로 해시에서 되찾힌다 —
        # 해시를 남기면 「본문을 남기지 않는다」 를 지킨 것이 아니다.
        if not fields.get("reason_code"):
            raise TransitionRefused("redact 에는 사유 코드가 필요합니다")
        fields.pop("body_sha256", None)
    return Revision(kind, last.revision_no + 1, **fields)


def current_revision(history: list[Revision]) -> Revision | None:
    """검색이 보는 것. 지워졌으면 `None`."""
    if not history:
        return None
    last = history[-1]
    return None if last.kind in HIDDEN_FROM_SEARCH else last


# ---------------------------------------------------------------------------
# 첨부 revision — 결정적이고, 덮어쓰지 않는다
# ---------------------------------------------------------------------------


def attachment_revision(
    *, source_sha256: str, converter_name: str, converter_version: str, config: dict
) -> str:
    """첨부 변환본의 revision. **네 가지에서 결정적으로 나온다.**

    같은 원본을 같은 변환기·같은 설정으로 다시 변환하면 같은 revision 이 나오므로
    재변환이 멱등하다. 변환기나 설정을 고치면 자동으로 다른 revision 이 되고,
    **옛 revision 은 남는다.**

    남겨야 하는 이유: 옛 변환본을 근거로 인용한 답변이 이미 나가 있을 수 있다.
    덮으면 사람이 출처를 눌렀을 때 인용된 문장이 없다.

    `canonical_digest` 와 같은 직렬화 규칙을 쓴다(정렬된 키, compact separator).
    칸을 이어 붙이면 값 안의 구분자가 경계를 밀어 서로 다른 입력이 같은 해시를 낸다.
    """
    payload = {
        "source_sha256": source_sha256,
        "converter_name": converter_name,
        "converter_version": converter_version,
        "config_sha256": config_digest(config),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def config_digest(config: dict) -> str:
    """변환 설정·스키마의 결정적 hash."""
    blob = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# production 게이트
# ---------------------------------------------------------------------------

#: 운영값이 정해져 있어야 하는 정책들. 스키마 §4 의 행 이름과 같다.
REQUIRED_RETENTION: tuple[str, ...] = ("bot_conversation_audit", "bot_dm_attachment")
REQUIRED_PRODUCTION_FLAGS: tuple[str, ...] = (
    "archiver_writes_live",
    "preserve_edit_delete",
    "require_attachment_ack",
    "revision_reader_ready",
)


def production_blockers(
    retention: dict[str, int | None], *, flags: dict[str, bool] | None = None
) -> list[str]:
    """production 전환을 막는 것들. 비어 있으면 가도 된다.

    보존 기간이 **안 정해진 채로** 넘어가면 기본값이 「영구 보관」 이 된다.
    개인 대화를 영구 보관하는 것은 아무도 결정한 적이 없는데 그냥 그렇게 된다 —
    그래서 「값이 없음」 을 0 과 구분해 다룬다(스키마의 `NULL`).
    """
    blockers = [
        f"보존 기간이 정해지지 않았습니다: {name}"
        for name in REQUIRED_RETENTION
        if retention.get(name) is None
    ]
    blockers.extend(
        f"보존 기간은 1일 이상이어야 합니다: {name}"
        for name in REQUIRED_RETENTION
        if retention.get(name) is not None and retention[name] <= 0
    )
    flags = flags or {}
    blockers.extend(
        f"운영 기능 스위치가 꺼져 있습니다: {name}"
        for name in REQUIRED_PRODUCTION_FLAGS
        if not flags.get(name, False)
    )
    if flags.get("separate_attachments") and not flags.get("attachment_reader_ready", False):
        # 읽는 쪽이 없는데 분리를 켜면 그 본문이 조용히 답변에서 빠진다.
        blockers.append(
            "첨부 분리가 켜져 있는데 읽는 쪽이 준비되지 않았습니다"
        )
    return blockers
