#!/usr/bin/env python3
"""조직·인사 스냅샷을 Oracle 뷰에서 뽑아 JSONL 로 저장한다 — **내부망에서 실행한다.**

    python scripts/oracle_export.py --out /var/tmp/tyslack
    python scripts/oracle_export.py --out /var/tmp/tyslack --previous /var/tmp/tyslack/last

만들어지는 것:
    <out>/<YYYY-MM-DD_HHMM>/org.jsonl
    <out>/<YYYY-MM-DD_HHMM>/emp.jsonl
    <out>/<YYYY-MM-DD_HHMM>/manifest.json   ← sha256·행수·추출시각

이 폴더를 통째로 봇 서버의 inbox 로 올리면 끝이다(전송 절차는
`docs/deploy/infra-request-snapshot-push.md` 6절). 받는 쪽은 `python -m tybot.orgsync`.

## 왜 sqlplus 가 아니라 파이썬인가
`export_*_12_1.sql` 은 12.1 에 `JSON_OBJECT` 가 없어서 문자열을 이어 붙여 JSON 을 만든다.
이스케이프를 검증해 두긴 했지만, 조직명에 큰따옴표나 역슬래시가 하나 들어오면 그 줄만
조용히 깨지고 그게 부분 반영으로 이어지는 종류의 위험이 남는다.
`json.dumps` 는 그 문제 자체가 없다. sqlplus 의 `LINESIZE` 잘림·`NLS_LANG` 인코딩 사고도
함께 사라진다. 설계 문서(oracle-sync.md 2-B절)도 12.1 에서는 이 방법을 권한다.

## 접속 계정
**`TYSLACK_BOT`** 을 쓴다. 뷰 2개만 읽을 수 있는 계정이다.
뷰 소유자 `TYSLACK` 은 원본 테이블(LOGONPASSWORD 포함)을 읽을 수 있으므로 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

KST = timezone(timedelta(hours=9))

# 뷰에 정의된 컬럼 그대로. 여기 없는 것은 가져오지 않는다.
ORG_SQL = (
    "select org_code, org_name, parent_org_code, org_kind, company_code, use_yn"
    "  from {schema}.V_TYSLACK_ORG order by org_code"
)
EMP_SQL = (
    "select emp_no, emp_name, email, org_code, position_name, use_yn"
    "  from {schema}.V_TYSLACK_EMP order by emp_no"
)

# 뽑은 결과가 이보다 적으면 원본 조회가 반쯤 실패한 것으로 본다.
# 받는 쪽에도 같은 방어가 있지만, **깨진 파일을 애초에 만들지 않는 편이 낫다.**
MIN_ORG_ROWS = 50
MIN_EMP_ROWS = 100


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
        print("ORACLE_USER / ORACLE_PASSWORD 가 없다(.env.example 참고).")
        raise SystemExit(2)
    if user.upper() == "TYSLACK":
        print("경고: 뷰 소유자 계정이다. TYSLACK_BOT 으로 바꾼다"
              " — 소유자는 원본 테이블까지 읽을 수 있다.")

    dsn = os.environ.get("ORACLE_DSN")
    if not dsn:
        host = os.environ.get("ORACLE_HOST")
        sid = os.environ.get("ORACLE_SID") or None
        service = os.environ.get("ORACLE_SERVICE") or None
        if not host or not (sid or service):
            print("ORACLE_HOST 와 ORACLE_SID(또는 ORACLE_SERVICE)가 필요하다.")
            raise SystemExit(2)
        port = int(os.environ.get("ORACLE_PORT", 1521))
        dsn = (oracledb.makedsn(host, port, sid=sid) if sid
               else oracledb.makedsn(host, port, service_name=service))
    return oracledb.connect(user=user, password=password, dsn=dsn)


def fetch(cur, sql: str, schema: str, mapper) -> list[dict]:
    cur.execute(sql.format(schema=schema))
    return [mapper(row) for row in cur.fetchall()]


def org_row(row) -> dict:
    code, name, parent, kind, company, use_yn = row
    return {
        "org_code": code,
        "org_name": name,
        "parent_code": parent,
        "kind": kind,
        "company_code": company,
        "active": use_yn == "Y",
    }


def emp_row(row) -> dict:
    emp_no, name, email, org_code, position, use_yn = row
    return {
        "emp_no": emp_no,
        "name": name,
        "email": email,
        "org_code": org_code,
        "position": position,
        "active": use_yn == "Y",
    }


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> str:
    """임시 이름으로 쓰고 rename 한다 — 반쯤 쓰인 파일을 남기지 않는다."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="스냅샷을 만들 상위 폴더")
    ap.add_argument("--schema", default=os.environ.get("ORACLE_SCHEMA") or "TYSLACK",
                    help="뷰가 있는 스키마 (기본 TYSLACK)")
    ap.add_argument("--label", help="폴더 이름. 비우면 추출 시각")
    args = ap.parse_args()

    now = datetime.now(KST)
    outdir = pathlib.Path(args.out) / (args.label or now.strftime("%Y-%m-%d_%H%M"))
    outdir.mkdir(parents=True, exist_ok=True)

    with connect() as conn, conn.cursor() as cur:
        org = fetch(cur, ORG_SQL, args.schema, org_row)
        emp = fetch(cur, EMP_SQL, args.schema, emp_row)

    print(f"조직 {len(org):,}행 · 인사 {len(emp):,}행")

    if len(org) < MIN_ORG_ROWS or len(emp) < MIN_EMP_ROWS:
        print(f"중단: 행 수가 너무 적다(최소 조직 {MIN_ORG_ROWS} · 인사 {MIN_EMP_ROWS}).")
        print("원본 조회가 실패했을 수 있다. 파일을 만들지 않는다.")
        return 1

    # 사람 이름·이메일이 화면·로그에 남지 않게 통계만 찍는다.
    active_org = sum(1 for r in org if r["active"])
    active_emp = sum(1 for r in emp if r["active"])
    no_mail = sum(1 for r in emp if r["active"] and not r["email"])
    print(f"  사용중 조직 {active_org:,} · 재직 {active_emp:,} (이메일 없음 {no_mail})")

    files = {
        "org.jsonl": write_jsonl(outdir / "org.jsonl", org),
        "emp.jsonl": write_jsonl(outdir / "emp.jsonl", emp),
    }
    manifest = {
        "taken_at": now.isoformat(),
        "schema": args.schema,
        "counts": {"org": len(org), "emp": len(emp),
                   "org_active": active_org, "emp_active": active_emp},
        "files": files,
    }
    # manifest 를 **마지막에** 쓴다. 받는 쪽은 manifest 가 있어야 반영을 시작하므로,
    # 이 순서면 전송이 중간에 끊겨도 반쪽 스냅샷을 반영하지 않는다.
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"만들었다: {outdir}")
    print("이 폴더를 봇 서버 inbox 로 올린 뒤, 봇 서버에서:")
    print("    python -m tybot.orgsync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
