"""봇·연결·라우트의 **저장소 경계** — SQL 은 전부 여기 있다.

설계: `docs/design/workspace-service-console-redesign.md` §4·§7 (2026-09-28)
스키마: `deploy/sql/bot_connection_schema.sql` · 판단: `bot_admin`

## 왜 가르나

`archiving_repo` 와 같은 이유다. 규칙과 SQL 이 한 함수에 섞이면 시험이 **커서를
흉내 내야** 규칙을 볼 수 있고, 커서 흉내는 진짜 DB 와 달라진다. 「SQL 이 나갔나」
를 보느라 정작 「규칙이 맞나」 를 안 보게 된다.

## 여기서 하지 않는 것

- **판단하지 않는다.** 신원 일치·전이 가능 여부·사유 필수는 `bot_admin` 몫이다
- **평문 토큰을 다루지 않는다.** 나가는 것은 `mask` 뿐이다. 암호문은 `save_secrets`
  로 들어가기만 하고, 읽는 질의는 이 파일에 없다 — 있으면 언젠가 로그에 찍히고,
  로그는 감사보다 오래 남는다
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from .workspace_store import _connect

#: 변경 이력 화면이 걷는 주어들. 연결·라우트·옛 서비스가 한 줄기로 보여야
#: 「언제 무엇으로 옮겼나」 를 한 화면에서 읽는다.
AUDIT_SUBJECTS = ("bot_connection", "specialist_route", "workspace_service")


class BotRepo(Protocol):
    """저장소가 할 수 있는 일. 시험은 이걸 흉내 낸다."""

    def catalog(self) -> list[dict]: ...
    def connections(self, workspace: str = "") -> list[dict]: ...
    def connection(self, workspace: str, bot_key: str) -> dict | None: ...
    def save_connection(self, row: dict) -> int: ...
    def save_secrets(self, connection_id: int, secrets: dict, actor: str) -> None: ...
    def reset_identity(self, connection_id: int) -> None: ...
    def record_identity(self, row: dict) -> int: ...
    def set_connection_state(self, connection_id: int, state: str, actor: str) -> int: ...
    def save_manifest_attestation(self, row: dict) -> None: ...
    def routes(self, workspace: str = "") -> list[dict]: ...
    def route(self, specialist: str, workspace: str) -> dict | None: ...
    def save_route(self, row: dict) -> None: ...
    def specialists(self) -> list[dict]: ...
    def legacy_services(self, workspace: str = "") -> list[dict]: ...
    def audit(self, workspace: str, limit: int) -> list[dict]: ...
    def add_audit(self, row: dict) -> None: ...
    def transaction(self) -> Iterator[BotRepo]: ...


class PostgresBotRepo:
    """진짜 저장소. **잠금은 여기서 잡는다** — 좌표를 아는 곳이 여기다."""

    def __init__(self, connect=_connect, *, transaction_cursor=None) -> None:
        self._connect = connect
        self._transaction_cursor = transaction_cursor

    @contextmanager
    def transaction(self) -> Iterator[PostgresBotRepo]:
        """잠금·판단·쓰기·감사를 **한 트랜잭션**으로 묶는다.

        나누면 판단과 쓰기 사이에 다른 사람이 끼어들 수 있고, 그때 두 번째 사람의
        변경이 첫 번째 사람의 판단 위에 얹힌다.
        """
        if self._transaction_cursor is not None:
            yield self
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield PostgresBotRepo(self._connect, transaction_cursor=cur)

    @contextmanager
    def _cursor(self):
        if self._transaction_cursor is not None:
            yield self._transaction_cursor
            return
        with self._connect() as conn, conn.cursor() as cur:
            yield cur

    # -- 카탈로그 -------------------------------------------------------
    def catalog(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT key, display_name, category, owner_team,
                       slack_connectable, internally_invokable, state,
                       created_at, updated_at
                  FROM bot_catalog
                 ORDER BY CASE category
                            WHEN 'orchestrator' THEN 0
                            WHEN 'collector' THEN 1
                            ELSE 2 END,
                          key
                """
            )
            return [dict(row) for row in cur.fetchall()]

    # -- Slack 연결 -----------------------------------------------------
    def connections(self, workspace: str = "") -> list[dict]:
        """연결과 **mask 만.** 암호문은 고르지 않는다.

        `token_count` 로 「한쪽만 들어간 상태」 를 화면이 구분한다 — 봇 토큰만
        있고 앱 토큰이 없으면 Socket Mode 가 안 열리는데, 그건 오류 없이 조용하다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.workspace, c.bot_key, c.connector_type, c.state,
                       c.team_id, c.bot_user_id, c.identity_ok, c.identity_error,
                       c.identity_checked_at, c.manifest_id,
                       c.manifest_attested_sha256, c.manifest_attested_at,
                       c.manifest_attested_by, c.last_heartbeat_at, c.last_event_at,
                       c.runtime_version, c.runtime_error, c.note,
                       c.updated_at, c.updated_by,
                       max(s.mask) FILTER (WHERE s.kind = 'bot') AS bot_mask,
                       max(s.mask) FILTER (WHERE s.kind = 'app') AS app_mask,
                       count(s.kind) AS token_count
                  FROM bot_connection c
                  LEFT JOIN bot_connection_secret s ON s.connection_id = c.id
                 WHERE (%s = '' OR c.workspace = %s)
                 GROUP BY c.id
                 ORDER BY c.workspace, c.bot_key
                """,
                (workspace, workspace),
            )
            return [dict(row) for row in cur.fetchall()]

    def connection(self, workspace: str, bot_key: str) -> dict | None:
        """바꿀 연결 하나. **잠그고** 읽는다."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"bot-connection:{workspace}",),
            )
            cur.execute(
                """
                SELECT id, workspace, bot_key, connector_type, state, team_id,
                       bot_user_id, identity_ok, note
                  FROM bot_connection
                 WHERE workspace = %s AND bot_key = %s AND connector_type = 'slack_socket'
                 FOR UPDATE
                """,
                (workspace, bot_key),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def save_connection(self, row: dict) -> int:
        """연결을 만들거나 메모를 고친다. **상태는 여기서 안 켠다.**

        켜는 것은 신원 검사를 통과했을 때뿐이다(`record_identity`). 등록과 동시에
        켜면 토큰을 잘못 붙인 채로 수집이 시작되고, 그건 붙인 사람이 자리를 뜬 뒤에
        드러난다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_connection
                    (workspace, bot_key, connector_type, state, note, updated_by)
                VALUES (%(workspace)s, %(bot_key)s, 'slack_socket', 'draft',
                        %(note)s, %(actor)s)
                ON CONFLICT (workspace, bot_key, connector_type) DO UPDATE SET
                    note = excluded.note,
                    updated_at = now(),
                    updated_by = excluded.updated_by
                RETURNING id
                """,
                row,
            )
            return int(cur.fetchone()["id"])

    def save_secrets(self, connection_id: int, secrets: dict, actor: str) -> None:
        """암호문과 mask 를 넣는다. **평문은 여기까지 오지 않는다.**"""
        with self._cursor() as cur:
            for kind, (ciphertext, mask) in sorted(secrets.items()):
                cur.execute(
                    """
                    INSERT INTO bot_connection_secret
                        (connection_id, kind, ciphertext, mask, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (connection_id, kind) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        mask = excluded.mask,
                        updated_at = now(),
                        updated_by = excluded.updated_by
                    """,
                    (connection_id, kind, ciphertext, mask, actor),
                )

    def reset_identity(self, connection_id: int) -> None:
        """토큰이 바뀌면 **이전 검사 결과는 이 토큰의 것이 아니다.**

        지우지 않으면 새 토큰이 옛 검사 결과를 물려받아 검사 없이 켜진 채로 남는다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bot_connection
                   SET identity_ok = NULL, identity_error = '',
                       identity_checked_at = NULL,
                       state = CASE WHEN state = 'enabled' THEN 'disabled' ELSE state END,
                       updated_at = now()
                 WHERE id = %s
                """,
                (connection_id,),
            )

    def record_identity(self, row: dict) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bot_connection
                   SET team_id = %(team_id)s, bot_user_id = %(bot_user_id)s,
                       identity_ok = %(ok)s, identity_error = %(error)s,
                       identity_checked_at = now(),
                       state = CASE WHEN %(ok)s THEN 'enabled' ELSE 'disabled' END,
                       runtime_error = '',
                       updated_at = now(), updated_by = %(actor)s
                 WHERE id = %(id)s
                """,
                row,
            )
            return int(cur.rowcount or 0)

    def set_connection_state(self, connection_id: int, state: str, actor: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bot_connection
                   SET state = %s, updated_at = now(), updated_by = %s
                 WHERE id = %s
                """,
                (state, actor, connection_id),
            )
            return int(cur.rowcount or 0)

    def save_manifest_attestation(self, row: dict) -> None:
        """**사람이 대조했을 때의 hash** 를 적는다.

        신원 검사와 다른 칸이다(§6.2). 하나는 「이 토큰이 누구인가」 이고 다른
        하나는 「그 앱이 무슨 권한을 갖고 있나」 다 — 합치면 토큰만 맞고 스코프가
        빠진 앱이 「확인됨」 으로 보인다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE bot_connection
                   SET manifest_id = %(manifest_id)s,
                       manifest_attested_sha256 = %(sha256)s,
                       manifest_attested_at = now(),
                       manifest_attested_by = %(actor)s,
                       updated_at = now(), updated_by = %(actor)s
                 WHERE id = %(id)s
                """,
                row,
            )

    # -- Master 내부 호출 라우트 ----------------------------------------
    def routes(self, workspace: str = "") -> list[dict]:
        """라우트와 **배정 여부**를 함께.

        배정(`specialist_workspace`)이 없는 라우트 행은 「그 봇이 여기 없다」 는
        뜻이다. 상태만 보면 `disabled` 와 구분되지 않는데, 사람이 할 일은 다르다 —
        하나는 켜면 되고 하나는 먼저 배정해야 한다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT r.specialist, r.workspace, r.route_mode, r.fallback_bot_key,
                       r.last_shadow_checked_at, r.last_shadow_result,
                       r.updated_at, r.updated_by,
                       (a.workspace IS NOT NULL) AS assigned
                  FROM specialist_route r
                  LEFT JOIN specialist_workspace a
                         ON a.specialist = r.specialist AND a.workspace = r.workspace
                 WHERE (%s = '' OR r.workspace = %s)
                 ORDER BY r.workspace, r.specialist
                """,
                (workspace, workspace),
            )
            rows = [dict(row) for row in cur.fetchall()]
            # 배정은 있는데 라우트 행이 아직 없는 경우. 행이 없는 것은 `disabled`
            # 다(§4.5) — 화면에서 빠지면 「배정했는데 안 보인다」 가 된다.
            cur.execute(
                """
                SELECT a.specialist, a.workspace
                  FROM specialist_workspace a
                  LEFT JOIN specialist_route r
                         ON r.specialist = a.specialist AND r.workspace = a.workspace
                 WHERE r.specialist IS NULL AND (%s = '' OR a.workspace = %s)
                 ORDER BY a.workspace, a.specialist
                """,
                (workspace, workspace),
            )
            rows.extend(
                {
                    "specialist": str(row["specialist"]),
                    "workspace": str(row["workspace"]),
                    "route_mode": "disabled",
                    "fallback_bot_key": "master",
                    "last_shadow_checked_at": None,
                    "last_shadow_result": "",
                    "updated_at": None,
                    "updated_by": "",
                    "assigned": True,
                }
                for row in cur.fetchall()
            )
            return rows

    def route(self, specialist: str, workspace: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"specialist-route:{workspace}",),
            )
            cur.execute(
                """
                SELECT r.route_mode, r.fallback_bot_key,
                       (a.workspace IS NOT NULL) AS assigned
                  FROM specialist_route r
                  LEFT JOIN specialist_workspace a
                         ON a.specialist = r.specialist AND a.workspace = r.workspace
                 WHERE r.specialist = %s AND r.workspace = %s
                 FOR UPDATE OF r
                """,
                (specialist, workspace),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            # 라우트 행이 없어도 배정은 있을 수 있다. 그 사실을 판단하는 쪽에
            # 넘겨야 「배정 없이 켜는」 것을 막을 수 있다.
            cur.execute(
                "SELECT 1 FROM specialist_workspace WHERE specialist = %s AND workspace = %s",
                (specialist, workspace),
            )
            if cur.fetchone() is None:
                return None
            return {"route_mode": "disabled", "fallback_bot_key": "master", "assigned": True}

    def save_route(self, row: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO specialist_route
                    (specialist, workspace, route_mode, fallback_bot_key, updated_by)
                VALUES (%(specialist)s, %(workspace)s, %(route_mode)s,
                        %(fallback_bot_key)s, %(actor)s)
                ON CONFLICT (specialist, workspace) DO UPDATE SET
                    route_mode = excluded.route_mode,
                    fallback_bot_key = excluded.fallback_bot_key,
                    updated_at = now(),
                    updated_by = excluded.updated_by
                """,
                row,
            )

    # -- 런타임(전문 봇) ------------------------------------------------
    def specialists(self) -> list[dict]:
        """전문 봇 런타임 상태. **카탈로그에 복제하지 않는다**(§4.2).

        읽어서 화면에 나란히 보여 줄 뿐이다. 두 값을 한 열에 합치면 「정체성이
        살아 있나」 와 「지금 켜져 있나」 를 구분할 수 없게 된다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT key, name, domain, adapter, state, health, version,
                       contract_version, error_code, last_checked_at
                  FROM specialist_bot
                 ORDER BY key
                """
            )
            return [dict(row) for row in cur.fetchall()]

    # -- 옛 표(비교용) ---------------------------------------------------
    def legacy_services(self, workspace: str = "") -> list[dict]:
        """이관 전 표. **비교 도구 전용**이다(§5.5).

        새 화면이 이 값을 쓰지 않는다 — 쓰면 정본이 둘이 된다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT s.workspace, s.service, s.state, s.team_id, s.bot_user_id,
                       s.identity_ok,
                       max(sec.mask) FILTER (WHERE sec.kind = 'bot') AS bot_mask,
                       max(sec.mask) FILTER (WHERE sec.kind = 'app') AS app_mask,
                       count(sec.kind) AS token_count
                  FROM workspace_service s
                  LEFT JOIN workspace_service_secret sec
                         ON sec.workspace = s.workspace AND sec.service = s.service
                 WHERE (%s = '' OR s.workspace = %s)
                 GROUP BY s.workspace, s.service, s.state, s.team_id, s.bot_user_id,
                          s.identity_ok
                 ORDER BY s.workspace, s.service
                """,
                (workspace, workspace),
            )
            return [dict(row) for row in cur.fetchall()]

    # -- 감사 ------------------------------------------------------------
    def audit(self, workspace: str, limit: int) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT at, actor, subject, workspace, field, old_value, new_value,
                       reason
                  FROM archive_config_audit
                 WHERE subject = ANY(%s) AND (%s = '' OR workspace = %s)
                 ORDER BY at DESC
                 LIMIT %s
                """,
                (list(AUDIT_SUBJECTS), workspace, workspace, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def add_audit(self, row: dict) -> None:
        """설정 변경은 **지워지지 않는 기록**으로 남는다.

        누가 언제 무엇을 바꿨는지 없으면, 사고가 났을 때 범위를 정할 수 없다.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archive_config_audit
                    (actor, subject, workspace, channel_id, field, old_value,
                     new_value, reason)
                VALUES (%(actor)s, %(subject)s, %(workspace)s, '', %(field)s,
                        %(old_value)s, %(new_value)s, %(reason)s)
                """,
                row,
            )


def default_repo() -> PostgresBotRepo:
    return PostgresBotRepo()
