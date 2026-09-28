"""시험용 가짜 봇 저장소 — **커서를 흉내 내지 않는다.**

`fake_archiving_repo` 와 같은 이유다. 저장소가 **무엇을 들고 있나**를 흉내 내어,
시험이 「바꾼 뒤 상태가 무엇인가」 를 보게 한다. SQL 이 맞는지는 격리 DB 검증이
따로 본다(`tests/test_schema_isolated_db.py`).
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy


class FakeBotRepo:
    """`BotRepo` 프로토콜의 메모리 구현."""

    def __init__(self) -> None:
        self.catalog_rows: list[dict] = []
        self.connection_rows: list[dict] = []
        self.secret_rows: dict[tuple[int, str], dict] = {}
        self.route_rows: dict[tuple[str, str], dict] = {}
        self.assignments: set[tuple[str, str]] = set()
        self.specialist_rows: list[dict] = []
        self.legacy_rows: list[dict] = []
        self.audit_rows: list[dict] = []
        self.locked: list[str] = []
        self.transaction_count = 0
        self._next_id = 1

    # -- 준비 -----------------------------------------------------------
    def given_bot(self, key: str, **over) -> None:
        self.catalog_rows.append({
            "key": key, "display_name": over.get("display_name", key.title()),
            "category": over.get("category", "specialist"),
            "owner_team": over.get("owner_team", ""),
            "slack_connectable": over.get("slack_connectable", False),
            "internally_invokable": over.get("internally_invokable", True),
            "state": over.get("state", "active"),
        })

    def given_connection(self, workspace: str, bot_key: str, **over) -> int:
        row = {
            "id": self._next_id, "workspace": workspace, "bot_key": bot_key,
            "connector_type": "slack_socket", "state": over.get("state", "draft"),
            "team_id": over.get("team_id", ""), "bot_user_id": over.get("bot_user_id", ""),
            "identity_ok": over.get("identity_ok"), "identity_error": "",
            "identity_checked_at": None, "manifest_id": over.get("manifest_id", ""),
            "manifest_attested_sha256": "", "manifest_attested_at": None,
            "manifest_attested_by": "", "last_heartbeat_at": None, "last_event_at": None,
            "runtime_version": "", "runtime_error": "", "note": over.get("note", ""),
            "updated_at": None, "updated_by": "",
            "bot_mask": over.get("bot_mask", ""), "app_mask": over.get("app_mask", ""),
            "token_count": over.get("token_count", 0),
        }
        self.connection_rows.append(row)
        self._next_id += 1
        return int(row["id"])

    def given_assignment(self, specialist: str, workspace: str) -> None:
        self.assignments.add((specialist, workspace))

    def given_route(self, specialist: str, workspace: str, mode: str) -> None:
        self.route_rows[(specialist, workspace)] = {
            "specialist": specialist, "workspace": workspace, "route_mode": mode,
            "fallback_bot_key": "master", "last_shadow_checked_at": None,
            "last_shadow_result": "", "updated_at": None, "updated_by": "",
        }

    def given_specialist(self, key: str, *, state="enabled", health="ok", **over) -> None:
        self.specialist_rows.append({
            "key": key, "name": over.get("name", key), "domain": over.get("domain", "업무"),
            "adapter": over.get("adapter", "prompt"), "state": state, "health": health,
            "version": over.get("version", "1"), "contract_version": "v1",
            "error_code": "", "last_checked_at": None,
        })

    def given_legacy(self, workspace: str, service: str, **over) -> None:
        self.legacy_rows.append({
            "workspace": workspace, "service": service,
            "state": over.get("state", "enabled"), "team_id": over.get("team_id", "T1"),
            "bot_user_id": over.get("bot_user_id", "U1"), "identity_ok": True,
            "bot_mask": over.get("bot_mask", ""), "app_mask": over.get("app_mask", ""),
            "token_count": over.get("token_count", 2),
        })

    # -- 저장소 ----------------------------------------------------------
    @contextmanager
    def transaction(self):
        before = deepcopy((
            self.connection_rows, self.secret_rows, self.route_rows, self.audit_rows,
        ))
        self.transaction_count += 1
        try:
            yield self
        except Exception:
            (
                self.connection_rows, self.secret_rows, self.route_rows, self.audit_rows,
            ) = before
            raise

    def catalog(self) -> list[dict]:
        return [dict(row) for row in self.catalog_rows]

    def connections(self, workspace: str = "") -> list[dict]:
        return [
            dict(row) for row in self.connection_rows
            if not workspace or row["workspace"] == workspace
        ]

    def connection(self, workspace: str, bot_key: str) -> dict | None:
        self.locked.append(f"bot-connection:{workspace}")
        for row in self.connection_rows:
            if row["workspace"] == workspace and row["bot_key"] == bot_key:
                return dict(row)
        return None

    def save_connection(self, row: dict) -> int:
        for existing in self.connection_rows:
            if (existing["workspace"], existing["bot_key"]) == (
                row["workspace"], row["bot_key"]
            ):
                existing["note"] = row["note"]
                existing["updated_by"] = row["actor"]
                return int(existing["id"])
        return self.given_connection(
            row["workspace"], row["bot_key"], note=row["note"],
        )

    def save_secrets(self, connection_id: int, secrets: dict, actor: str) -> None:
        for kind, (ciphertext, mask) in secrets.items():
            self.secret_rows[(connection_id, kind)] = {
                "ciphertext": ciphertext, "mask": mask, "updated_by": actor,
            }
        for row in self.connection_rows:
            if row["id"] == connection_id:
                row["bot_mask"] = self.secret_rows.get(
                    (connection_id, "bot"), {}
                ).get("mask", "")
                row["app_mask"] = self.secret_rows.get(
                    (connection_id, "app"), {}
                ).get("mask", "")
                row["token_count"] = sum(
                    1 for key in self.secret_rows if key[0] == connection_id
                )

    def reset_identity(self, connection_id: int) -> None:
        for row in self.connection_rows:
            if row["id"] == connection_id:
                row["identity_ok"] = None
                row["identity_error"] = ""
                row["identity_checked_at"] = None
                if row["state"] == "enabled":
                    row["state"] = "disabled"

    def record_identity(self, row: dict) -> int:
        for existing in self.connection_rows:
            if existing["id"] == row["id"]:
                existing["team_id"] = row["team_id"]
                existing["bot_user_id"] = row["bot_user_id"]
                existing["identity_ok"] = row["ok"]
                existing["identity_error"] = row["error"]
                existing["state"] = "enabled" if row["ok"] else "disabled"
                existing["updated_by"] = row["actor"]
                return 1
        return 0

    def set_connection_state(self, connection_id: int, state: str, actor: str) -> int:
        for row in self.connection_rows:
            if row["id"] == connection_id:
                row["state"] = state
                row["updated_by"] = actor
                return 1
        return 0

    def routes(self, workspace: str = "") -> list[dict]:
        rows = [
            {**row, "assigned": (row["specialist"], row["workspace"]) in self.assignments}
            for row in self.route_rows.values()
            if not workspace or row["workspace"] == workspace
        ]
        for specialist, ws in sorted(self.assignments):
            if (specialist, ws) in self.route_rows:
                continue
            if workspace and ws != workspace:
                continue
            rows.append({
                "specialist": specialist, "workspace": ws, "route_mode": "disabled",
                "fallback_bot_key": "master", "last_shadow_checked_at": None,
                "last_shadow_result": "", "updated_at": None, "updated_by": "",
                "assigned": True,
            })
        return rows

    def route(self, specialist: str, workspace: str) -> dict | None:
        self.locked.append(f"specialist-route:{workspace}")
        row = self.route_rows.get((specialist, workspace))
        if row:
            return {
                **row, "assigned": (specialist, workspace) in self.assignments,
            }
        if (specialist, workspace) in self.assignments:
            return {"route_mode": "disabled", "fallback_bot_key": "master", "assigned": True}
        return None

    def save_route(self, row: dict) -> None:
        key = (row["specialist"], row["workspace"])
        current = self.route_rows.get(key, {
            "specialist": row["specialist"], "workspace": row["workspace"],
            "last_shadow_checked_at": None, "last_shadow_result": "", "updated_at": None,
        })
        current.update({
            "route_mode": row["route_mode"],
            "fallback_bot_key": row["fallback_bot_key"],
            "updated_by": row["actor"],
        })
        self.route_rows[key] = current

    def specialists(self) -> list[dict]:
        return [dict(row) for row in self.specialist_rows]

    def legacy_services(self, workspace: str = "") -> list[dict]:
        return [
            dict(row) for row in self.legacy_rows
            if not workspace or row["workspace"] == workspace
        ]

    def audit(self, workspace: str, limit: int) -> list[dict]:
        rows = [
            row for row in self.audit_rows
            if not workspace or row.get("workspace") == workspace
        ]
        return list(reversed(rows))[:limit]

    def add_audit(self, row: dict) -> None:
        self.audit_rows.append(dict(row))
