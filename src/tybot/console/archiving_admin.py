"""Archiving Bot 운영 손잡이 — **콘솔에서만 돌린다.**

결정: 2026-09-25 오너 확정 §5·§6·§9·§10.
스키마: `deploy/sql/archiving_schema.sql` · `deploy/sql/workspace_service_schema.sql`.

## 왜 여기 있나

ENV 로 두면 채널을 늘릴 때마다 SSH 가 필요하고, 그건 **쓸 수 있는 사람이 한 명**
이라는 뜻이다. 실제로 그래서 「대기 31건·처리 0건」 이 났다(CLAUDE.md).

## 이 모듈이 지키는 세 규칙

**1. 모드와 주인을 따로 갱신하지 않는다.** 상태 변경은 전부
`archiving_state.plan_mode_change` 를 지난다. `mode` 만 바꾸거나 `writer_owner` 만
바꾸는 길을 열면 「active 인데 주인은 master」 가 만들어지고, 그 상태에서 두
writer 가 같은 파일에 쓴다. 줄이 섞이고 `doc_count` 가 유실된다.

**2. 사람과 사유 없이는 못 바꾼다.** 누가 언제 왜 바꿨는지 없으면 사고가 났을 때
범위를 정할 수 없다. 기본값을 두면 전부 그 기본값으로 남고, 그건 기록이 아니다.

**3. 검증 안 된 스키마로 `active` 에 못 간다.** 격리 DB 검증이 release gate 다
(`release_gate`). 그림자는 열려 있다 — 운영 원문을 안 건드리기 때문이다.

## SQL 은 여기 없다

`archiving_repo` 가 든다. 그래서 시험은 가짜 저장소 하나로 규칙을 전부 볼 수 있고,
커서를 흉내 내지 않는다 — 커서 흉내는 진짜 DB 와 달라진다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..archive.archiving_state import (
    ChannelMode,
    ChannelState,
    TransitionRefused,
    WriterOwner,
    plan_mode_change,
    production_blockers,
)
from .archiving_repo import ArchivingRepo, default_repo
from .release_gate import GateClosed, gate_status, require_verified_schema
from .workspace_store import WorkspaceStoreError

#: 감사에 남길 때 쓰는 주어. 표 이름이 아니라 **사람이 부르는 이름**이다.
SUBJECT_CHANNEL = "channel_mode"
SUBJECT_FLAG = "feature_flag"
SUBJECT_RETENTION = "retention"

#: 검증 전에는 못 가는 모드. **그림자는 여기 없다** — 운영 원문을 안 건드린다.
GATED_MODES: frozenset[ChannelMode] = frozenset({ChannelMode.ACTIVE})

#: 검증 전에는 못 켜는 스위치. 켜는 순간 운영 원문의 모양이 바뀐다.
GATED_FLAGS: frozenset[str] = frozenset({
    "archiver_writes_live", "separate_attachments", "preserve_edit_delete",
})

KNOWN_FLAGS: frozenset[str] = frozenset({
    "archiver_writes_live",
    "attachment_reader_ready",
    "preserve_edit_delete",
    "require_attachment_ack",
    "revision_reader_ready",
    "separate_attachments",
})

GLOBAL_ONLY_FLAGS: frozenset[str] = frozenset({
    "archiver_writes_live",
    "attachment_reader_ready",
    "revision_reader_ready",
})


class AdminRefused(WorkspaceStoreError):
    """콘솔 조작이 거절됐다. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class Actor:
    """누가 바꿨나. 빈 값을 허용하지 않는 이유가 이 클래스의 전부다."""

    name: str
    reason: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise AdminRefused("바꾼 사람이 비어 있습니다.")
        if not self.reason.strip():
            raise AdminRefused(
                "사유가 비어 있습니다. 사고가 났을 때 범위를 정하려면 왜 바꿨는지가 필요합니다."
            )


# ---------------------------------------------------------------------------
# 읽기 — Workspace 상세 화면이 쓰는 것
# ---------------------------------------------------------------------------


