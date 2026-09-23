#!/usr/bin/env python3
"""PF 콘솔(`/pf/`) 접근 권한을 준다·거둔다·본다.

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/pf_grant.py list
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/pf_grant.py add dan@taeyoung.com --role viewer
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/pf_grant.py remove dan@taeyoung.com --role viewer

설계: `docs/design/pf-console.md` §3

## 왜 별도 도구인가
PF 콘솔 프로세스는 **자기 권한 행을 못 고친다.** PF DB role 에 `console_user_service`
쓰기 권한이 없다(`pf_console_schema.sql`). 그건 의도한 것이다 — 조회만 여는 화면이
자기 권한을 넓힐 수 있으면 그건 조회 화면이 아니다.

그래서 권한은 **밖에서** 준다. 이 도구는 TYBot 쪽 `DATABASE_URL` 로 붙으므로
`src/tybot_pf/` 안에 두지 않는다. 거기 두면 PF 패키지가 TYBot DB 를 아는 것이 되고,
격리 시험(`tests/test_pf_isolation.py`)이 막는다.

## 왜 SQL 을 손으로 치지 않나
`INSERT INTO console_user_service ...` 한 줄이면 되지만, 그 한 줄이 조용히 틀린다.

- 이메일 대소문자가 다르면 행은 들어가는데 **로그인한 사람과 안 맞는다**
- `console_user` 에 없는 이메일을 넣으면 그 행은 영원히 아무 일도 안 한다
- 서비스 키를 잘못 적으면 외래키가 막지만, 오타가 실제 키와 비슷하면 안 막는다

셋 다 「넣었는데 화면이 비어 있다」 로 나타나고, 그때 사람은 코드를 의심한다.
그래서 넣기 전에 확인하고, 넣은 뒤에 **확인한 것을 보여 준다.**

> 이 작업은 결국 콘솔 화면으로 가야 한다(CLAUDE.md 「운영 기능은 콘솔에 둔다」).
> 오늘은 PF 계정이 몇 개뿐이라 CLI 로 둔다. 계정이 늘면 화면이 먼저다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.envfile import load_env_file  # noqa: E402
from tybot_pf.auth import ROLES  # noqa: E402


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "DATABASE_URL 이 없습니다. TYBOT_ENV_FILE 을 함께 넘기거나 환경변수를 설정하세요."
        )
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("psycopg 가 없습니다.") from exc
    return psycopg.connect(url, autocommit=True, row_factory=psycopg.rows.dict_row)


def cmd_list(args) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.key, s.label, s.state,
                   coalesce(array_agg(us.email || ' (' || us.role || ')'
                            ORDER BY us.email, us.role)
                            FILTER (WHERE us.email IS NOT NULL), '{}') AS grants
              FROM managed_service s
              LEFT JOIN console_user_service us ON us.service = s.key
             GROUP BY s.key, s.label, s.state
             ORDER BY s.label
            """
        )
        rows = cur.fetchall()
    if not rows:
        print("등록된 서비스가 없습니다. apply-schema.sh 를 먼저 실행하세요.")
        return 1
    for row in rows:
        print(f"\n■ {row['label']} ({row['key']}) · {row['state']}")
        grants = list(row["grants"])
        if not grants:
            # 이 상태가 「로그인은 되는데 아무것도 안 보인다」 의 원인이다.
            print("    권한이 부여된 계정이 없습니다 — 아무도 이 서비스를 볼 수 없습니다.")
        for item in grants:
            print(f"    {item}")
    return 0


