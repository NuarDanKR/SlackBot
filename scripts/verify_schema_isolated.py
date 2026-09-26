#!/usr/bin/env python3
"""스키마를 **격리된 DB** 에서 검증한다 — 운영 DB 는 건드리지 않는다.

    # 개발 PC
    python scripts/verify_schema_isolated.py --dsn postgresql://…/tybot_schema_test

    # 서버
    sudo -u tybot /opt/tybot/.venv/bin/python \\
        /opt/tybot/scripts/verify_schema_isolated.py --dsn <격리DSN>

## 무엇을 보나

| # | 무엇 | 왜 |
|---|---|---|
| 1 | 빈 DB 에 최초 적용 | 새 서버에서 선다는 것 |
| 2 | 같은 스키마를 **두 번** | 재적용이 안전하다는 것 |
| 3 | 초안(`4ecf634`)이 적용된 DB 에 재적용 | 이미 돌고 있는 DB 가 따라온다는 것 |
| 4 | 위험 권한이 실제로 `REVOKE` 되는가 | **GRANT 목록에서 빼는 것만으로는 안 사라진다** |

3번과 4번이 이 스크립트의 이유다. 1·2번은 새 파일이면 대개 통과하는데, 이미
권한을 받은 역할에서 그 권한을 **회수**하는 것은 다른 일이다. PostgreSQL 은
`GRANT` 를 쌓기만 하므로, 선언에서 지운다고 사라지지 않는다.

## DBA 가 먼저 만들어야 하는 것

이 스크립트는 **아무것도 만들지 않는다.** 개발 PC 계정에 `CREATEDB` 도
`CREATEROLE` 도 없고(2026-09-25 확인), 있더라도 만들지 않는 편이 맞다 — 만드는
권한이 있으면 지우는 사고도 그만큼 커진다.

```sql
CREATE DATABASE tybot_schema_test;
CREATE ROLE tyslackai NOLOGIN;
CREATE ROLE tybot_archiver NOLOGIN;
```

역할에 `LOGIN` 도 비밀번호도 주지 않는다. 이 검증이 보는 것은 **권한이 붙었나**
뿐이고, 붙는 것을 보는 데 접속은 필요 없다.

만든 뒤 준비 상태만 확인할 수 있다. 여기까지는 읽기만 한다.

    python scripts/verify_schema_isolated.py --dsn <DSN> --preflight-only

## 운영 DB 를 막는다 — 자물쇠 넷

이 스크립트는 `DROP SCHEMA public CASCADE` 를 돌린다. 잘못된 DB 에 닿으면
되돌리는 절차가 사람 손이고, 자료에 따라서는 되돌릴 방법이 없다.

| # | 무엇 | 왜 |
|---|---|---|
| 1 | `DATABASE_URL` 이 설정돼 있으면 거부 | 운영 셸에서 무심코 돌리는 모양 |
| 2 | **설정 파일**이 가리키는 DB 면 거부 | `.env` 에 있는데 export 는 안 된 경우가 있다 |
| 3 | 이름에 `bench`·`lab`·`index` 가 있으면 거부 | 실측 자료가 들어 있다 |
| 4 | 우리 것이 아닌 표가 있으면 거부 | 이름이 맞아도 남이 쓰는 DB 일 수 있다 |

2번이 늦게 들어왔다. 처음에는 환경변수만 봤는데, 개발 PC 의 `.env` 에
`DATABASE_URL` 이 운영 DB 를 가리키면서 export 는 안 돼 있었다. 그 상태에서
1번 자물쇠는 **헛돈다.**

3번도 늦게 들어왔다. 처음에는 `bench` 와 `lab` 을 「시험용 이름」 으로 보고
**안전 표시에 넣었다.** 그런데 `tybot_archive_bench` 와 `archive_lab` 에는 저장
구조 실측 자료가 있다. 「시험용처럼 보이는 이름」 과 「버려도 되는 DB」 는 다르다.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "deploy" / "sql"
SYSTEM_ENV_FILES = (Path("/etc/tybot/tybot.env"),)

# 이름에 이 중 하나가 **있어야** 격리 DB 로 본다.
#
# `bench` 와 `lab` 을 뺐다. 처음에는 넣었는데, `tybot_archive_bench` 와
# `archive_lab` 에는 **저장 구조 실측 자료가 들어 있다.** 이 스크립트는
# `DROP SCHEMA public CASCADE` 를 돌리므로 그 DB 를 받으면 자료가 사라진다.
# 「시험용처럼 보이는 이름」 과 「버려도 되는 DB」 는 다르다.
SAFE_DB_MARKERS = ("schema_test", "schema_check", "scratch")

# 이름에 이게 있으면 표시가 맞아도 **거부한다.** 다른 작업이 쓰는 DB 다.
RESERVED_DB_MARKERS = ("bench", "lab", "index", "prod", "tyslackai")

#: 검증 대상. `apply-schema.sh` 순서를 따른다 — 뒤 파일이 앞 파일의 표를 참조한다.
TARGET_FILES = ("index_schema.sql", "console_schema.sql",
                "archiving_schema.sql", "workspace_service_schema.sql")

#: 역할에서 **없어야 하는** 권한. (역할, 표, 권한)
#:
#: 초안(`4ecf634`)이 준 뒤 선언에서 뺀 것들이다. 선언에서 빼는 것만으로는
#: 사라지지 않으므로 여기서 확인한다.
FORBIDDEN: tuple[tuple[str, str, str], ...] = (
    ("tybot_archiver", "archive_config_audit", "INSERT"),
    ("tybot_archiver", "archive_channel_mode", "UPDATE"),
    ("tybot_archiver", "archive_feature_flag", "UPDATE"),
    ("tybot_archiver", "archive_message_revision", "UPDATE"),
    ("tybot_archiver", "archive_message_revision", "DELETE"),
    ("tybot_archiver", "archive_refusal", "UPDATE"),
    ("tybot_archiver", "archive_refusal", "DELETE"),
    ("tybot_archiver", "archive_ingest_state", "DELETE"),
    ("tybot_archiver", "workspace_service_secret", "SELECT"),
    ("tyslackai", "archive_message_revision", "UPDATE"),
    ("tyslackai", "archive_message_revision", "DELETE"),
    ("tyslackai", "archive_config_audit", "UPDATE"),
    ("tyslackai", "bot_conversation_audit", "UPDATE"),
)

#: 있어야 하는 권한. 없으면 봇에게는 그 표가 없는 것과 같다(2026-09-14 실측).
REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("tybot_archiver", "archive_channel_mode", "SELECT"),
    ("tybot_archiver", "archive_ingest_state", "INSERT"),
    ("tybot_archiver", "archive_message_revision", "INSERT"),
    ("tybot_archiver", "workspace_service", "SELECT"),
    ("tyslackai", "workspace_service_secret", "SELECT"),
    ("tyslackai", "archive_channel_mode", "UPDATE"),
)

REQUIRED_FUNCTIONS: tuple[tuple[str, str, str], ...] = (
    ("tybot_archiver", "archiver_runtime_config(text)", "EXECUTE"),
)


def dsn_database(dsn: str) -> str:
    text = dsn.strip()
    if "://" in text:
        from urllib.parse import urlparse

        with contextlib.suppress(ValueError):
            return urlparse(text).path.lstrip("/").split("?")[0]
        return ""
    found = re.search(r"(?:^|\s)dbname\s*=\s*('[^']*'|\"[^\"]*\"|\S+)", text)
    return found.group(1).strip("'\"") if found else ""


def configured_databases() -> set[str]:
    """설정 파일이 가리키는 DB 이름들. **값을 적용하지 않고 이름만 본다.**

    개발 PC 의 `.env` 에 `DATABASE_URL` 이 있는데 export 는 안 돼 있는 경우가 있다.
    그러면 환경변수만 보는 자물쇠는 **헛돈다** — 설정에는 운영 DB 가 적혀 있는데
    이 프로세스에는 안 보인다. 2026-09-25 에 실제로 그 상태였다.

    파일을 읽되 `os.environ` 에 넣지 않는다. 넣으면 다른 코드가 그 값으로 붙는다.
    비밀번호는 읽지도 돌려주지도 않는다 — 필요한 것은 DB 이름뿐이다.
    """
    names: set[str] = set()
    for candidate in (
        os.getenv("TYBOT_ENV_FILE"),
        ROOT / ".env",
        *SYSTEM_ENV_FILES,
    ):
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith("DATABASE_URL"):
                    continue
                _, _, value = stripped.partition("=")
                name = dsn_database(value.strip().strip("\"'"))
                if name:
                    names.add(name.lower())
    return names


def operational_db_refusal(dsn: str) -> str:
    """돌려도 되는가. 안 되면 사유를 돌려준다.

    이 스크립트는 `DROP SCHEMA public CASCADE` 를 돌린다. 잘못된 DB 에 닿으면
    되돌리는 절차가 사람 손이고, 자료에 따라서는 되돌릴 방법이 없다.
    """
    if os.environ.get("DATABASE_URL", "").strip():
        return (
            "DATABASE_URL 이 설정돼 있어 시작하지 않습니다. 이 스크립트는 스키마를"
            " 통째로 지우고 다시 만듭니다.\n"
            "  이 셸에서 DATABASE_URL 을 비우고 --dsn 만 주세요."
        )
    name = dsn_database(dsn)
    if not name:
        return "--dsn 에서 DB 이름을 읽지 못했습니다. 격리 DB 인지 확인할 수 없습니다"
    lowered = name.lower()

    # 설정 파일이 가리키는 DB 는 **이름 표시와 무관하게** 거부한다.
    if lowered in configured_databases():
        return (
            f"「{name}」 은 이 저장소의 설정 파일이 가리키는 DB 입니다. 환경변수로"
            " 올라와 있지 않아도 운영 대상입니다 — 이 스크립트는 스키마를 통째로"
            " 지우므로 돌리지 않습니다"
        )

    reserved = [marker for marker in RESERVED_DB_MARKERS if marker in lowered]
    if reserved:
        return (
            f"「{name}」 은 다른 작업이 쓰는 DB 로 보입니다({' · '.join(reserved)})."
            " 실측 자료가 들어 있을 수 있고 이 스크립트는 스키마를 통째로 지웁니다 —"
            " 전용 DB 를 따로 만드세요"
        )

    if not any(marker in lowered for marker in SAFE_DB_MARKERS):
        return (
            f"--dsn 의 DB 이름이 「{name}」 입니다. 격리 DB 임을 이름으로 알 수 없으면"
            f" 돌리지 않습니다 — 이름에 {' · '.join(SAFE_DB_MARKERS)} 중 하나를"
            " 넣은 DB 를 따로 만드세요"
        )
    return ""


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def apply_files(conn, names: tuple[str, ...]) -> None:
    with conn.cursor() as cur:
        for name in names:
            cur.execute((SQL_DIR / name).read_text(encoding="utf-8"))


def has_privilege(conn, role: str, table: str, privilege: str) -> bool | None:
    """`None` 은 「역할이나 표가 없어 판정 불가」 다. `False` 와 구분한다."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if not cur.fetchone():
            return None
        cur.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS ok", (role, table, privilege)
        )
        return bool(cur.fetchone()[0])


