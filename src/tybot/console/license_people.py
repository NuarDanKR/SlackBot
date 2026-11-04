"""라이선스 보유자 명단 — 「어느 워크스페이스 라이선스를 누가 쓰고 있나」.

콘솔 > 운영 > 라이선스 현황에서 워크스페이스 줄을 펼치면 나온다. 스키마:
`deploy/sql/license_member_schema.sql`

## 명단이 어디서 오나
| 워크스페이스 | 명단 |
|---|---|
| 봇 토큰이 있는 연동 워크스페이스 | Slack `users.list` (활성 수를 셀 때 읽은 그 목록, 캐시 공유) |
| 봇 토큰이 없는 연동 워크스페이스 | 사람이 사번으로 적은 `license_member` |
| 직접 추가한 워크스페이스 | 사람이 사번으로 적은 `license_member` |

Slack 계정을 사번에 잇는 순서: `user_identity`(워크스페이스·Slack 사용자) → 없으면 Slack
프로필 이메일과 `employee.email`. 퇴직자도 잇는다 — 「퇴직했는데 계정이 살아 있다」 를
찾는 것이 이 화면의 목적 하나다.

## 회수 기록
「뺀다」 는 언제나 사유와 함께 `license_release` 에 남는다. 직접 입력 명단은 기록 후 명단에서
빠지고, Slack 명단은 기록만 남아 Slack 에서 비활성화될 때까지 「대기」 로 보인다.

## 하지 않는 것
- Slack 에 쓰지 않는다(계정을 비활성화하지 않는다). 회수는 사람이 Slack 관리자 화면에서 한다
- 토큰이 있는 워크스페이스에는 명단을 적지 못하게 한다 — 출처가 둘이면 어느 쪽이 맞는지 모른다
"""
from __future__ import annotations

import re

from . import license_store
from .workspace_store import WorkspaceStoreError, _connect

MAX_ADD = 300
_EMP = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,39}$")

_EMP_COLS = """e.emp_no, e.name AS emp_name, e.position, e.active AS emp_active,
               coalesce(o.name, e.org_code) AS org_name"""


class LicensePeopleError(RuntimeError):
    """문자열은 화면에 그대로 보여 줄 수 있는 문장이다."""


def _db(connect):
    try:
        return (connect or _connect)()
    except WorkspaceStoreError as exc:
        raise LicensePeopleError(str(exc)) from exc


def parse_target(target: str) -> tuple[str | None, int | None]:
    """`ws:<키>` 또는 `id:<번호>`."""
    kind, _, value = str(target or "").partition(":")
    if kind == "ws" and re.fullmatch(r"[a-z][a-z0-9-]{1,23}", value):
        return value, None
    if kind == "id" and value.isdigit() and int(value) > 0:
        return None, int(value)
    raise LicensePeopleError("대상 워크스페이스를 알 수 없습니다.")


