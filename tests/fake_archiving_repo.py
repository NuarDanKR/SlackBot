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

import time
from contextlib import contextmanager
from copy import deepcopy

#: `archive_settled_cutover_ts()` 의 `interval '60 seconds'` 와 같다.
SETTLE_SECONDS = 60


def _check_row(row: dict) -> None:
    """DB CHECK 두 개(`owner_matches`·`cutover_present`)를 흉내 낸다."""
    mode, owner = row.get("mode"), row.get("writer_owner")
    if not (
        mode == "paused"
        or (mode in {"off", "shadow"} and owner == "master")
        or (mode == "active" and owner == "archiver")
    ):
        raise ValueError(f"archive_channel_mode_owner_matches: {mode}/{owner}")
    if owner == "archiver" and not row.get("cutover_ts"):
        raise ValueError("archive_channel_mode_cutover_present")


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
        self.transaction_count = 0
        self.fail_audit = False
        #: 정착 좌표를 정하는 시계(초). 시험이 바꿔 끼운다.
        self.clock = time.time

    @contextmanager
    def transaction(self):
        before = deepcopy((
            self.channel_rows,
            self.flag_rows,
            self.retention_rows,
            self.audit_rows,
        ))
        self.transaction_count += 1
        try:
            yield self
        except Exception:
            (
                self.channel_rows,
                self.flag_rows,
                self.retention_rows,
                self.audit_rows,
            ) = before
            raise

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

    # `archiver_save_membership` 과 **같은 규칙**(deploy/sql/archiving_schema.sql).
    # 실제 SQL 은 격리 DB 시험이 따로 본다. 여기는 「바꾼 뒤 상태」 만 흉내 낸다.
    def _live_on(self) -> bool:
        row = self.flag_rows.get(("archiver_writes_live", "global", ""))
        return bool(row and row.get("enabled") is True)

    def _settled_cutover(self) -> str:
        return f"{self.clock() + SETTLE_SECONDS:.6f}"

    def save_membership(self, row: dict) -> str:
        if row["mode"] not in {"active", "paused", "shadow"}:
            raise ValueError(f"membership sync cannot request mode {row['mode']}")
        if row["membership"] not in {"joined", "left"}:
            raise ValueError(f"membership sync cannot write membership {row['membership']}")
        key = (row["workspace"], row["channel_id"])
        self.locked.append(key)
        current = self.channel_rows.get(key)
        live = self._live_on()
        if current is None:
            if row["membership"] != "joined" or row["mode"] == "paused":
                raise ValueError("a new invited channel must be registered as joined/active")
            if not live:
                return "deferred"
            cutover = self._settled_cutover()
            self.channel_rows[key] = {
                "workspace": row["workspace"], "channel_id": row["channel_id"],
                "mode": "active", "writer_owner": "archiver",
                "cutover_ts": cutover, "cutover_at": "now",
                "membership": "joined", "operator_hold": False,
                "channel_name": row.get("channel_name", ""),
                "is_private": row.get("is_private", False),
                "updated_by": row.get("updated_by", ""), "last_event_at": None,
            }
            if row.get("audit"):
                self._membership_audit(row, "", "joined/active")
                self._owner_audit(row, "", f"archiver@{cutover}")
            return "active"

        before_membership = current.get("membership")
        before_mode = current.get("mode")
        before_owner = current.get("writer_owner")
        before_cutover = current.get("cutover_ts") or ""
        if row["membership"] == "left" or current.get("operator_hold") or row["mode"] == "paused":
            mode = "paused"
        elif before_owner == "archiver":
            mode = "active"
        elif live:
            mode = "active"
            fresh = self._settled_cutover()
            keep = bool(before_cutover) and float(before_cutover) > float(fresh)
            current["writer_owner"] = "archiver"
            if not keep:
                current["cutover_ts"] = fresh
                current["cutover_at"] = "now"
        else:
            mode = "off"
        current.update({
            "mode": mode, "membership": row["membership"],
            "channel_name": row.get("channel_name", ""),
            "is_private": row.get("is_private", False),
            "updated_by": row.get("updated_by", ""),
        })
        _check_row(current)
        if row.get("audit") and (before_membership != row["membership"] or before_mode != mode):
            self._membership_audit(
                row, f"{before_membership}/{before_mode}", f"{row['membership']}/{mode}",
            )
        if before_owner != current["writer_owner"]:
            self._owner_audit(
                row, f"{before_owner}@{before_cutover or '-'}",
                f"{current['writer_owner']}@{current['cutover_ts']}",
            )
        return mode

    def _membership_audit(self, row: dict, old: str, new: str) -> None:
        self.audit_rows.append({
            "actor": row["updated_by"], "subject": "channel_membership",
            "workspace": row["workspace"], "channel_id": row["channel_id"],
            "field": "membership", "old_value": old, "new_value": new,
            "reason": row["reason"],
        })

    def _owner_audit(self, row: dict, old: str, new: str) -> None:
        self.audit_rows.append({
            "actor": row["updated_by"], "subject": "channel_membership",
            "workspace": row["workspace"], "channel_id": row["channel_id"],
            "field": "writer_owner", "old_value": old, "new_value": new,
            "reason": "settle 60s after registration",
        })

    def set_operator_hold(self, row: dict) -> int:
        key = (row["workspace"], row["channel_id"])
        current = self.channel_rows.get(key)
        if current is None:
            return 0
        self.locked.append(key)
        current["operator_hold"] = row["hold"]
        if row["hold"]:
            current["mode"] = "paused"
        elif current.get("membership") == "joined":
            if current.get("writer_owner") == "archiver":
                current["mode"] = "active"
            elif self._live_on():
                current.update({
                    "mode": "active", "writer_owner": "archiver",
                    "cutover_ts": self._settled_cutover(), "cutover_at": "now",
                })
            else:
                current["mode"] = "off"
        _check_row(current)
        current["updated_by"] = row["actor"]
        return 1

    def mark_channel_event(self, workspace: str, channel_id: str) -> bool:
        current = self.channel_rows.get((workspace, channel_id))
        if not current or current.get("membership") != "joined":
            return False
        if current.get("operator_hold") or current.get("mode") != "active":
            return False
        current["last_event_at"] = "now"
        return True

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
        if self.fail_audit:
            raise RuntimeError("audit failed")
        self.audit_rows.append(dict(row))

    # -- 시험 편의 --------------------------------------------------------
    def given_channel(self, workspace: str, channel_id: str, mode: str,
                      owner: str = "master", cutover_ts: str = "",
                      membership: str = "unknown", operator_hold: bool = False) -> None:
        self.channel_rows[(workspace, channel_id)] = {
            "workspace": workspace, "channel_id": channel_id, "mode": mode,
            "writer_owner": owner, "cutover_ts": cutover_ts, "updated_by": "seed",
            # 초대 기반 수집(2026-09-29). 멤버십을 모르는 것과 참여 중인 것은 다르다.
            "membership": membership, "operator_hold": operator_hold,
            "channel_name": "", "is_private": False, "last_event_at": None,
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