def workspace_detail(workspace: str, repo: ArchivingRepo | None = None) -> dict:
    """화면 한 장에 필요한 것을 **한 번에** 모은다.

    화면마다 따로 질의하면 사람이 보는 순간의 상태가 서로 다른 시각의 것이 된다.
    모드는 바뀌었는데 감사에는 아직 안 보이는 식이고, 그때 사람은 자기가 누른
    것이 안 먹었다고 생각해 한 번 더 누른다.
    """
    store = repo or default_repo()
    channels = store.channels(workspace)
    flags = store.flags(workspace, [str(row["channel_id"]) for row in channels])
    retention = store.retention()
    gate = gate_status()
    return {
        "workspace": workspace,
        "services": store.services(workspace),
        "channels": channels,
        "flags": flags,
        "retention": retention,
        "audit": store.audit(workspace, 50),
        # 「지금 무엇이 막혀 있나」 를 화면이 직접 답한다. 안 보여 주면 누군가
        # 막힌 이유를 찾으러 서버에 들어간다.
        "schemaGate": gate.as_json(),
        "blockers": production_blockers(
            {str(row["name"]): row["retention_days"] for row in retention},
            flags=_flag_map(flags),
        ),
        # 화면이 버튼을 회색으로 만들 근거. 눌러 보고 거절당하는 것보다 낫다.
        "gatedModes": sorted(str(mode) for mode in GATED_MODES),
        "gatedFlags": sorted(GATED_FLAGS),
    }


def _flag_map(flags: list[dict]) -> dict[str, bool]:
    """전역 스위치만 게이트에 쓴다. 파일럿 한 채널이 전체 판정을 뒤집으면 안 된다."""
    return {
        str(row["name"]): bool(row["enabled"])
        for row in flags
        if row["scope"] == "global"
    }


# ---------------------------------------------------------------------------
# 쓰기 — 전부 전이 함수를 지난다
# ---------------------------------------------------------------------------


def set_channel_mode(
    workspace: str,
    channel_id: str,
    target: ChannelMode | str,
    actor: Actor,
    *,
    cutover_ts: str = "",
    repo: ArchivingRepo | None = None,
) -> ChannelState:
    """채널 모드를 바꾼다. **`archiving_state` 가 다음 상태를 정한다.**

    `writer_owner` 를 인자로 받지 않는다. 호출부가 주인을 고를 수 있으면 전이
    규칙이 장식이 된다.
    """
    mode = ChannelMode(target)
    if mode in GATED_MODES:
        # 게이트를 **전이 계산보다 먼저** 본다. 전이가 통과한 뒤에 막으면
        # 「갈 수 있는데 안 보내 준다」 로 보이고, 사람은 규칙을 의심한다.
        try:
            require_verified_schema(f"{mode} 전환")
        except GateClosed as exc:
            raise AdminRefused(str(exc)) from exc

    store = repo or default_repo()
    with store.transaction() as tx:
        row = tx.channel_state(workspace, channel_id)
        current = (
            ChannelState(
                workspace, channel_id,
                ChannelMode(row["mode"]), WriterOwner(row["writer_owner"]),
                str(row.get("cutover_ts") or ""),
            )
            if row
            else ChannelState(workspace, channel_id, ChannelMode.OFF, WriterOwner.MASTER)
        )
        try:
            after = plan_mode_change(current, mode, cutover_ts=cutover_ts)
        except TransitionRefused as exc:
            raise AdminRefused(str(exc)) from exc

        if mode == ChannelMode.ACTIVE:
            blockers = _production_blockers(tx, workspace, [channel_id])
            if blockers:
                raise AdminRefused(
                    "운영 전환 조건이 남아 있습니다: " + " / ".join(blockers)
                )

        tx.save_channel_state({
            "workspace": workspace,
            "channel_id": channel_id,
            "mode": str(after.mode),
            "writer_owner": str(after.writer_owner),
            "cutover_ts": after.cutover_ts,
            "updated_by": actor.name,
        })
        _audit(
            tx, actor, SUBJECT_CHANNEL, workspace, channel_id,
            field="mode+writer_owner",
            old=f"{current.mode}/{current.writer_owner}@{current.cutover_ts or '-'}",
            new=f"{after.mode}/{after.writer_owner}@{after.cutover_ts or '-'}",
        )
    return after


