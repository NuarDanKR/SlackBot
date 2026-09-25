"""Archiving Bot 운영 손잡이 — **콘솔에서만 돌린다.**

결정: 2026-09-25 오너 확정 §5·§6.
스키마: `deploy/sql/archiving_schema.sql` · `deploy/sql/workspace_service_schema.sql`.

## 왜 여기 있나

ENV 로 두면 채널을 늘릴 때마다 SSH 가 필요하고, 그건 **쓸 수 있는 사람이 한 명**
이라는 뜻이다. 실제로 그래서 「대기 31건·처리 0건」 이 났다(CLAUDE.md).

## 이 모듈이 지키는 두 규칙

**1. 모드와 주인을 따로 갱신하지 않는다.** 상태 변경은 전부
`archiving_state.plan_mode_change` 를 지난다. SQL 에서 `mode` 만 바꾸거나
`writer_owner` 만 바꾸는 길을 열면, 「active 인데 주인은 master」 가 만들어지고
그 상태에서 두 writer 가 같은 파일에 쓴다. 줄이 섞이고 `doc_count` 가 유실된다.

**2. 사람과 사유 없이는 못 바꾼다.** 누가 언제 왜 바꿨는지 없으면 사고가 났을 때
범위를 정할 수 없다. `actor` 와 `reason` 이 비면 거절한다 — 기본값을 두면 전부
그 기본값으로 남고, 그건 기록이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..archive.archiving_state import (
    ChannelMode,
    ChannelState,
    TransitionRefused,
    WriterOwner,
    production_blockers,
)
from .workspace_service_store import list_services
from .workspace_store import WorkspaceStoreError, _connect

#: 감사에 남길 때 쓰는 주어. 표 이름이 아니라 **사람이 부르는 이름**이다.
SUBJECT_CHANNEL = "channel_mode"
SUBJECT_FLAG = "feature_flag"
SUBJECT_RETENTION = "retention"


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


def workspace_detail(workspace: str) -> dict:
    """화면 한 장에 필요한 것을 **한 번에** 모은다.

    화면마다 따로 질의하면 사람이 보는 순간의 상태가 서로 다른 시각의 것이 된다.
    모드는 바뀌었는데 감사에는 아직 안 보이는 식이고, 그때 사람은 자기가 누른
    것이 안 먹었다고 생각해 한 번 더 누른다.
    """
    services = list_services(workspace)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT channel_id, mode, writer_owner, cutover_ts, cutover_at,
                   is_pilot, note, updated_at, updated_by
              FROM archive_channel_mode
             WHERE workspace = %s
             ORDER BY channel_id
            """,
            (workspace,),
        )
        channels = [dict(row) for row in cur.fetchall()]

        # 전역 + 이 워크스페이스 + 이 워크스페이스의 채널. 전역만 보여 주면
        # 「전역은 꺼졌는데 왜 켜져 있나」 를 화면에서 답할 수 없다.
        cur.execute(
            """
            SELECT name, scope, scope_key, enabled, description, updated_at, updated_by
              FROM archive_feature_flag
             WHERE scope = 'global'
                OR (scope = 'workspace' AND scope_key = %s)
                OR (scope = 'channel' AND scope_key = ANY(%s))
             ORDER BY name, scope, scope_key
            """,
            (workspace, [str(row["channel_id"]) for row in channels]),
        )
        flags = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT name, retention_days, approved_by, approved_at, description
              FROM archive_retention_policy ORDER BY name
            """
        )
        retention = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT at, actor, subject, channel_id, field, old_value, new_value, reason
              FROM archive_config_audit
             WHERE workspace = %s OR workspace = ''
             ORDER BY at DESC LIMIT 50
            """,
            (workspace,),
        )
        audit = [dict(row) for row in cur.fetchall()]

    return {
        "workspace": workspace,
        "services": services,
        "channels": channels,
        "flags": flags,
        "retention": retention,
        "audit": audit,
        # 「지금 production 으로 가도 되나」 를 화면이 직접 답한다. 안 보여 주면
        # 누군가 막힌 이유를 찾으러 서버에 들어간다.
        "blockers": production_blockers(
            {str(row["name"]): row["retention_days"] for row in retention},
            flags=_flag_map(flags),
        ),
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
) -> ChannelState:
    """채널 모드를 바꾼다. **`archiving_state` 가 다음 상태를 정한다.**

    콘솔이 `mode` 와 `writer_owner` 를 따로 쓰지 않는다. 여기서 계산한 상태를
    통째로 저장하고, 표의 `CHECK` 가 그 결과를 한 번 더 본다.
    """
    mode = ChannelMode(target)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"archive-mode:{workspace}/{channel_id}",),
            )
            cur.execute(
                """
                SELECT mode, writer_owner, cutover_ts FROM archive_channel_mode
                 WHERE workspace = %s AND channel_id = %s FOR UPDATE
                """,
                (workspace, channel_id),
            )
            row = cur.fetchone()
            current = (
                ChannelState(
                    workspace, channel_id,
                    ChannelMode(row["mode"]), WriterOwner(row["writer_owner"]),
                    str(row["cutover_ts"] or ""),
                )
                if row
                else ChannelState(workspace, channel_id, ChannelMode.OFF, WriterOwner.MASTER)
            )
            try:
                after = plan(current, mode, cutover_ts=cutover_ts)
            except TransitionRefused as exc:
                raise AdminRefused(str(exc)) from exc

            cur.execute(
                """
                INSERT INTO archive_channel_mode
                    (workspace, channel_id, mode, writer_owner, cutover_ts,
                     cutover_at, updated_by)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s <> '' THEN now() ELSE NULL END, %s)
                ON CONFLICT (workspace, channel_id) DO UPDATE SET
                    mode = excluded.mode,
                    writer_owner = excluded.writer_owner,
                    cutover_ts = excluded.cutover_ts,
                    cutover_at = COALESCE(excluded.cutover_at, archive_channel_mode.cutover_at),
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                (
                    workspace, channel_id, str(after.mode), str(after.writer_owner),
                    after.cutover_ts, after.cutover_ts, actor.name,
                ),
            )
            # 모드와 주인을 **한 기록에** 남긴다. 따로 남기면 나중에 둘이 같이
            # 움직였는지 확인할 수 없다.
            _audit(
                cur, actor, SUBJECT_CHANNEL, workspace, channel_id,
                field="mode+writer_owner",
                old=f"{current.mode}/{current.writer_owner}@{current.cutover_ts or '-'}",
                new=f"{after.mode}/{after.writer_owner}@{after.cutover_ts or '-'}",
            )
            return after
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise AdminRefused(f"채널 모드 변경 실패: {exc}") from exc


def plan(current: ChannelState, target: ChannelMode, *, cutover_ts: str = "") -> ChannelState:
    """전이 계산을 한 이름으로 감싼다.

    감싸는 이유는 **여기 말고 다른 곳에서 상태를 계산하지 못하게** 하기 위해서다.
    호출부가 `archiving_state` 를 직접 부르면 그 호출부만 규칙이 달라질 수 있다.
    """
    from ..archive.archiving_state import plan_mode_change

    return plan_mode_change(current, target, cutover_ts=cutover_ts)


def set_feature_flag(
    name: str, enabled: bool, actor: Actor, *, scope: str = "global", scope_key: str = ""
) -> None:
    """기능 스위치. 전역·워크스페이스·채널 범위를 구분해 켠다."""
    if scope not in ("global", "workspace", "channel"):
        raise AdminRefused(f"알 수 없는 범위입니다: {scope}")
    if (scope == "global") != (not scope_key):
        raise AdminRefused(
            "전역 스위치에는 대상이 없어야 하고, 전역이 아니면 대상이 있어야 합니다."
        )
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled FROM archive_feature_flag
                 WHERE name = %s AND scope = %s AND scope_key = %s FOR UPDATE
                """,
                (name, scope, scope_key),
            )
            row = cur.fetchone()
            before = "" if row is None else str(bool(row["enabled"])).lower()
            cur.execute(
                """
                INSERT INTO archive_feature_flag (name, scope, scope_key, enabled, updated_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name, scope, scope_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                (name, scope, scope_key, enabled, actor.name),
            )
            _audit(
                cur, actor, SUBJECT_FLAG,
                scope_key if scope == "workspace" else "",
                scope_key if scope == "channel" else "",
                field=f"{name}@{scope}",
                old=before, new=str(enabled).lower(),
            )
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise AdminRefused(f"기능 스위치 변경 실패: {exc}") from exc


def set_retention(name: str, days: int | None, actor: Actor) -> None:
    """보존 기간. **`None` 으로 되돌리는 것도 결정이라 기록한다.**

    운영값은 1일 이상이다. `0` 을 즉시 삭제로 읽으면 기록이 생기자마자 사라져
    감사 계약이 성립하지 않는다.
    """
    if days is not None and days <= 0:
        raise AdminRefused("보존 기간은 1일 이상이어야 합니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT retention_days FROM archive_retention_policy WHERE name = %s FOR UPDATE",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                raise AdminRefused(f"없는 보존 정책입니다: {name}")
            before = row["retention_days"]
            cur.execute(
                """
                UPDATE archive_retention_policy
                   SET retention_days = %s,
                       approved_by = CASE WHEN %s IS NULL THEN '' ELSE %s END,
                       approved_at = CASE WHEN %s IS NULL THEN NULL ELSE now() END
                 WHERE name = %s
                """,
                (days, days, actor.name, days, name),
            )
            _audit(
                cur, actor, SUBJECT_RETENTION, "", "",
                field=name,
                old="" if before is None else str(before),
                new="" if days is None else str(days),
            )
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise AdminRefused(f"보존 정책 변경 실패: {exc}") from exc


def _audit(cur, actor: Actor, subject: str, workspace: str, channel_id: str,
           *, field: str, old: str, new: str) -> None:
    cur.execute(
        """
        INSERT INTO archive_config_audit
            (actor, subject, workspace, channel_id, field, old_value, new_value, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (actor.name, subject, workspace, channel_id, field, old, new, actor.reason),
    )
