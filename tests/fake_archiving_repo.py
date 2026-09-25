"""시험용 가짜 저장소 — **커서를 흉내 내지 않는다.**

결정: 2026-09-25 오너 §3(저장소 경계 분리, 시험은 fake DB).

전에는 커서를 흉내 내어 「어떤 SQL 이 나갔나」 를 봤다. 그러면 시험이 규칙이
아니라 **문장 모양**을 지키게 되고, SQL 을 손보면 규칙이 그대로여도 깨진다.
반대로 규칙이 바뀌어도 SQL 이 같으면 통과한다.

여기는 저장소가 **무엇을 들고 있나**를 흉내 낸다. 그래서 시험은 「바꾼 뒤 상태가
무엇인가」 를 본다. SQL 이 맞는지는 격리 DB 검증이 따로 본다
(`tests/test_schema_isolated_db.py`).
"""

from __future__ import annotations


class FakeArchivingRepo:
    """`ArchivingRepo` 프로토콜의 메모리 구현."""

    def __init__(self) -> None:
        self.channel_rows: dict[tuple[str, str], dict] = {}
        self.flag_rows: dict[tuple[str, str, str], dict] = {}
        self.retention_rows: dict[str, dict] = {}
        self.audit_rows: list[dict] = []
        self.service_rows: list[dict] = []
        #: 잠금을 잡았는지. 진짜 저장소는 좌표별로 잡는다.
        self.locked: list[tuple[str, str]] = []

    # -- 서비스 ----------------------------------------------------------
    def services(self, workspace: str) -> list[dict]:
        return [dict(row) for row in self.service_rows]

    # -- 채널 모드 ------------------------------------------------------
    def channel_state(self, workspace: str, channel_id: str) -> dict | None:
        self.locked.append((workspace, channel_id))
        row = self.channel_rows.get((workspace, channel_id))
        return dict(row) if row else None

    def save_channel_state(self, row: dict) -> None:
        key = (row["workspace"], row["channel_id"])
        self.channel_rows[key] = dict(row)

    def channels(self, workspace: str) -> list[dict]:
        return [
            dict(row) for (ws, _), row in sorted(self.channel_rows.items())
            if ws == workspace
        ]

    # -- 기능 스위치 ----------------------------------------------------
    def flags(self, workspace: str, channel_ids: list[str]) -> list[dict]:
        wanted = set(channel_ids)
        return [
            dict(row)
            for (_, scope, scope_key), row in sorted(self.flag_rows.items())
            if scope == "global"
            or (scope == "workspace" and scope_key == workspace)
            or (scope == "channel" and scope_key in wanted)
        ]

    def flag(self, name: str, scope: str, scope_key: str) -> dict | None:
        row = self.flag_rows.get((name, scope, scope_key))
        return dict(row) if row else None

    def save_flag(self, row: dict) -> None:
        self.flag_rows[(row["name"], row["scope"], row["scope_key"])] = dict(row)

    # -- 보존 정책 ------------------------------------------------------
    def retention(self) -> list[dict]:
        return [dict(row) for _, row in sorted(self.retention_rows.items())]

    def retention_row(self, name: str) -> dict | None:
        row = self.retention_rows.get(name)
        return dict(row) if row else None

    def save_retention(self, name: str, days: int | None, approved_by: str) -> int:
        """**없는 이름을 만들지 않는다.** 진짜 저장소의 `UPDATE` 와 같은 성질이다."""
        row = self.retention_rows.get(name)
        if row is None:
            return 0
        row["retention_days"] = days
        row["approved_by"] = "" if days is None else approved_by
        return 1

    # -- 감사 ------------------------------------------------------------
    def audit(self, workspace: str, limit: int = 50) -> list[dict]:
        rows = [
            dict(row) for row in self.audit_rows
            if row["workspace"] in (workspace, "")
        ]
        return list(reversed(rows))[:limit]

    def add_audit(self, row: dict) -> None:
        self.audit_rows.append(dict(row))

    # -- 시험 편의 --------------------------------------------------------
    def given_channel(self, workspace: str, channel_id: str, mode: str,
                      owner: str = "master", cutover_ts: str = "") -> None:
        self.channel_rows[(workspace, channel_id)] = {
            "workspace": workspace, "channel_id": channel_id, "mode": mode,
            "writer_owner": owner, "cutover_ts": cutover_ts, "updated_by": "seed",
        }

    def given_flag(self, name: str, enabled: bool, scope: str = "global",
                   scope_key: str = "") -> None:
        self.flag_rows[(name, scope, scope_key)] = {
            "name": name, "scope": scope, "scope_key": scope_key,
            "enabled": enabled, "updated_by": "seed",
        }

    def given_retention(self, name: str, days: int | None = None) -> None:
        self.retention_rows[name] = {
            "name": name, "retention_days": days, "approved_by": "", "approved_at": None,
            "description": "",
        }