def set_feature_flag(
    name: str,
    enabled: bool,
    actor: Actor,
    *,
    scope: str = "global",
    scope_key: str = "",
    workspace_context: str = "",
    repo: ArchivingRepo | None = None,
) -> None:
    """기능 스위치. 전역·워크스페이스·채널 범위를 구분해 켠다.

    **끄는 것은 게이트가 막지 않는다.** 사고 때 내리는 손잡이를 검증 상태로
    막으면, 막아야 할 순간에 못 막는다.
    """
    if scope not in ("global", "workspace", "channel"):
        raise AdminRefused(f"알 수 없는 범위입니다: {scope}")
    if name not in KNOWN_FLAGS:
        raise AdminRefused(f"알 수 없는 기능 스위치입니다: {name}")
    if name in GLOBAL_ONLY_FLAGS and scope != "global":
        raise AdminRefused(f"{name} 스위치는 전역 범위에서만 바꿀 수 있습니다.")
    if (scope == "global") != (not scope_key):
        raise AdminRefused(
            "전역 스위치에는 대상이 없어야 하고, 전역이 아니면 대상이 있어야 합니다."
        )
    if enabled and name in GATED_FLAGS:
        try:
            require_verified_schema(f"{name} 켜기")
        except GateClosed as exc:
            raise AdminRefused(str(exc)) from exc

    if workspace_context and scope == "workspace" and scope_key != workspace_context:
        raise AdminRefused("요청 경로와 다른 워크스페이스 설정은 바꿀 수 없습니다.")

    store = repo or default_repo()
    with store.transaction() as tx:
        if workspace_context and scope == "channel":
            known_channels = {
                str(row["channel_id"]) for row in tx.channels(workspace_context)
            }
            if scope_key not in known_channels:
                raise AdminRefused("요청 워크스페이스에 없는 채널입니다.")

        if enabled and name == "separate_attachments":
            reader = tx.flag("attachment_reader_ready", "global", "")
            if not reader or not bool(reader["enabled"]):
                raise AdminRefused(
                    "첨부 reader 준비가 확인되기 전에는 첨부 분리를 켤 수 없습니다."
                )

        row = tx.flag(name, scope, scope_key)
        before = "" if row is None else str(bool(row["enabled"])).lower()
        tx.save_flag({
            "name": name, "scope": scope, "scope_key": scope_key,
            "enabled": enabled, "updated_by": actor.name,
        })
        _audit(
            tx, actor, SUBJECT_FLAG,
            scope_key if scope == "workspace" else "",
            scope_key if scope == "channel" else "",
            field=f"{name}@{scope}", old=before, new=str(enabled).lower(),
        )


def set_retention(
    name: str, days: int | None, actor: Actor, *, repo: ArchivingRepo | None = None
) -> None:
    """보존 기간. **`None` 으로 되돌리는 것도 결정이라 기록한다.**

    운영값은 1일 이상이다. `0` 을 즉시 삭제로 읽으면 기록이 생기자마자 사라져
    감사 계약이 성립하지 않는다.
    """
    if days is not None and days <= 0:
        raise AdminRefused("보존 기간은 1일 이상이어야 합니다.")
    store = repo or default_repo()
    with store.transaction() as tx:
        row = tx.retention_row(name)
        if row is None:
            raise AdminRefused(f"없는 보존 정책입니다: {name}")
        before = row.get("retention_days")
        if tx.save_retention(name, days, actor.name) == 0:
            raise AdminRefused(f"없는 보존 정책입니다: {name}")
        _audit(
            tx, actor, SUBJECT_RETENTION, "", "",
            field=name,
            old="" if before is None else str(before),
            new="" if days is None else str(days),
        )


def _production_blockers(
    repo: ArchivingRepo, workspace: str, channel_ids: list[str]
) -> list[str]:
    retention = repo.retention()
    flags = repo.flags(workspace, channel_ids)
    return production_blockers(
        {str(row["name"]): row["retention_days"] for row in retention},
        flags=_flag_map(flags),
    )


def _audit(repo: ArchivingRepo, actor: Actor, subject: str, workspace: str,
           channel_id: str, *, field: str, old: str, new: str) -> None:
    repo.add_audit({
        "actor": actor.name, "subject": subject, "workspace": workspace,
        "channel_id": channel_id, "field": field,
        "old_value": old, "new_value": new, "reason": actor.reason,
    })