def has_function_privilege(
    conn, role: str, function: str, privilege: str
) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regprocedure(%s) IS NOT NULL AS ok", (function,))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if not cur.fetchone():
            return None
        cur.execute(
            "SELECT has_function_privilege(%s, %s, %s) AS ok",
            (role, function, privilege),
        )
        return bool(cur.fetchone()[0])


#: 이 스키마들이 만드는 표. 여기 없는 표가 있으면 남의 DB 다.
OUR_TABLE_PREFIXES = (
    "archive_", "workspace", "bot_conversation_audit", "org_unit", "employee",
    "user_identity", "raw_line", "console_", "usage_", "specialist_", "harness_",
    "qa_", "answer_", "deploy_",
)

REQUIRED_ROLES = ("tyslackai", "tybot_archiver")


def missing_roles(conn) -> list[str]:
    """없는 역할. **만들지 않는다.**

    개발 PC 계정에는 `CREATEROLE` 이 없다(2026-09-25 확인). 전에는 여기서
    `CREATE ROLE` 을 시도했는데, 그 실패가 **`reset()` 뒤에** 났다 — 스키마를
    지우고 나서 권한 오류로 죽는다. 순서가 그 자체로 사고였다.

    지금은 preflight 에서 확인만 하고, 없으면 DBA 가 돌릴 문장을 알려 준다.
    """
    with conn.cursor() as cur:
        out = []
        for role in REQUIRED_ROLES:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if not cur.fetchone():
                out.append(role)
        return out


