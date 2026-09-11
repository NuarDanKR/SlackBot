"""일정 폴더↔조직 — 무엇이 누구에게 가는지 보고, 시끄러운 것을 끈다.

## 기본은 허용이다 (2026-09-11 오너 결정)
폴더↔조직 매핑은 우리 추정이 아니라 **그룹웨어의 폴더 ACL** 이다. Oracle 뷰의
`org_code` 는 `SYS_OBJECT_ACL` 에서 `SUBJECTTYPE='GR'`·`READ='R'` 인 행 — "이 부서는
이 폴더를 읽을 수 있다" 를 그룹웨어가 직접 선언한 값이다.

그룹웨어가 이미 열어 준 것을 우리가 또 승인받으면 같은 판단을 두 번 하는 것이고, 그
사이에 **아무에게도 DM 이 가지 않는다.** 실제로 그렇게 됐다 — 승인 표가 빈 채로 남아
일정 DM 이 0건이었다. 그래서 동기화가 켠 상태로 넣는다.

노출이 늘지는 않는다. 받는 사람은 그 폴더를 그룹웨어에서 이미 열어 볼 수 있고, 게다가
`/일정 알림` 을 스스로 켠 사람만 받는다. 우리가 더하는 것은 밀어 주기뿐이다.

## 그래서 사람이 하는 일은 **끄는 것**이다
시끄러운 폴더를 빼는 것은 판단이라 사람이 한다. 끈 행은 동기화가 되살리지 않는다
(`on conflict do nothing`) — 되살리면 끌 방법이 없어진다.

`approve` 는 남겨 둔다. 끈 것을 되돌릴 때, 그리고 ACL 에 아직 안 잡힌 폴더를 손으로
열 때 쓴다.

## 쓰기

    python scripts/schedule_folder_approve.py list            # 폴더별 수신 조직
    python scripts/schedule_folder_approve.py who 3420-M      # 이 사람이 받는 폴더 수
    python scripts/schedule_folder_approve.py gap             # 켜졌는데 수신 조직이 없는 폴더
    python scripts/schedule_folder_approve.py pending         # 사람이 끈 것
    python scripts/schedule_folder_approve.py revoke  654 ABB155 --actor dan@taeyoung.com
    python scripts/schedule_folder_approve.py approve 654 ABB155 --actor dan@taeyoung.com

`revoke` 는 행을 지우지 않고 끄고, 그 조직의 미발송 큐를 함께 취소한다. 지우면 언제부터
왜 멈췄는지 알 수 없고, 큐를 남기면 끈 뒤에도 DM 이 나간다.
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

# 사람이 끈 행. 기본이 허용이므로 여기 있는 것은 **누군가의 결정**이다.
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
    ap = argparse.ArgumentParser(
        description="일정 폴더↔조직 — ACL 기준 기본 허용. 사람은 끄는 쪽을 한다")
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
            _print(_rows(cur), empty="사람이 끈 폴더↔조직이 없다.")
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
                print("0개다 — 알림을 켜도 아무것도 오지 않는다.")
                print("그룹웨어에서 이 사람 부서에 열린 일정 폴더가 없거나,"
                      " 누군가 끈 것이다. `gap` 과 `pending` 으로 본다.")
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
