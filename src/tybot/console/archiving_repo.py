"""Archiving 설정의 **저장소 경계** — SQL 은 전부 여기 있다.

결정: 2026-09-25 오너 §3(저장소 경계 분리, 시험은 fake DB).

## 왜 가르나

전에는 규칙과 SQL 이 한 함수에 섞여 있었다. 그러면 시험이 **커서를 흉내 내야**
규칙을 볼 수 있고, 커서 흉내는 진짜 DB 와 달라진다. 「SQL 이 나갔나」 를 보느라
정작 「규칙이 맞나」 를 안 보게 된다.

여기는 좌표를 받아 행을 돌려준다. 판단은 `archiving_admin` 이 한다. 그래서 시험은
가짜 저장소 하나로 규칙을 전부 볼 수 있고, SQL 은 격리 DB 검증이 따로 본다.

## 여기서 하지 않는 것

- **판단하지 않는다.** 전이 가능 여부·사유 필수 같은 것은 `archiving_admin` 몫이다
- **평문 토큰을 다루지 않는다.** mask 만 나간다(`workspace_service_store`)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from .workspace_store import _connect


class ArchivingRepo(Protocol):
    """저장소가 할 수 있는 일. 시험은 이걸 흉내 낸다."""

    def services(self, workspace: str) -> list[dict]: ...
    def channel_state(self, workspace: str, channel_id: str) -> dict | None: ...
    def save_channel_state(self, row: dict) -> None: ...
    def channels(self, workspace: str) -> list[dict]: ...
    def save_membership(self, row: dict) -> None: ...
    def mark_channel_event(self, workspace: str, channel_id: str) -> bool: ...
    def set_operator_hold(self, row: dict) -> int: ...
    def flags(self, workspace: str, channel_ids: list[str]) -> list[dict]: ...
    def flag(self, name: str, scope: str, scope_key: str) -> dict | None: ...
    def save_flag(self, row: dict) -> None: ...
    def retention(self) -> list[dict]: ...
    def save_retention(self, name: str, days: int | None, approved_by: str) -> int: ...
    def audit(self, workspace: str, limit: int) -> list[dict]: ...
    def add_audit(self, row: dict) -> None: ...
    def transaction(self) -> Iterator[ArchivingRepo]: ...


class PostgresArchivingRepo:
    """진짜 저장소. **잠금은 여기서 잡는다** — 좌표를 아는 곳이 여기다."""

    def __init__(self, connect=_connect, *, transaction_cursor=None) -> None:
        self._connect = connect
        self._transaction_cursor = transaction_cursor

    @contextmanager
    def transaction(self) -> Iterator[PostgresArchivingRepo]:
        """Use one transaction for lock, decision, write and audit."""
        if self._transaction_cursor is not None:
            yield self
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield PostgresArchivingRepo(self._connect, transaction_cursor=cur)

    @contextmanager
    def _cursor(self):
        if self._transaction_cursor is not None:
            yield self._transaction_cursor
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield cur

    # -- 서비스 --------------------------------------------------------
    def services(self, workspace: str) -> list[dict]:
        """`workspace_service_store` 가 든다. **mask 만 나온다.**

        여기서 다시 SQL 을 쓰지 않는다 — 토큰 표를 읽는 질의가 두 자리에
        있으면 한쪽이 언젠가 `ciphertext` 를 고른다.
        """
        from .workspace_service_store import list_services

        return list_services(workspace)

    # -- 채널 모드 ------------------------------------------------------
    def channel_state(self, workspace: str, channel_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"archive-mode:{workspace}/{channel_id}",),
            )
            cur.execute(
                """
                SELECT mode, writer_owner, cutover_ts
                  FROM archive_channel_mode
                 WHERE workspace = %s AND channel_id = %s
                 FOR UPDATE
                """,
                (workspace, channel_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def save_channel_state(self, row: dict) -> None:
        """모드와 주인을 **한 문장에** 쓴다.

        나눠 쓰면 둘이 갈라지는 순간이 생기고, 그 순간 두 writer 가 같은 파일에
        쓴다고 판단한다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archive_channel_mode
                    (workspace, channel_id, mode, writer_owner, cutover_ts,
                     cutover_at, updated_by)
                VALUES (%(workspace)s, %(channel_id)s, %(mode)s, %(writer_owner)s,
                        %(cutover_ts)s,
                        CASE WHEN %(cutover_ts)s <> '' THEN now() ELSE NULL END,
                        %(updated_by)s)
                ON CONFLICT (workspace, channel_id) DO UPDATE SET
                    mode = excluded.mode,
                    writer_owner = excluded.writer_owner,
                    cutover_ts = excluded.cutover_ts,
                    cutover_at = COALESCE(excluded.cutover_at,
                                          archive_channel_mode.cutover_at),
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                row,
            )

    def channels(self, workspace: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT channel_id, mode, writer_owner, cutover_ts, cutover_at,
                       is_pilot, note, updated_at, updated_by,
                       membership, membership_checked_at, operator_hold,
                       last_event_at, channel_name, is_private
                  FROM archive_channel_mode
                 WHERE workspace = %s
                 ORDER BY channel_id
                """,
                (workspace,),
            )
            return [dict(row) for row in cur.fetchall()]

    def save_membership(self, row: dict) -> None:
        """초대 기반 동기화가 쓰는 자리. **모드와 멤버십을 한 문장에** 쓴다.

        나눠 쓰면 「참여 중인데 멈춘」 과 「빠졌는데 도는」 순간이 생기고, 그 순간
        수집 여부를 화면에서 설명할 수 없다.

        `writer_owner` 는 건드리지 않는다 — 운영 원문의 주인은 사람이 사유와 함께
        바꾸는 것이고, 동기화 잡이 할 일이 아니다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT archiver_save_membership(
                    %(workspace)s, %(channel_id)s, %(mode)s, %(membership)s,
                    %(channel_name)s, %(is_private)s, %(updated_by)s,
                    %(audit)s, %(reason)s
                )
                """,
                row,
            )

    def set_operator_hold(self, row: dict) -> int:
        """사람이 그 채널 수집을 끄거나 켠다. **재초대가 이 결정을 뒤집지 않는다.**

        끄면 `paused`, 켜면 참여 중일 때만 `shadow` 로 돌아간다 — 봇이 없는 채널을
        켜 봐야 권한 오류만 쌓인다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE archive_channel_mode
                   SET operator_hold = %(hold)s,
                       mode = CASE
                           WHEN %(hold)s THEN 'paused'
                           WHEN membership = 'joined' THEN 'shadow'
                           ELSE mode
                       END,
                       updated_at = now(), updated_by = %(actor)s
                 WHERE workspace = %(workspace)s AND channel_id = %(channel_id)s
                """,
                row,
            )
            return int(cur.rowcount or 0)

    def mark_channel_event(self, workspace: str, channel_id: str) -> bool:
        """Record an accepted event without granting the runtime table UPDATE."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT archiver_mark_channel_event(%s, %s)",
                (workspace, channel_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            value = next(iter(row.values())) if hasattr(row, "values") else row[0]
            return bool(value)

    # -- 기능 스위치 ----------------------------------------------------
    def flags(self, workspace: str, channel_ids: list[str]) -> list[dict]:
        """전역 + 이 워크스페이스 + 이 워크스페이스의 채널.

        전역만 보여 주면 「전역은 꺼졌는데 왜 켜져 있나」 를 화면에서 못 답한다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT name, scope, scope_key, enabled, description,
                       updated_at, updated_by
                  FROM archive_feature_flag
                 WHERE scope = 'global'
                    OR (scope = 'workspace' AND scope_key = %s)
                    OR (scope = 'channel' AND scope_key = ANY(%s))
                 ORDER BY name, scope, scope_key
                 FOR SHARE
                """,
                (workspace, list(channel_ids)),
            )
            return [dict(row) for row in cur.fetchall()]

    def flag(self, name: str, scope: str, scope_key: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT enabled FROM archive_feature_flag
                 WHERE name = %s AND scope = %s AND scope_key = %s
                 FOR UPDATE
                """,
                (name, scope, scope_key),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def save_flag(self, row: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archive_feature_flag
                    (name, scope, scope_key, enabled, updated_by)
                VALUES (%(name)s, %(scope)s, %(scope_key)s, %(enabled)s, %(updated_by)s)
                ON CONFLICT (name, scope, scope_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                row,
            )

    # -- 보존 정책 ------------------------------------------------------
    def retention(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT name, retention_days, approved_by, approved_at, description
                  FROM archive_retention_policy ORDER BY name FOR SHARE
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def save_retention(self, name: str, days: int | None, approved_by: str) -> int:
        """바뀐 행 수를 돌려준다. `0` 이면 **없는 정책**이다.

        조용히 만들지 않는다. 없는 이름으로 행이 생기면 게이트가 보는 행과 다른
        행이 만들어지고, 그때 게이트는 계속 「안 정했다」 라고 말한다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE archive_retention_policy
                   SET retention_days = %(days)s,
                       approved_by = CASE WHEN %(days)s IS NULL THEN ''
                                          ELSE %(approved_by)s END,
                       approved_at = CASE WHEN %(days)s IS NULL THEN NULL
                                          ELSE now() END
                 WHERE name = %(name)s
                """,
                {"days": days, "approved_by": approved_by, "name": name},
            )
            return int(cur.rowcount or 0)

    def retention_row(self, name: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT retention_days FROM archive_retention_policy
                 WHERE name = %s FOR UPDATE
                """,
                (name,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # -- 감사 ------------------------------------------------------------
    def audit(self, workspace: str, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT at, actor, subject, channel_id, field,
                       old_value, new_value, reason
                  FROM archive_config_audit
                 WHERE workspace = %s OR workspace = ''
                 ORDER BY at DESC LIMIT %s
                """,
                (workspace, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def add_audit(self, row: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archive_config_audit
                    (actor, subject, workspace, channel_id, field,
                     old_value, new_value, reason)
                VALUES (%(actor)s, %(subject)s, %(workspace)s, %(channel_id)s,
                        %(field)s, %(old_value)s, %(new_value)s, %(reason)s)
                """,
                row,
            )


def default_repo() -> PostgresArchivingRepo:
    return PostgresArchivingRepo()