def _check_account(cur, email: str) -> str | None:
    """`console_user` 에 있는 실제 이메일. 없으면 None.

    **DB 에 적힌 대소문자를 그대로 돌려준다.** 권한 행을 다른 대소문자로 넣으면
    행은 들어가는데 로그인한 사람과 안 맞고, 화면은 비어 있다.
    """
    cur.execute(
        "SELECT email, active FROM console_user WHERE lower(email) = lower(%s)",
        (email.strip(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if not row["active"]:
        print(f"  ! {row['email']} 은 비활성 계정입니다. 권한을 줘도 로그인하지 못합니다.")
    return str(row["email"])


def cmd_add(args) -> int:
    with _connect() as conn, conn.cursor() as cur:
        actual = _check_account(cur, args.email)
        if actual is None:
            print(f"콘솔 계정이 없습니다: {args.email}")
            print("  먼저 TYBot 콘솔 > 콘솔 사용자 관리에서 계정을 만드세요.")
            print("  PF 콘솔은 회사 계정을 공유하고 서비스 권한만 따로 봅니다.")
            return 2
        if actual != args.email.strip():
            print(f"  · DB 에 적힌 대소문자로 맞춥니다: {args.email} → {actual}")

        cur.execute("SELECT key FROM managed_service WHERE key = %s", (args.service,))
        if cur.fetchone() is None:
            cur.execute("SELECT key FROM managed_service ORDER BY key")
            known = ", ".join(row["key"] for row in cur.fetchall()) or "(없음)"
            print(f"그런 서비스가 없습니다: {args.service}")
            print(f"  등록된 서비스: {known}")
            return 2

        cur.execute(
            """
            INSERT INTO console_user_service (email, service, role, granted_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email, service, role) DO NOTHING
            """,
            (actual, args.service, args.role, args.by or os.getenv("SUDO_USER", "") or "cli"),
        )
        changed = cur.rowcount

        # 넣은 뒤에 **읽어서 보여 준다.** 「넣었다」 와 「들어갔다」 는 다르다.
        cur.execute(
            "SELECT role FROM console_user_service WHERE email = %s AND service = %s"
            " ORDER BY role",
            (actual, args.service),
        )
        roles = [row["role"] for row in cur.fetchall()]

    if changed:
        print(f"권한을 주었습니다: {actual} → {args.service} ({args.role})")
    else:
        print(f"이미 있는 권한입니다: {actual} → {args.service} ({args.role})")
    print(f"  현재 역할: {', '.join(roles)}")
    print(f"  확인: http://<서버>:8788/pf/  에서 {actual} 로 로그인")
    return 0


def cmd_remove(args) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM console_user_service"
            " WHERE lower(email) = lower(%s) AND service = %s AND role = %s",
            (args.email.strip(), args.service, args.role),
        )
        removed = cur.rowcount
        cur.execute(
            "SELECT role FROM console_user_service"
            " WHERE lower(email) = lower(%s) AND service = %s ORDER BY role",
            (args.email.strip(), args.service),
        )
        roles = [row["role"] for row in cur.fetchall()]

    if not removed:
        print(f"그런 권한이 없습니다: {args.email} → {args.service} ({args.role})")
        return 1
    print(f"권한을 거뒀습니다: {args.email} → {args.service} ({args.role})")
    if roles:
        print(f"  남은 역할: {', '.join(roles)}")
    else:
        # 세션에 권한을 담지 않으므로 다음 요청부터 바로 막힌다(설계 §3).
        print("  남은 역할이 없습니다 — 이 사람은 이제 /pf/ 에서 아무것도 보지 못합니다.")
        print("  이미 로그인한 세션도 다음 요청부터 막힙니다(권한은 요청마다 다시 읽습니다).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PF 콘솔(/pf/) 접근 권한을 준다·거둔다·본다. 자세한 설명은 이 파일 머리말.",
    )
    parser.add_argument("--env-file", default=os.getenv("TYBOT_ENV_FILE", ""))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="서비스와 권한을 본다")

    for name, help_text in (("add", "권한을 준다"), ("remove", "권한을 거둔다")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("email", help="회사 이메일")
        p.add_argument("--service", default="pf-hermes")
        p.add_argument("--role", default="viewer", choices=ROLES)
        if name == "add":
            p.add_argument("--by", default="", help="누가 줬는지(감사용)")

    args = parser.parse_args()
    if args.env_file:
        os.environ["TYBOT_ENV_FILE"] = args.env_file
    load_env_file()

    return {"list": cmd_list, "add": cmd_add, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
