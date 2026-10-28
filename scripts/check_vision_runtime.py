#!/usr/bin/env python3
"""Vision 전용 **접속 계정**이 제대로 섰는지 대조한다. 읽기 전용이다.

    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_vision_runtime.py

    # 활성화 직전 — 계정이 없으면 「준비 상태」 가 아니라 실패다
    … /opt/tybot/scripts/check_vision_runtime.py --require-login

## 왜 드리프트 검사기로 부족한가

`check_schema_drift.py` 는 `tybot_vision` 그룹에 **권한이 붙었나**를 본다.
그 그룹은 NOLOGIN 이라 아무도 접속할 수 없다. 2026-10-28 실측에서 그룹의
멤버는 0 명이었고, 드리프트 검사기는 그것을 보지 않으므로 `exit 0` 이었다.

「권한 정상」 과 「그 권한을 쓸 주체가 있다」 는 다른 사실이다. 활성화 뒤에는
이 차이가 조용한 고장이 된다 — 검사기는 초록인데 worker 는 접속하지 못한다.

## 「준비 상태」 와 「실패」 를 가른다

접속 계정이 아직 없는 것은 **지금은 정상**이다. 정책이 OFF 이고 worker 도 없다.
그러나 활성화를 하러 와서 같은 상태를 보면 그것은 실패다. 둘을 같은 색으로
보고하면 운영자가 활성화를 「됐다」 로 읽는다. `--require-login` 이 그 선이다.

## 권한 목록을 여기 베끼지 않는다

`check_schema_drift` 가 선언한 계약을 그대로 가져온다. 베껴 적으면 한쪽만
고쳐지는 날이 오고, **갈라진 검사는 없는 안전을 보고한다.**

종료 코드: `0` 이상 없음 · `1` 문제 있음 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from check_schema_drift import (
    VISION_COLUMN_REQUIRED,
    VISION_REQUIRED,
    VISION_ROLE,
    VISION_TABLES,
)

from tybot.envfile import load_env_file

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_INPUT = 2

RUNTIME_ROLE = "tybot_vision_runtime"
CLEARANCE = "archiver_image_description_clearance"
APPROVAL = "archiver_image_description_approval"

#: 접속 계정이 가져서는 안 되는 속성. 하나라도 서 있으면 「전용 계정」 이 아니다.
FORBIDDEN = ("super", "createdb", "createrole", "replication", "bypassrls")

#: Vision 표에 권한이 있으면 안 되는 역할들. 여기 있는 이름만 보는 것이 아니라
#: **Vision 역할이 아닌 전부**를 보지만, 없는 역할을 물어보지 않으려고 적어 둔다.
MASTER_ROLES = ("tyslackai", "tybot", "tybot_archiver", "tybot_archiver_runtime")


@dataclass(frozen=True)
class Facts:
    """DB 에서 읽은 사실만. 판정은 :func:`evaluate` 가 한다."""

    group_exists: bool = False
    group_login: bool = True
    runtime: dict | None = None
    memberships: tuple[tuple[str, bool], ...] = ()
    foreign_grants: tuple[tuple[str, str, str], ...] = ()
    inherited: dict[tuple[str, str], bool] = field(default_factory=dict)
    clearance_table_update: bool = True
    clearance_update_columns: tuple[str, ...] = ()
    approval_table_update: bool = True
    approval_update_columns: tuple[str, ...] = ()
    public_grants: int = 0


def evaluate(facts: Facts, *, require_login: bool) -> tuple[list[str], str]:
    """문제 코드 목록과 상태(`ready`/`prepared`/`blocked`).

    그룹 쪽 문제는 접속 계정 유무와 **무관하게** 본다. 계정이 없다고 넘어가면
    그룹이 LOGIN 으로 바뀐 사고를 활성화 날까지 못 본다.
    """
    problems: list[str] = []
    if not facts.group_exists:
        problems.append("group_role_missing")
    elif facts.group_login:
        problems.append("group_role_can_login")
    for role, table, privilege in facts.foreign_grants:
        problems.append(f"foreign_grant:{role}:{table}:{privilege.lower()}")
    if facts.public_grants:
        problems.append("public_grant_present")

    if facts.runtime is None:
        if require_login:
            problems.append("runtime_role_missing")
            return problems, "blocked"
        # 정책 OFF · worker 없음. 지금은 이것이 맞는 모양이다.
        return problems, "blocked" if problems else "prepared"

    if not facts.runtime.get("login"):
        problems.append("runtime_cannot_login")
    if not facts.runtime.get("inherit"):
        # NOINHERIT 면 그룹 권한을 쓰려면 SET ROLE 이 필요하다. 코드는 그것을
        # 하지 않으므로 권한이 「있는데 안 되는」 상태가 된다.
        problems.append("runtime_does_not_inherit")
    for name in FORBIDDEN:
        if facts.runtime.get(name):
            problems.append(f"runtime_has_{name}")

    held = {name for name, _ in facts.memberships}
    if held != {VISION_ROLE}:
        problems.append("runtime_membership_mismatch")
    if any(admin for _, admin in facts.memberships):
        # ADMIN OPTION 이 있으면 접속 계정이 그룹을 **남에게 줄 수 있다.**
        problems.append("runtime_membership_admin_option")

    for table, privileges in sorted(VISION_REQUIRED.items()):
        for privilege in sorted(privileges):
            if not facts.inherited.get((table, privilege)):
                problems.append(f"not_inherited:{table}:{privilege.lower()}")

    if facts.clearance_table_update:
        problems.append("clearance_table_update_granted")
    expected = VISION_COLUMN_REQUIRED[CLEARANCE]["UPDATE"]
    actual = set(facts.clearance_update_columns)
    if actual - expected:
        problems.append("clearance_update_columns_too_wide")
    elif actual != expected:
        problems.append("clearance_update_columns_missing")
    if facts.approval_table_update:
        problems.append("approval_table_update_granted")
    expected = VISION_COLUMN_REQUIRED[APPROVAL]["UPDATE"]
    actual = set(facts.approval_update_columns)
    if actual - expected:
        problems.append("approval_update_columns_too_wide")
    elif actual != expected:
        problems.append("approval_update_columns_missing")

    return problems, "blocked" if problems else "ready"


def collect(conn) -> Facts:
    """읽기만 한다. 어떤 문장도 쓰지 않는다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,"
            "rolcreaterole,rolreplication,rolbypassrls FROM pg_roles "
            "WHERE rolname = ANY(%s)",
            ([VISION_ROLE, RUNTIME_ROLE],),
        )
        roles = {str(row["rolname"]): dict(row) for row in cur.fetchall()}
        group = roles.get(VISION_ROLE)
        raw = roles.get(RUNTIME_ROLE)
        runtime = None if raw is None else {
            "login": bool(raw["rolcanlogin"]),
            "inherit": bool(raw["rolinherit"]),
            "super": bool(raw["rolsuper"]),
            "createdb": bool(raw["rolcreatedb"]),
            "createrole": bool(raw["rolcreaterole"]),
            "replication": bool(raw["rolreplication"]),
            "bypassrls": bool(raw["rolbypassrls"]),
        }

        cur.execute(
            """SELECT g.rolname AS group_name, a.admin_option
                 FROM pg_auth_members a
                 JOIN pg_roles g ON g.oid=a.roleid
                 JOIN pg_roles m ON m.oid=a.member
                WHERE m.rolname=%s ORDER BY 1""",
            (RUNTIME_ROLE,),
        )
        memberships = tuple(
            (str(row["group_name"]), bool(row["admin_option"])) for row in cur.fetchall()
        )

        cur.execute(
            """SELECT r.rolname AS role_name, c.relname AS table_name, p.privilege
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 CROSS JOIN pg_roles r
                 CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                                         'REFERENCES','TRIGGER']) AS p(privilege)
                WHERE n.nspname='public' AND c.relkind='r'
                  AND c.relname = ANY(%s) AND r.rolname = ANY(%s)
                  AND has_table_privilege(r.oid, c.oid, p.privilege)
                ORDER BY 1,2,3""",
            (list(VISION_TABLES), list(MASTER_ROLES)),
        )
        foreign_grants = tuple(
            (str(row["role_name"]), str(row["table_name"]), str(row["privilege"]))
            for row in cur.fetchall()
        )

        cur.execute(
            """SELECT count(*) AS held
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                                         'REFERENCES','TRIGGER']) AS p(privilege)
                WHERE n.nspname='public' AND c.relkind='r'
                  AND c.relname = ANY(%s)
                  AND has_table_privilege('public', c.oid, p.privilege)""",
            (list(VISION_TABLES),),
        )
        public_grants = int(cur.fetchone()["held"])

        inherited: dict[tuple[str, str], bool] = {}
        clearance_table_update = False
        clearance_columns: tuple[str, ...] = ()
        approval_table_update = False
        approval_columns: tuple[str, ...] = ()
        if runtime is not None:
            wanted = [
                (table, privilege)
                for table, privileges in sorted(VISION_REQUIRED.items())
                for privilege in sorted(privileges)
            ]
            cur.execute(
                """SELECT w.table_name, w.privilege,
                          has_table_privilege(%s, w.table_name, w.privilege) AS allowed
                     FROM unnest(%s::text[], %s::text[]) AS w(table_name, privilege)""",
                (
                    RUNTIME_ROLE,
                    [table for table, _ in wanted],
                    [privilege for _, privilege in wanted],
                ),
            )
            inherited = {
                (str(row["table_name"]), str(row["privilege"])): bool(row["allowed"])
                for row in cur.fetchall()
            }
            cur.execute(
                "SELECT has_table_privilege(%s,%s,'UPDATE') AS allowed",
                (RUNTIME_ROLE, CLEARANCE),
            )
            clearance_table_update = bool(cur.fetchone()["allowed"])
            cur.execute(
                """SELECT a.attname
                     FROM pg_attribute a
                    WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped
                      AND has_column_privilege(%s, a.attrelid, a.attname, 'UPDATE')
                    ORDER BY a.attnum""",
                (CLEARANCE, RUNTIME_ROLE),
            )
            clearance_columns = tuple(str(row["attname"]) for row in cur.fetchall())
            cur.execute(
                "SELECT has_table_privilege(%s,%s,'UPDATE') AS allowed",
                (RUNTIME_ROLE, APPROVAL),
            )
            approval_table_update = bool(cur.fetchone()["allowed"])
            cur.execute(
                """SELECT a.attname
                     FROM pg_attribute a
                    WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped
                      AND has_column_privilege(%s, a.attrelid, a.attname, 'UPDATE')
                    ORDER BY a.attnum""",
                (APPROVAL, RUNTIME_ROLE),
            )
            approval_columns = tuple(str(row["attname"]) for row in cur.fetchall())

    return Facts(
        group_exists=group is not None,
        group_login=bool(group["rolcanlogin"]) if group else True,
        runtime=runtime,
        memberships=memberships,
        foreign_grants=foreign_grants,
        inherited=inherited,
        clearance_table_update=clearance_table_update,
        clearance_update_columns=clearance_columns,
        approval_table_update=approval_table_update,
        approval_update_columns=approval_columns,
        public_grants=public_grants,
    )


