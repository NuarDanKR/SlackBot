"""스키마 파일이 선언한 것과 실제 DB 를 대조한다.

## 왜 필요한가
스키마 파일에 `ALTER TABLE … ADD COLUMN IF NOT EXISTS` 를 써 두는 것만으로는 DB 가
바뀌지 않는다. **사람이 적용해야** 바뀐다. 그 단계가 빠지면 코드는 새 컬럼을 쓰는데
DB 에는 없고, 그 사실은 **그 화면을 누가 열 때까지 아무도 모른다.**

2026-09-14 실측: 콘솔 「봇 분류 상태」 가 `column "qa_record_id" does not exist` 로
죽었다. 대조해 보니 빠진 컬럼이 하나가 아니라 **9개**였다 — 나머지 8개는 그 화면을
아직 안 열어서 안 터졌을 뿐이었다.

같은 일이 앞서 두 번 더 있었다. `review_digest_sent` 권한 누락, `tybot-review-dm.timer`
미기동. 셋 다 「선언은 했는데 적용은 안 됐다」 이고, 셋 다 사람이 그 기능을 쓸 때까지
숨어 있었다.

## 배포 때마다 돌린다
`update.sh` 뒤에 한 번 돌리면 **적용 안 된 스키마가 즉시 드러난다.** 화면이 죽기 전에.

## 고치지 않는다
무엇이 빠졌는지만 말한다. 자동으로 `ALTER` 를 돌리면 스키마 파일을 읽지 않은 채
DB 가 바뀌고, 그때부터 파일이 진실이 아니게 된다. 적용은 사람이 스키마 파일로 한다.

    python scripts/check_schema_drift.py
    python scripts/check_schema_drift.py --verbose   # 대조한 항목 전부

종료 코드: `0` 일치 · `1` 빠진 것 있음 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_INPUT = 2

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "sql"

# 스키마 파일이 선언하는 것들. **주석 처리된 줄은 제외한다** — 예시로 적어 둔 GRANT 나
# 롤백 안내가 요구사항으로 잡히면 없는 고장을 매번 보고한다.
ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(?P<column>\w+)",
    re.IGNORECASE | re.MULTILINE,
)
CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?P<table>\w+)",
    re.IGNORECASE | re.MULTILINE,
)


def _uncommented(text: str) -> str:
    """`--` 주석 줄을 지운다. 문서의 예시가 요구사항으로 잡히면 안 된다."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


def declared() -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """스키마 파일이 선언한 (표, 컬럼) 과 표 목록. 값은 그것을 적은 파일 이름."""
    columns: dict[tuple[str, str], str] = {}
    tables: dict[str, str] = {}
    for path in sorted(SQL_DIR.glob("*.sql")):
        body = _uncommented(path.read_text(encoding="utf-8"))
        for m in CREATE_TABLE_RE.finditer(body):
            tables.setdefault(m.group("table").lower(), path.name)
        for m in ADD_COLUMN_RE.finditer(body):
            key = (m.group("table").lower(), m.group("column").lower())
            columns.setdefault(key, path.name)
    return columns, tables


def live(conn) -> tuple[set[tuple[str, str]], set[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        )
        columns = {
            (str(r["table_name"]), str(r["column_name"])) for r in cur.fetchall()
        }
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public'"
        )
        tables = {str(r["table_name"]) for r in cur.fetchall()}
    return columns, tables


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="스키마 선언 ↔ 실제 DB 대조 (읽기 전용)")
    ap.add_argument("--verbose", action="store_true", help="대조한 항목 전부 출력")
    args = ap.parse_args(argv)

    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 이 없다.")
        return EXIT_INPUT
    if not SQL_DIR.is_dir():
        print(f"스키마 디렉터리가 없다: {SQL_DIR}")
        return EXIT_INPUT

    import psycopg

    want_columns, want_tables = declared()
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as conn:
        have_columns, have_tables = live(conn)

    missing_tables = sorted(
        (t, f) for t, f in want_tables.items() if t not in have_tables
    )
    # 표가 통째로 없으면 그 표의 컬럼은 따로 세지 않는다 — 같은 원인을 두 번 세면
    # 무엇을 먼저 할지 흐려진다.
    missing_columns = sorted(
        (t, c, f) for (t, c), f in want_columns.items()
        if t in have_tables and (t, c) not in have_columns
    )

    print(f"스키마 파일 {len(list(SQL_DIR.glob('*.sql')))}개 · "
          f"선언된 표 {len(want_tables)}개 · 추가 컬럼 {len(want_columns)}개")
    if args.verbose:
        for (t, c), f in sorted(want_columns.items()):
            mark = "OK " if (t, c) in have_columns else "없음"
            print(f"   {mark} {t}.{c}  ({f})")

    if missing_tables:
        print(f"\n🔴 DB 에 없는 표 {len(missing_tables)}개:")
        for table, source in missing_tables:
            print(f"   {table}   ← {source}")
    if missing_columns:
        print(f"\n🔴 DB 에 없는 컬럼 {len(missing_columns)}개:")
        for table, column, source in missing_columns:
            print(f"   {table}.{column}   ← {source}")

    if not missing_tables and not missing_columns:
        print("\n✅ 선언과 실제가 같다.")
        return EXIT_OK

    files = sorted({f for _, f in missing_tables} | {f for _, _, f in missing_columns})
    print("\n적용할 파일 — 스키마 파일이 진실이다. 여기서 직접 ALTER 하지 않는다:")
    for name in files:
        print(f"   sudo cat /opt/tybot/deploy/sql/{name}"
              " | sudo -u postgres psql -p 55432 -d tyslackai -f -")
    print("\n전부 멱등하다. 여러 번 실행해도 안전하다.")
    return EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
