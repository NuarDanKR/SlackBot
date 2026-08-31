#!/usr/bin/env python3
"""레거시 Oracle 의 조직·인사 구조를 **메타데이터 수준에서** 살펴본다.

사용:
    python scripts/oracle_probe.py                 # 조직·인사로 보이는 객체 찾기
    python scripts/oracle_probe.py --table V_ORG   # 그 객체의 컬럼 목록
    python scripts/oracle_probe.py --tree V_ORG    # 조직 트리 건전성(고아·순환) 점검
    python scripts/oracle_probe.py --sample V_EMP  # 마스킹한 표본 5행

## 이 도구가 하지 않는 것 — 지키려고 코드로 막아 둔 선
그룹웨어 스키마에는 주민번호·연락처·급여·인사평가가 함께 있다. 그건 아카이브 금지 대상이고
열람 대상도 아니다. 그래서:

- `SELECT` 만 실행한다. DDL·DML 은 이 파일에 없다(뷰 생성은 사람이 직접 실행한다).
- 조직 트리(코드·이름·상위코드)는 값을 본다. 권한 상속이 끊기는지 확인해야 하기 때문이다.
- **인사 데이터는 건수와 컬럼 이름만 본다.** 표본이 필요하면 마스킹해서 출력한다.
- 민감해 보이는 컬럼(주민·급여·연락처 등)은 이름만 표시하고 **값은 조회하지 않는다.**

읽기 전용 계정으로 접속한다. DBA 계정을 쓰면 위 약속이 기술적으로 의미가 없다.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

# 이름만 봐도 가져오면 안 되는 컬럼. 값 조회를 막고 경고로 알린다.
SENSITIVE_HINTS = (
    "PASSWORD", "PASSWD", "PWD", "SECRET", "TOKEN",
    "JUMIN", "SSN", "RESIDENT", "REG_NO", "BIRTH",
    "SALARY", "PAY", "ANNUAL", "BONUS",
    "TEL", "PHONE", "HP", "MOBILE", "ADDR", "ADDRESS", "ZIP", "POST",
    "ACCOUNT", "BANK", "CARD", "PASSPORT", "LICENSE",
    "EVAL", "APPRAISAL", "FAMILY", "MARRY", "MILITARY", "DISABL",
)


def is_sensitive(column: str) -> bool:
    up = column.upper()
    return any(hint in up for hint in SENSITIVE_HINTS)


def mask(value: object) -> str:
    """이름·이메일을 알아볼 수 없게 줄인다. 구조 확인에는 이걸로 충분하다."""
    if value is None:
        return "(없음)"
    text = str(value)
    if "@" in text:
        head, _, domain = text.partition("@")
        return head[:1] + "***@" + domain
    if len(text) <= 2:
        return text[0] + "*"
    return text[0] + "*" * (len(text) - 2) + text[-1]


def connect():
    load_env_file()
    try:
        import oracledb
    except ImportError:
        print("oracledb 가 없다:  pip install oracledb")
        raise SystemExit(2) from None

    user = os.environ.get("ORACLE_USER")
    password = os.environ.get("ORACLE_PASSWORD")
    if not user or not password:
        print("ORACLE_USER / ORACLE_PASSWORD 가 없다. .env 를 확인한다"
              " (형식은 .env.example 참고).")
        raise SystemExit(2)

    dsn = os.environ.get("ORACLE_DSN")
    if not dsn:
        host = os.environ.get("ORACLE_HOST")
        # SID 와 서비스명은 다른 것이다. 우리 그룹웨어(BPROD)는 SID 로 붙는다.
        # 서비스명으로 잘못 붙으면 DPY-6001(ORA-12514) 이 난다.
        sid = os.environ.get("ORACLE_SID") or None
        service = os.environ.get("ORACLE_SERVICE") or None
        if not host or not (sid or service):
            print("ORACLE_DSN 이 없으면 ORACLE_HOST 와"
                  " ORACLE_SID(또는 ORACLE_SERVICE)가 필요하다.")
            raise SystemExit(2)
        port = int(os.environ.get("ORACLE_PORT", 1521))
        dsn = (oracledb.makedsn(host, port, sid=sid) if sid
               else oracledb.makedsn(host, port, service_name=service))
    # thin 모드(기본). Instant Client 없이 Oracle 12.1+ 에 붙는다.
    return oracledb.connect(user=user, password=password, dsn=dsn)


def show_identity(cur) -> None:
    cur.execute("select user, sys_context('USERENV', 'DB_NAME') from dual")
    who, db = cur.fetchone()
    print("접속 계정: " + str(who) + "   DB: " + str(db))
    try:
        cur.execute("select banner from v$version where rownum = 1")
        row = cur.fetchone()
        print("버전: " + (row[0] if row else "(조회 불가)"))
    except Exception as exc:  # noqa: BLE001
        print("버전 조회 불가(권한 없음): " + str(exc).splitlines()[0])

    # 이 계정이 쓰기 권한을 갖고 있으면 알린다 — 읽기 전용이어야 한다.
    try:
        cur.execute("select privilege from user_sys_privs "
                    "where privilege not like '%SESSION%'")
        privs = [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        privs = []
    if privs:
        print("경고: 이 계정에 시스템 권한이 있다 → " + ", ".join(privs[:8]))
        print("      조회만 할 것이지만, 읽기 전용 계정으로 바꾸기를 권한다.")
    print("")


def find_objects(cur, schema: str | None) -> None:
    sql = ("select owner, object_name, object_type from all_objects "
           "where object_type in ('TABLE', 'VIEW') ")
    params: dict[str, str] = {}
    if schema:
        sql += "and owner = :owner "
        params["owner"] = schema.upper()
    sql += ("and (object_name like '%ORG%' or object_name like '%DEPT%' "
            "or object_name like '%EMP%' or object_name like '%HR%' "
            "or object_name like '%MEMBER%') "
            "order by owner, object_name")
    cur.execute(sql, params)
    rows = cur.fetchall()
    if not rows:
        print("조직·인사로 보이는 객체를 못 찾았다. --schema 로 소유자를 지정해 본다.")
        return
    print("후보 객체 " + str(len(rows)) + "개")
    for owner, name, kind in rows:
        print("  " + owner + "." + name + "  (" + kind + ")")
    print("")
    print("다음: python scripts/oracle_probe.py --table <객체명> [--schema <소유자>]")


def show_columns(cur, schema: str | None, table: str) -> None:
    cur.execute(
        "select owner, column_name, data_type, data_length, nullable "
        "from all_tab_columns where table_name = :t "
        "and (:o is null or owner = :o) order by owner, column_id",
        {"t": table.upper(), "o": schema.upper() if schema else None},
    )
    rows = cur.fetchall()
    if not rows:
        print(table + " 을 찾을 수 없다(권한이 없거나 이름이 다르다).")
        return

    owner = rows[0][0]
    print(owner + "." + table.upper() + " — 컬럼 " + str(len(rows)) + "개")
    flagged = []
    for _, col, typ, length, nullable in rows:
        warn = ""
        if is_sensitive(col):
            warn = "   ← 민감. 뷰에서 제외한다"
            flagged.append(col)
        null = "" if nullable == "Y" else " NOT NULL"
        print("  " + col.ljust(28) + typ + "(" + str(length) + ")" + null + warn)

    cur.execute("select count(*) from " + owner + '."' + table.upper() + '"')
    print("")
    print("행 수: " + format(cur.fetchone()[0], ","))

    if flagged:
        print("")
        print("민감 컬럼 " + str(len(flagged)) + "개: " + ", ".join(flagged))
        print("값은 조회하지 않았다. TYBot 용 뷰에도 넣지 않는다.")
    print("")
    print("가져올 컬럼은 조직 트리(코드·이름·상위코드·구분·사용여부)와")
    print("사번·이름·이메일·소속·직위뿐이다.")


def check_tree(cur, schema: str | None, table: str,
               code: str, parent: str, name: str) -> None:
    prefix = (schema.upper() + ".") if schema else ""
    target = prefix + '"' + table.upper() + '"'

    cur.execute("select count(*) from " + target)
    print("조직 행 수: " + format(cur.fetchone()[0], ","))

    cur.execute("select count(*) from " + target + " where " + parent + " is null")
    roots = cur.fetchone()[0]
    note = "   ← 보통 1개다" if roots != 1 else ""
    print("최상위(상위코드 없음): " + str(roots) + "개" + note)

    cur.execute(
        "select o." + code + ", o." + name + ", o." + parent +
        " from " + target + " o where o." + parent + " is not null "
        "and not exists (select 1 from " + target + " p where p." + code +
        " = o." + parent + ") and rownum <= 20"
    )
    orphans = cur.fetchall()
    print("")
    print("고아 조직(상위코드가 실재하지 않음): " + str(len(orphans)) + "개"
          + (" 이상" if len(orphans) == 20 else ""))
    for org_code, org_name, parent_code in orphans:
        print("  " + str(org_code) + "  " + str(org_name)
              + "  → 없는 상위 " + str(parent_code))

    try:
        cur.execute(
            "select " + code + " from " + target +
            " start with " + parent + " is null" +
            " connect by nocycle prior " + code + " = " + parent +
            " and connect_by_iscycle = 1"
        )
        cycles = cur.fetchall()
        print("")
        print("순환 참조: " + str(len(cycles)) + "개")
        for row in cycles[:20]:
            print("  " + str(row[0]))
    except Exception as exc:  # noqa: BLE001
        print("")
        print("순환 검사 실패: " + str(exc).splitlines()[0])

    if orphans or roots != 1:
        print("")
        print("고아나 다중 최상위가 있으면 권한 상속이 조용히 어긋난다.")
        print("뷰에서 걸러낼지, 우리 쪽에서 처리할지 정해야 한다.")


def show_sample(cur, schema: str | None, table: str) -> None:
    prefix = (schema.upper() + ".") if schema else ""
    cur.execute("select * from " + prefix + '"' + table.upper() + '" where rownum <= 5')
    cols = [d[0] for d in cur.description]
    for i, row in enumerate(cur.fetchall(), 1):
        print("[" + str(i) + "]")
        for col, val in zip(cols, row, strict=True):
            if is_sensitive(col):
                print("  " + col.ljust(24) + "(민감 — 표시하지 않음)")
            else:
                print("  " + col.ljust(24) + mask(val))
    print("")
    print("값은 전부 마스킹했다. 구조 확인이 목적이지 내용 열람이 아니다.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default=os.environ.get("ORACLE_SCHEMA"))
    ap.add_argument("--table", help="컬럼 목록을 볼 객체")
    ap.add_argument("--tree", help="조직 트리 건전성을 볼 객체")
    ap.add_argument("--sample", help="마스킹한 표본 5행을 볼 객체")
    ap.add_argument("--code", default="ORG_CODE", help="--tree 의 조직코드 컬럼")
    ap.add_argument("--parent", default="PARENT_ORG_CODE", help="--tree 의 상위코드 컬럼")
    ap.add_argument("--name", default="ORG_NAME", help="--tree 의 조직명 컬럼")
    args = ap.parse_args()

    with connect() as conn, conn.cursor() as cur:
        show_identity(cur)
        if args.table:
            show_columns(cur, args.schema, args.table)
        elif args.tree:
            check_tree(cur, args.schema, args.tree, args.code, args.parent, args.name)
        elif args.sample:
            show_sample(cur, args.schema, args.sample)
        else:
            find_objects(cur, args.schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