def foreign_tables(conn) -> list[str]:
    """우리 것이 아닌 표. 하나라도 있으면 **지우지 않는다.**

    이름 표시만으로는 부족하다. 누가 `..._schema_test` 라는 이름으로 다른 일을
    하고 있을 수 있고, 그때 `DROP SCHEMA public CASCADE` 는 되돌릴 수 없다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        names = [str(row[0]) for row in cur.fetchall()]
    return [
        name for name in names
        if not any(name.startswith(prefix) for prefix in OUR_TABLE_PREFIXES)
    ]


def preflight(conn) -> list[str]:
    """**지우기 전에** 확인한다. 하나라도 걸리면 아무것도 안 한다.

    확인을 뒤에 두면 「지우고 나서 못 한다고 말하는」 스크립트가 된다.
    """
    problems = []
    absent = missing_roles(conn)
    if absent:
        statements = "; ".join(f"CREATE ROLE {role} NOLOGIN" for role in absent)
        problems.append(
            f"역할이 없습니다: {', '.join(absent)}. 이 계정에는 CREATEROLE 이 없으므로"
            f" DBA 가 만들어야 합니다 — {statements}"
        )
    strangers = foreign_tables(conn)
    if strangers:
        shown = ", ".join(strangers[:8]) + (" …" if len(strangers) > 8 else "")
        problems.append(
            f"이 DB 에 우리 것이 아닌 표가 {len(strangers)}개 있습니다({shown})."
            " 격리 DB 가 아닌 것으로 보여 지우지 않습니다"
        )
    return problems


def reset(conn) -> None:
    """빈 상태로 되돌린다. **`preflight()` 를 통과한 뒤에만 부른다.**"""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


def draft_shape(conn) -> None:
    """`4ecf634` 초안이 적용된 DB 를 흉내 낸다.

    정확히 그 커밋의 SQL 을 돌리는 것이 아니라, **재적용이 고쳐야 하는 모양**을
    만든다 — 초안이 준 권한과 초안의 단일 PK.

    초안 파일을 git 에서 꺼내 돌리는 방법도 있지만, 그러면 이 시험이 커밋 하나에
    묶인다. 다음에 또 모양이 바뀌면 여기만 고치면 된다.
    """
    with conn.cursor() as cur:
        cur.execute("GRANT SELECT, INSERT ON TABLE archive_config_audit TO tybot_archiver")
        cur.execute(
            "GRANT USAGE, SELECT ON SEQUENCE archive_config_audit_id_seq TO tybot_archiver"
        )
        cur.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE"
            " archive_message_revision, bot_conversation_audit TO tyslackai"
        )


def check_privileges(conn) -> list[str]:
    problems = []
    for role, table, privilege in FORBIDDEN:
        got = has_privilege(conn, role, table, privilege)
        if got is True:
            problems.append(f"{role} 이 {table} 에 {privilege} 권한을 아직 갖고 있다")
    for role, table, privilege in REQUIRED:
        got = has_privilege(conn, role, table, privilege)
        if got is None:
            problems.append(f"{role}/{table} 을 확인할 수 없다(역할이나 표가 없다)")
        elif got is False:
            problems.append(f"{role} 에게 {table} 의 {privilege} 권한이 없다")
    for role, function, privilege in REQUIRED_FUNCTIONS:
        got = has_function_privilege(conn, role, function, privilege)
        if got is None:
            problems.append(f"{role}/{function} 을 확인할 수 없다(역할이나 함수가 없다)")
        elif got is False:
            problems.append(f"{role} 에게 {function} 의 {privilege} 권한이 없다")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="격리된 DB 의 DSN")
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="아무것도 바꾸지 않고 준비 상태만 본다(역할·남의 표·DSN 안전성)",
    )
    parser.add_argument(
        "--artifact-out", type=Path,
        help="검증 결과를 서버 반입용 JSON으로 쓴다. 생략하면 현재 호스트 게이트에 기록한다",
    )
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    problem = operational_db_refusal(args.dsn)
    if problem:
        print(problem)
        return 2

    try:
        conn = _connect(args.dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"DB 에 붙지 못했습니다: {type(exc).__name__}: {exc}")
        return 2

    failures: list[str] = []
    with conn:
        # 0. **지우기 전에** 확인한다. 뒤에 두면 「지우고 나서 못 한다고 말하는」
        #    스크립트가 된다 — 개발 PC 에 CREATEROLE 이 없어 실제로 그 모양이었다.
        blockers = preflight(conn)
        if blockers:
            print("■ 시작 전 확인에서 걸렸습니다. **아무것도 바꾸지 않았습니다.**")
            for item in blockers:
                print(f"  ✗ {item}")
            return 2
        if args.preflight_only:
            # DBA 가 넘기기 전에 확인하는 길. 여기까지는 읽기만 했다.
            print("준비 확인 통과 — DSN 안전, 역할 있음, 남의 표 없음.")
            print("아무것도 바꾸지 않았습니다. --preflight-only 를 빼면 검증을 시작합니다.")
            return 0

        # 1. 빈 DB 에 최초 적용
        reset(conn)
        apply_files(conn, TARGET_FILES)
        print("1. 빈 DB 최초 적용 — 통과")
        failures += [f"[최초] {item}" for item in check_privileges(conn)]

        # 2. 같은 스키마를 두 번
        apply_files(conn, TARGET_FILES)
        print("2. 재적용(멱등) — 통과")
        failures += [f"[재적용] {item}" for item in check_privileges(conn)]

        # 3. 초안이 적용된 DB 에 재적용
        draft_shape(conn)
        before = has_privilege(conn, "tybot_archiver", "archive_config_audit", "INSERT")
        if before is not True:
            failures.append("[초안] 흉내 낸 초안 권한이 안 붙었다 — 이 시험이 무의미하다")
        apply_files(conn, TARGET_FILES)
        print("3. 초안 DB 재적용 — 통과")
        failures += [f"[초안 재적용] {item}" for item in check_privileges(conn)]

    if failures:
        print("\n■ 확인 실패")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print("\n4. 권한 회수·부여 — 통과")
    required_count = len(REQUIRED) + len(REQUIRED_FUNCTIONS)
    print(f"\n네 가지 모두 통과했습니다 (금지 {len(FORBIDDEN)}건 · 필수 {required_count}건).")

    # 통과를 **지문과 함께** 남긴다. 콘솔의 release gate 가 이 파일을 본다.
    #
    # 지문에 묶는 이유: 검증 뒤에 스키마를 고치면 게이트가 다시 닫혀야 한다.
    # 「한 번 했으니 됐다」 로 두면 고친 부분은 **아무도 확인하지 않은 채로** 간다.
    # 그리고 고치는 것은 늘 검증 뒤다.
    sys.path.insert(0, str(ROOT / "src"))
    from tybot.console.release_gate import record_pass

    marker = record_pass(
        by=os.getenv("USER") or os.getenv("USERNAME") or "unknown",
        dsn_label=dsn_database(args.dsn),
        destination=args.artifact_out,
    )
    print(f"검증 기록: {marker}")
    if args.artifact_out:
        print(
            "이 파일은 아직 서버 게이트를 열지 않습니다. 서버에서 "
            "scripts/install_schema_verification.py 로 설치하세요."
        )
    else:
        print("현재 호스트 콘솔의 active 전환 잠금이 이 기록으로 열립니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
