"""일정 폴더↔조직 승인 — DM 이 나가려면 사람이 한 번 켜야 한다.

## 왜 이 도구가 필요한가
`schedule_folder_org` 가 비어 있으면 **아무에게도 일정 DM 이 나가지 않는다.** 그런데
승인으로 넘기는 수단이 없었다. 코드 주석은 "콘솔에서 켜야 발송된다" 고 하지만 그 화면이
아직 없어서, 지금까지는 손으로 SQL 을 쓰는 것이 유일한 길이었다.

첨부 검수 게이트에서 같은 일이 있었다 — 격리는 되는데 승인 전이가 없어서 원본이 쌓이기만
했다. 게이트를 만들 때는 **통과시키는 문도 같이** 만들어야 한다.

## 자동 승인하지 않는 이유
한 Oracle 폴더가 여러 조직에 열려 있을 수 있다. 그대로 켜면 관계없는 팀에 남의 일정이
DM 으로 나간다. 그래서 값은 동기화가 후보로 채우고(`enabled=false`), 켜는 것은 사람이
한다(원칙 3: 막는 쪽이 기본값).

## 쓰기

    python scripts/schedule_folder_approve.py list            # 켜진 폴더와 자격 조직
    python scripts/schedule_folder_approve.py pending         # 후보(미승인)
    python scripts/schedule_folder_approve.py who 3420-M      # 이 사람이 받을 수 있나
    python scripts/schedule_folder_approve.py approve 654 ABB155 --actor dan@taeyoung.com
    python scripts/schedule_folder_approve.py revoke  654 ABB155 --actor dan@taeyoung.com

`revoke` 는 행을 지우지 않고 끈다. 지우면 언제부터 왜 멈췄는지 알 수 없다.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

LIST_SQL = """
select f.source_folder_id, f.label, f.org_code as 대표조직,
       coalesce(string_agg(fo.org_code, ',' order by fo.org_code)
                filter (where fo.enabled), '(없음)') as 승인조직
  from schedule_folder f
  left join schedule_folder_org fo on fo.source_folder_id = f.source_folder_id
 where f.enabled
 group by f.source_folder_id, f.label, f.org_code
 order by f.source_folder_id
"""

PENDING_SQL = """
select fo.source_folder_id, f.label, fo.org_code, fo.approved_by
  from schedule_folder_org fo
  join schedule_folder f on f.source_folder_id = fo.source_folder_id
 where not fo.enabled
 order by fo.source_folder_id, fo.org_code
"""

# 대표 조직은 있는데 승인 목록에 없는 폴더. 지금 우리가 빠진 자리가 바로 여기다.
GAP_SQL = """
select f.source_folder_id, f.label, f.org_code
  from schedule_folder f
 where f.enabled and f.org_code is not null
   and not exists (select 1 from schedule_folder_org fo
                    where fo.source_folder_id = f.source_folder_id
                      and fo.org_code = f.org_code and fo.enabled)
 order by f.source_folder_id
"""

APPROVE_SQL = """
insert into schedule_folder_org (source_folder_id, org_code, enabled, approved_by)
values (%(folder_id)s, %(org_code)s, true, %(actor)s)
on conflict (source_folder_id, org_code) do update
   set enabled = true, approved_by = %(actor)s, approved_at = now(), updated_at = now()
"""

REVOKE_SQL = """
update schedule_folder_org
   set enabled = false, approved_by = %(actor)s, updated_at = now()
 where source_folder_id = %(folder_id)s and org_code = %(org_code)s
"""

# 끈 폴더의 미발송 큐는 취소한다. 남겨 두면 승인을 거둔 뒤에도 DM 이 나간다.
CANCEL_SQL = """
update schedule_dm_delivery d
   set status = 'cancelled', cancelled_at = now(), updated_at = now()
 where d.status in ('pending', 'retry')
   and d.source_folder_id = %(folder_id)s
   and exists (select 1 from employee e
                where e.emp_no = d.emp_no and e.org_code = %(org_code)s)
