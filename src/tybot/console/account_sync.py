"""콘솔 계정 자동 관리 — Slack 을 쓰는 재직자는 게스트 계정, Slack 이 지워지거나 퇴직하면 멈춤.

표: `deploy/sql/console_account_sync_schema.sql` (규칙 설명도 거기 있다)
실행: 콘솔 「계정 동기화」(미리 보기 → 적용) · `python -m tybot.console.account_sync [--apply]`

## 한 회차
1. 연동 워크스페이스마다 Slack `users.list` 로 살아 있는 사람(삭제·봇·앱 제외)을 읽는다
2. `slack_presence` 를 갱신한다 — 읽은 워크스페이스만. 못 읽은 곳은 이전 값을 둔다
3. 아직 사번이 없는 Slack 사람을 이메일로 잇는다(`identity` 와 **같은 SQL**)
4. 재직 중 + 살아 있는 Slack 계정이 하나라도 있는 사번 → 게스트 계정(없으면 만들고,
   멈춰 있으면 되살리고, 담당 워크스페이스를 Slack 워크스페이스와 맞춘다)
5. 동기화가 만든 계정 중 대상에서 빠진 것 → 멈춤(퇴직 / Slack 없음)

미리 보기는 **같은 코드를 끝까지 돌리고 되돌린다.** 미리 보기와 적용이 다른 길을 타면
미리 본 숫자를 믿을 수 없다.

## 하지 않는 것
- 사람이 만든 계정(관리자·개발자 포함)은 만들거나 멈추거나 역할을 바꾸지 않는다.
  퇴직자면 `manualRetired` 경고로만 알린다
- 계정을 지우지 않는다
- Slack 을 못 읽은 워크스페이스가 걸린 사람은 「Slack 없음」 으로 멈추지 않는다
  (퇴직은 Slack 과 무관하므로 멈춘다)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .. import identity

logger = logging.getLogger("tybot.console.account_sync")

MANAGED = "slack_sync"
ROLE = "guest"


class AccountSyncError(RuntimeError):
    """동기화를 시작하지 못했다. 문자열은 코드다."""


@dataclass
class SlackSnapshot:
    """워크스페이스별 살아 있는 Slack 사람. {ws: {slack_user: email}}."""

    members: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 읽지 못한 워크스페이스와 사유 코드(토큰 없음 · Slack 오류 이름)
    unread: dict[str, str] = field(default_factory=dict)


def _live_members(client) -> dict[str, str]:
    out: dict[str, str] = {}
    cursor = None
    while True:
        page = client.users_list(limit=200, cursor=cursor) if cursor else client.users_list(limit=200)
        for member in (page or {}).get("members") or []:
            uid = str(member.get("id") or "")
            if (not uid or uid == "USLACKBOT" or member.get("deleted")
                    or member.get("is_bot") or member.get("is_app_user")):
                continue
            out[uid] = str((member.get("profile") or {}).get("email") or "").strip()
        cursor = (((page or {}).get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            return out


def fetch_slack(tokens: dict[str, str | None], client_factory=None) -> SlackSnapshot:
    """워크스페이스마다 한 번씩 읽는다. 실패는 그 워크스페이스만 `unread` 로 적는다."""
    if client_factory is None:
        from slack_sdk import WebClient

        client_factory = WebClient
    snap = SlackSnapshot()
    for ws, token in sorted(tokens.items()):
        if not token:
            snap.unread[ws] = "no_token"
            continue
        try:
            snap.members[ws] = _live_members(client_factory(token=token, timeout=20))
        except Exception as exc:  # noqa: BLE001 - 한 워크스페이스 실패로 전체를 멈추지 않는다
            response = getattr(exc, "response", None)
            code = ""
            try:
                code = str(response.get("error") or "") if response is not None else ""
            except Exception:  # noqa: BLE001
                code = ""
            snap.unread[ws] = code or type(exc).__name__
            logger.warning("계정 동기화 Slack 조회 실패 — workspace=%s error=%s", ws, snap.unread[ws])
    return snap


def _person(row: dict, workspaces=()) -> dict:
    return {"empNo": row.get("emp_no"), "name": row.get("name"), "email": row.get("email"),
            "workspaces": sorted(workspaces)}


def sync(conn, snap: SlackSnapshot, *, actor: str, dry_run: bool) -> dict:
    """한 회차. `conn` 은 autocommit 이 아닌 연결 — 미리 보기면 끝에서 되돌린다."""
    read_ws = sorted(snap.members)
    result: dict = {
        "dryRun": dry_run,
        "readWorkspaces": read_ws,
        "unreadWorkspaces": dict(sorted(snap.unread.items())),
        "linked": 0,
        "created": [], "reactivated": [], "stopped": [], "workspacesChanged": [],
        "protected": [], "manualRetired": [],
    }
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('console_account_sync'))")
        # 사람 손 계정 변경(account_store)과 겹치지 않게 같은 자물쇠도 잡는다.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('console_user_admin_guard'))")

        # 2. 살아 있는가
        for ws in read_ws:
            live = sorted(snap.members[ws])
            cur.execute(
                """UPDATE slack_presence SET active = false, checked_at = now()
                    WHERE workspace = %s AND active AND NOT (slack_user = ANY(%s))""",
                (ws, live),
            )
            cur.execute(
                """INSERT INTO slack_presence (workspace, slack_user, active)
                   SELECT %s, u, true FROM unnest(%s::text[]) AS u
                   ON CONFLICT (workspace, slack_user)
                   DO UPDATE SET active = true, checked_at = now()""",
                (ws, live),
            )

        # 3. 사번 잇기 — identity 와 같은 SQL
        for ws in read_ws:
            cur.execute("SELECT slack_user FROM user_identity WHERE workspace = %s AND emp_no IS NOT NULL",
                        (ws,))
            known = {r["slack_user"] for r in cur.fetchall()}
            for uid, email in snap.members[ws].items():
                if uid in known or not email:
                    continue
                cur.execute(identity.EMPLOYEE_BY_EMAIL_SQL, {"email": email})
                rows = cur.fetchall()
                if len(rows) != 1:
                    continue
                cur.execute(identity.UPSERT_SQL,
                            {"workspace": ws, "slack_user": uid, "emp_no": rows[0]["emp_no"]})
                result["linked"] += cur.rowcount

        # 4·5. 대상 계산
        cur.execute(
            """SELECT ui.emp_no, array_agg(DISTINCT ui.workspace) AS ws
                 FROM user_identity ui
                 JOIN slack_presence sp ON sp.workspace = ui.workspace
                                       AND sp.slack_user = ui.slack_user AND sp.active
                WHERE ui.emp_no IS NOT NULL AND ui.workspace = ANY(%s)
                GROUP BY ui.emp_no""",
            (read_ws,),
        )
        live_ws = {r["emp_no"]: set(r["ws"]) for r in cur.fetchall()}
        cur.execute(
            """SELECT emp_no, array_agg(DISTINCT workspace) AS ws FROM user_identity
                WHERE emp_no IS NOT NULL AND NOT (workspace = ANY(%s)) GROUP BY emp_no""",
            (read_ws,),
        )
        unread_ws = {r["emp_no"]: set(r["ws"]) for r in cur.fetchall()}
        cur.execute(
            """SELECT emp_no, name, lower(btrim(email)) AS email, active FROM employee
                WHERE email IS NOT NULL AND btrim(email) <> ''"""
        )
        employees = {r["emp_no"]: dict(r) for r in cur.fetchall()}
        by_email = {}
        for emp in employees.values():
            by_email.setdefault(emp["email"], []).append(emp)
        cur.execute("SELECT key FROM workspace")
        ws_keys = {r["key"] for r in cur.fetchall()}
        cur.execute(
            """SELECT u.email, u.name, u.role, u.active, u.managed_by, u.emp_no,
                      coalesce(array_agg(cuw.workspace) FILTER (WHERE cuw.workspace IS NOT NULL),
                               '{}') AS ws
                 FROM console_user u
                 LEFT JOIN console_user_workspace cuw ON cuw.email = u.email
                GROUP BY u.email"""
        )
        accounts = {r["email"].lower(): dict(r) for r in cur.fetchall()}

        def set_ws(email: str, wanted: set[str]) -> None:
            cur.execute("DELETE FROM console_user_workspace WHERE email = %s AND NOT (workspace = ANY(%s))",
                        (email, sorted(wanted)))
            cur.execute(
                """INSERT INTO console_user_workspace (email, workspace)
                   SELECT %s, w FROM unnest(%s::text[]) AS w ON CONFLICT DO NOTHING""",
                (email, sorted(wanted)),
            )

        covered: set[str] = set()
        for emp_no, ws in sorted(live_ws.items()):
            emp = employees.get(emp_no)
            if not emp or not emp["active"] or len(by_email.get(emp["email"], [])) != 1:
                continue  # 퇴직 · 이메일 없음 · 같은 이메일 둘 → 만들지 않는다
            email = emp["email"]
            wanted = ws & ws_keys
            acc = accounts.get(email)
            covered.add(email)
            if acc is None:
                cur.execute(
                    """INSERT INTO console_user
                         (email, name, password_hash, role, active, managed_by, emp_no)
                       VALUES (%s, %s, '', %s, true, %s, %s)""",
                    (email, emp["name"], ROLE, MANAGED, emp_no),
                )
                set_ws(email, wanted)
                result["created"].append(_person(emp, wanted))
            elif acc["managed_by"] != MANAGED:
                continue  # 사람 손 계정
            else:
                if not acc["active"]:
                    cur.execute(
                        """UPDATE console_user SET active = true, stopped_at = NULL,
                                  stop_reason = NULL, emp_no = %s WHERE email = %s""",
                        (emp_no, acc["email"]),
                    )
                    result["reactivated"].append(_person(emp, wanted))
                elif set(acc["ws"]) != wanted:
                    result["workspacesChanged"].append(_person(emp, wanted))
                set_ws(acc["email"], wanted)

        for email, acc in sorted(accounts.items()):
            emp = employees.get(acc["emp_no"]) if acc["emp_no"] else None
            if emp is None:
                matches = by_email.get(email, [])
                emp = matches[0] if len(matches) == 1 else None
            if acc["managed_by"] != MANAGED:
                if acc["active"] and emp is not None and not emp["active"]:
                    result["manualRetired"].append(
                        {**_person(emp), "email": acc["email"], "role": acc["role"]})
                continue
            if email in covered or not acc["active"]:
                continue
            retired = emp is None or not emp["active"]
            who = {**_person(emp or {"name": acc["name"], "emp_no": acc["emp_no"]}),
                   "email": acc["email"]}
            if not retired and unread_ws.get(emp["emp_no"]):
                # Slack 을 못 읽은 워크스페이스에 이 사람이 있다 — 「없다」 로 읽지 않는다.
                result["protected"].append({**who, "workspaces": sorted(unread_ws[emp["emp_no"]])})
                continue
            reason = "retired" if retired else "slack_gone"
            cur.execute(
                """UPDATE console_user SET active = false, stopped_at = now(), stop_reason = %s
                    WHERE email = %s AND managed_by = %s""",
                (reason, acc["email"], MANAGED),
            )
            result["stopped"].append({**who, "reason": reason})

        counts = {k: len(result[k]) for k in
                  ("created", "reactivated", "stopped", "workspacesChanged", "protected", "manualRetired")}
        result["counts"] = {**counts, "linked": result["linked"]}
        if not dry_run:
            cur.execute(
                """INSERT INTO account_sync_run
                     (actor, created, reactivated, stopped, workspaces_changed, linked,
                      failed_workspaces, stops_skipped, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   RETURNING id, started_at""",
                (actor, counts["created"], counts["reactivated"], counts["stopped"],
                 counts["workspacesChanged"], result["linked"], sorted(snap.unread),
                 counts["protected"] > 0,
                 _json({"unread": snap.unread,
                        "stopped": [[p["empNo"], p["reason"]] for p in result["stopped"]],
                        "created": [p["empNo"] for p in result["created"]],
                        "reactivated": [p["empNo"] for p in result["reactivated"]]})),
            )
            row = cur.fetchone()
            result["runId"] = row["id"]
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    logger.info("콘솔 계정 동기화(%s) — %s", "미리 보기" if dry_run else "적용", result["counts"])
    return result


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise AccountSyncError("database_unavailable")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise AccountSyncError("database_unavailable") from exc


def run(*, actor: str, dry_run: bool, connect=None, tokens=None, client_factory=None) -> dict:
    """Slack 을 읽고 한 회차를 돈다. Slack 은 DB 트랜잭션 밖에서 먼저 읽는다."""
    if tokens is None:
        from .license_store import linked_workspaces

        tokens = {ws: token for ws, (_label, token) in linked_workspaces().items()}
    snap = fetch_slack(tokens, client_factory)
    if not snap.members:
        raise AccountSyncError("slack_unavailable")
    try:
        with (connect or _connect)() as conn:
            return sync(conn, snap, actor=actor, dry_run=dry_run)
    except AccountSyncError:
        raise
    except Exception as exc:
        logger.exception("콘솔 계정 동기화 실패")
        raise AccountSyncError("database_unavailable") from exc


def recent_runs(limit: int = 20, *, connect=None) -> list[dict]:
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, started_at, actor, created, reactivated, stopped,
                          workspaces_changed, linked, failed_workspaces, stops_skipped
                     FROM account_sync_run ORDER BY started_at DESC LIMIT %s""",
                (limit,),
            )
            return [
                {"id": r["id"], "startedAt": r["started_at"], "actor": r["actor"],
                 "created": r["created"], "reactivated": r["reactivated"], "stopped": r["stopped"],
                 "workspacesChanged": r["workspaces_changed"], "linked": r["linked"],
                 "failedWorkspaces": list(r["failed_workspaces"]), "stopsSkipped": r["stops_skipped"]}
                for r in cur.fetchall()
            ]
    except AccountSyncError:
        raise
    except Exception as exc:
        raise AccountSyncError("database_unavailable") from exc


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..envfile import load_env_file

    parser = argparse.ArgumentParser(description="콘솔 계정 자동 관리(Slack → 게스트 계정)")
    parser.add_argument("--apply", action="store_true", help="실제로 반영한다. 없으면 미리 보기")
    args = parser.parse_args(argv)
    load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = run(actor="timer:tybot-account-sync", dry_run=not args.apply)
    except AccountSyncError as exc:
        print(f"동기화 실패: {exc}")
        return 1
    print(("적용" if args.apply else "미리 보기"), result["counts"],
          "읽지 못함:", result["unreadWorkspaces"] or "없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