def listed_counts(*, connect=None) -> dict[str, int]:
    """직접 적은 명단 인원 — {`ws:<키>` | `id:<번호>`: 인원}. 표가 아직 없으면 빈 값."""
    try:
        with _db(connect) as conn, conn.cursor() as cur:
            cur.execute("""SELECT workspace, manual_id, count(*) AS n FROM license_member
                            GROUP BY workspace, manual_id""")
            return {(f"ws:{r['workspace']}" if r["workspace"] else f"id:{r['manual_id']}"): int(r["n"])
                    for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - 명단 표가 없거나 DB 가 잠깐 안 돼도 표는 띄운다
        return {}


def _employees(cur, members: dict[str, tuple]) -> dict[tuple[str, str], dict]:
    """(워크스페이스, Slack 사용자) → 직원 행. 매핑 표 우선, 그다음 이메일."""
    found: dict[tuple[str, str], dict] = {}
    if not members:
        return found
    cur.execute(f"""SELECT ui.workspace, ui.slack_user, {_EMP_COLS}
                      FROM user_identity ui
                      JOIN employee e ON e.emp_no = ui.emp_no
                      LEFT JOIN org_unit o ON o.code = e.org_code
                     WHERE ui.workspace = ANY(%s)""", (sorted(members),))
    for row in cur.fetchall():
        found[(row["workspace"], row["slack_user"])] = dict(row)
    emails = sorted({m.email for ws_members in members.values() for m in ws_members
                     if m.email})
    by_email: dict[str, list[dict]] = {}
    if emails:
        cur.execute(f"""SELECT lower(btrim(e.email)) AS email, {_EMP_COLS}
                          FROM employee e LEFT JOIN org_unit o ON o.code = e.org_code
                         WHERE lower(btrim(e.email)) = ANY(%s)""", (emails,))
        for row in cur.fetchall():
            by_email.setdefault(row["email"], []).append(dict(row))
    for ws, ws_members in members.items():
        for m in ws_members:
            if (ws, m.slack_user) not in found and len(by_email.get(m.email, [])) == 1:
                found[(ws, m.slack_user)] = by_email[m.email][0]
    return found


def _person(row: dict | None) -> dict:
    if not row:
        return {"empNo": None, "empName": None, "org": None, "position": None, "employeeActive": None}
    return {"empNo": row["emp_no"], "empName": row["emp_name"], "org": row["org_name"],
            "position": row["position"], "employeeActive": bool(row["emp_active"])}


def holders(
    linked: dict[str, tuple[str, str | None]],
    counts: dict[str, license_store.ActiveCount],
    manual: list[dict],
    *,
    connect=None,
) -> dict:
    """모든 워크스페이스의 라이선스 보유자. 한 줄 = (워크스페이스, 계정) 하나."""
    linked = license_store.without_excluded(linked)
    slack = {key: counts[key].members for key, (_label, token) in linked.items()
             if token and key in counts and counts[key].error is None}
    out: list[dict] = []
    with _db(connect) as conn, conn.cursor() as cur:
        employees = _employees(cur, slack)
        for key, members in slack.items():
            label = linked[key][0]
            for m in members:
                out.append({"uid": f"ws:{key}", "label": label, "source": "slack", "memberId": None,
                            "slackUser": m.slack_user, "slackName": m.name, "email": m.email,
                            "guest": m.guest, **_person(employees.get((key, m.slack_user)))})
        try:
            cur.execute("""SELECT id, uid, label, source, slack_user, emp_no, person_name, org_name,
                                  reason, released_by, released_at, cancelled_at, cancelled_by
                             FROM license_release ORDER BY released_at DESC, id DESC LIMIT 1000""")
            released = [dict(r) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001 - 스키마 적용 전
            conn.rollback()
            released = []
        try:
            cur.execute(f"""SELECT lm.id, lm.workspace, lm.manual_id, lm.added_by, lm.added_at,
                                   e.email, {_EMP_COLS}
                              FROM license_member lm
                              JOIN employee e ON e.emp_no = lm.emp_no
                              LEFT JOIN org_unit o ON o.code = e.org_code
                             ORDER BY lm.id""")
            listed = cur.fetchall()
        except Exception:  # noqa: BLE001 - 스키마 적용 전이면 직접 명단만 비어 있다
            conn.rollback()
            listed = []
    labels = {f"ws:{key}": label for key, (label, token) in linked.items() if not token}
    labels.update({f"id:{int(row['id'])}": str(row["label"]) for row in manual})
    for row in listed:
        uid = f"ws:{row['workspace']}" if row["workspace"] else f"id:{row['manual_id']}"
        if uid not in labels:
            continue  # 토큰이 생긴 워크스페이스 — 이제 Slack 이 정답이다
        out.append({"uid": uid, "label": labels[uid], "source": "manual", "memberId": int(row["id"]),
                    "slackUser": None, "slackName": None, "email": (row["email"] or "").lower(),
                    "guest": False, "addedBy": row["added_by"],
                    "addedAt": row["added_at"].isoformat() if row["added_at"] else None,
                    **_person(row)})
    open_release = {(r["uid"], r["slack_user"]): r for r in released
                    if r["source"] == "slack" and r["cancelled_at"] is None}
    live = {(h["uid"], h["slackUser"]) for h in out if h["source"] == "slack"}
    for h in out:
        r = open_release.get((h["uid"], h["slackUser"])) if h["source"] == "slack" else None
        h["release"] = _release_json(r) if r else None
    history = []
    for r in released:
        item = _release_json(r)
        if r["cancelled_at"] is not None:
            item["status"] = "cancelled"
        elif r["source"] == "manual":
            item["status"] = "done"
        elif (r["uid"], r["slack_user"]) in live:
            item["status"] = "pending"          # Slack 에서 아직 활성
        elif r["uid"].startswith("ws:") and r["uid"][3:] in slack:
            item["status"] = "done"             # Slack 명단에서 사라졌다
        else:
            item["status"] = "unknown"          # 그 워크스페이스를 지금 못 읽었다
        history.append(item)
    out.sort(key=lambda h: (h["label"], h["empName"] or h["slackName"] or ""))
    unread = sorted(linked[key][0] for key, (_l, token) in linked.items()
                    if token and key not in slack)
    return {"holders": out, "unread": unread, "releases": history}


def _release_json(r: dict) -> dict:
    return {"id": int(r["id"]), "uid": r["uid"], "label": r["label"], "source": r["source"],
            "slackUser": r["slack_user"], "empNo": r["emp_no"], "name": r["person_name"],
            "org": r["org_name"], "reason": r["reason"], "releasedBy": r["released_by"],
            "releasedAt": r["released_at"].isoformat() if r["released_at"] else None,
            "cancelledAt": r["cancelled_at"].isoformat() if r.get("cancelled_at") else None,
            "cancelledBy": r.get("cancelled_by")}


def add(
    target: str,
    emp_nos: list[str],
    *,
    actor: str,
    linked: dict[str, tuple[str, str | None]],
    connect=None,
) -> dict:
    """사번 여러 개를 한 번에 명단에 넣는다. 있으면 건너뛰고, 없거나 퇴직이면 알려 준다."""
    workspace, manual_id = parse_target(target)
    if workspace is not None:
        if workspace not in linked:
            raise LicensePeopleError("Slack 에 연동되지 않은 워크스페이스입니다.")
        if linked[workspace][1]:
            raise LicensePeopleError("Slack 에서 명단을 읽는 워크스페이스라 직접 추가할 수 없습니다.")
    wanted: list[str] = []
    for raw in emp_nos:
        value = str(raw or "").strip()
        if not value:
            continue
        if not _EMP.fullmatch(value):
            raise LicensePeopleError(f"사번 형식이 아닙니다: {value[:40]}")
        if value.upper() not in {w.upper() for w in wanted}:
            wanted.append(value)
    if not wanted or len(wanted) > MAX_ADD:
        raise LicensePeopleError(f"사번을 1~{MAX_ADD}개 입력해 주세요.")
    result = {"added": [], "already": [], "notFound": [], "inactive": []}
    with _db(connect) as conn, conn.cursor() as cur:
        if manual_id is not None:
            cur.execute("SELECT 1 FROM slack_license_manual WHERE id = %s", (manual_id,))
            if not cur.fetchone():
                raise LicensePeopleError("이미 삭제된 워크스페이스입니다. 화면을 새로 고쳐 주세요.")
        for value in wanted:
            cur.execute("SELECT emp_no, active FROM employee WHERE upper(emp_no) = upper(%s)", (value,))
            rows = cur.fetchall()
            if len(rows) != 1:
                result["notFound"].append(value)
                continue
            emp_no = rows[0]["emp_no"]
            if not rows[0]["active"]:
                result["inactive"].append(emp_no)
                continue
            cur.execute("""INSERT INTO license_member (workspace, manual_id, emp_no, added_by)
                           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                        (workspace, manual_id, emp_no, actor.strip().lower()))
            (result["added"] if cur.fetchone() else result["already"]).append(emp_no)
    return result


def _reason(reason: str) -> str:
    text = str(reason or "").strip()
    if not text or len(text) > 500:
        raise LicensePeopleError("회수 사유를 1~500자로 적어 주세요.")
    return text


def release(holder: dict, *, reason: str, actor: str, connect=None) -> dict:
    """보유자 한 명을 회수 처리한다. `holder` 는 `holders()` 가 돌려준 한 줄이다.

    직접 입력 명단이면 기록과 함께 명단에서 뺀다(한 트랜잭션). Slack 명단이면 기록만 남긴다.
    """
    text = _reason(reason)
    who = actor.strip().lower()
    row = (holder["uid"], holder["label"], holder["source"], holder.get("slackUser"),
           holder.get("empNo"), holder.get("empName") or holder.get("slackName") or "",
           holder.get("org") or "", text, who)
    with _db(connect) as conn, conn.cursor() as cur:
        if holder["source"] == "manual":
            cur.execute("DELETE FROM license_member WHERE id = %s RETURNING id", (holder["memberId"],))
            if not cur.fetchone():
                raise LicensePeopleError("이미 명단에서 빠진 사람입니다. 화면을 새로 고쳐 주세요.")
        try:
            cur.execute("""INSERT INTO license_release (uid, label, source, slack_user, emp_no,
                                person_name, org_name, reason, released_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", row)
        except Exception as exc:
            if "license_release_one_open" in str(exc):
                raise LicensePeopleError("이미 회수 처리한 계정입니다.") from exc
            raise
        return {"id": int(cur.fetchone()["id"]), "source": holder["source"]}


def cancel(release_id: int, *, actor: str, connect=None) -> dict:
    """Slack 명단의 회수 처리를 취소한다. 직접 입력 명단은 사번을 다시 추가하면 된다."""
    with _db(connect) as conn, conn.cursor() as cur:
        cur.execute("""UPDATE license_release SET cancelled_at = now(), cancelled_by = %s
                        WHERE id = %s AND source = 'slack' AND cancelled_at IS NULL
                    RETURNING id, uid, emp_no, slack_user""", (actor.strip().lower(), int(release_id)))
        row = cur.fetchone()
    if not row:
        raise LicensePeopleError("취소할 수 있는 회수 기록이 아닙니다.")
    return {"id": int(row["id"]), "uid": row["uid"], "empNo": row["emp_no"]}


__all__ = ["LicensePeopleError", "add", "cancel", "holders", "listed_counts", "parse_target", "release"]