"""


def _connect():
    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 이 없다.")
        raise SystemExit(2)
    import psycopg

    return psycopg.connect(url, row_factory=psycopg.rows.dict_row)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _print(rows: list[dict], *, empty: str) -> None:
    if not rows:
        print(empty)
        return
    keys = list(rows[0])
    width = {k: max(len(k), *(len(str(r[k])) for r in rows)) for k in keys}
    print("  ".join(k.ljust(width[k]) for k in keys))
    for r in rows:
        print("  ".join(str(r[k]).ljust(width[k]) for k in keys))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일정 폴더↔조직 승인")
    ap.add_argument("action", choices=("list", "pending", "gap", "who", "approve", "revoke"))
    ap.add_argument("target", nargs="?", help="approve/revoke 의 폴더 ID, who 의 사번")
    ap.add_argument("org_code", nargs="?", help="approve/revoke 의 조직코드")
    ap.add_argument(
        "--actor",
        default=os.getenv("USER") or os.getenv("USERNAME") or "",
        help="승인자. 누가 켰는지 남지 않는 승인은 받지 않는다",
    )
    args = ap.parse_args(argv)

    with _connect() as conn, conn.cursor() as cur:
        if args.action == "list":
            cur.execute(LIST_SQL)
            _print(_rows(cur), empty="켜진 폴더가 없다. 폴더를 먼저 켜야 한다.")
            return 0
        if args.action == "pending":
            cur.execute(PENDING_SQL)
            _print(_rows(cur), empty="미승인 후보가 없다.")
            return 0
        if args.action == "gap":
            cur.execute(GAP_SQL)
            rows = _rows(cur)
            _print(rows, empty="대표 조직이 모두 승인되어 있다.")
            if rows:
                print()
                print("이 폴더들은 켜져 있지만 승인 조직이 없어 DM 이 나가지 않는다. 켜기:")
                for r in rows:
                    print(f"  python scripts/schedule_folder_approve.py approve"
                          f" {r['source_folder_id']} {r['org_code']} --actor <나>")
            return 0
        if args.action == "who":
            if not args.target:
                print("사번이 필요하다.")
                return 2
            from tybot.schedule_dm import entitled_folders

            count = entitled_folders(conn, args.target)
            print(f"{args.target} 가 받을 수 있는 폴더: {count}개")
            if not count:
                print("0개다 — 알림을 켜도 아무것도 오지 않는다. `gap` 으로 원인을 본다.")
                return 1
            return 0

        if not args.target or not args.org_code:
            print("폴더 ID 와 조직코드가 필요하다. `list` 로 확인한다.")
            return 2
        if not args.actor.strip():
            print("--actor 가 필요하다. 누가 켰는지 남지 않는 승인은 받지 않는다.")
            return 2
        try:
            folder_id = int(args.target)
        except ValueError:
            print(f"폴더 ID 는 숫자다: {args.target!r}")
            return 2

        params = {"folder_id": folder_id, "org_code": args.org_code, "actor": args.actor}
        if args.action == "approve":
            cur.execute(APPROVE_SQL, params)
            conn.commit()
            print(f"폴더 {folder_id} ← {args.org_code} 승인 (by {args.actor})")
            print("다음 플래너 회차(tybot-schedule-dm.timer)에서 큐가 생긴다.")
            return 0

        cur.execute(REVOKE_SQL, params)
        changed = cur.rowcount
        cur.execute(CANCEL_SQL, params)
        cancelled = cur.rowcount
        conn.commit()
        if not changed:
            print(f"폴더 {folder_id} ← {args.org_code} 승인 기록이 없다.")
            return 1
        print(f"폴더 {folder_id} ← {args.org_code} 해제 (by {args.actor})"
              f" · 미발송 {cancelled}건 취소")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