def report(problems: list[str], state: str, *, require_login: bool) -> int:
    if state == "prepared":
        print(f"준비 상태: {RUNTIME_ROLE} 이 아직 없습니다. 정책 OFF·worker 미설치와 맞습니다.")
        print("   만들려면: sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --apply")
        return EXIT_OK
    if not problems:
        print(f"✅ {RUNTIME_ROLE}: 속성·멤버십·상속 권한이 모두 계약과 같습니다.")
        print("   계정이 선 것이지 worker 가 도는 것은 아닙니다. 정책은 따로 봅니다.")
        return EXIT_OK
    head = "활성화할 수 없습니다" if require_login else "문제가 있습니다"
    print(f"🔴 {head} — {len(problems)}건:")
    for code in problems:
        print(f"   {code}")
    print("\n   Master/수집기 권한을 넓혀 해결하지 마세요.")
    print("   조치: sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --verify")
    return EXIT_PROBLEM


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vision 전용 접속 계정 대조 (읽기 전용)")
    ap.add_argument(
        "--require-login",
        action="store_true",
        help="활성화 직전 확인. 접속 계정이 없으면 준비 상태가 아니라 실패다",
    )
    args = ap.parse_args(argv)

    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 이 없다.")
        return EXIT_INPUT

    import psycopg

    # 읽기 전용으로 못 박는다. 이 스크립트가 쓰는 일은 없어야 한다.
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        facts = collect(conn)
    problems, state = evaluate(facts, require_login=args.require_login)
    return report(problems, state, require_login=args.require_login)


if __name__ == "__main__":
    raise SystemExit(main())
