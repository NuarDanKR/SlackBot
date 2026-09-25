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

## 운영 DB 를 막는다

`--dsn` 의 DB 이름에 실측/시험 표시가 없으면 거부한다. 그리고 `DATABASE_URL` 이
설정돼 있으면 시작하지 않는다 — 이 스크립트가 만드는 것은 역할과 권한이라,
잘못된 DB 에 돌면 되돌리는 절차가 사람 손이다.
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

# 이름에 이 중 하나가 없으면 격리 DB 로 보지 않는다.
SAFE_DB_MARKERS = ("test", "bench", "lab", "schema_check", "scratch")

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


def dsn_database(dsn: str) -> str:
    text = dsn.strip()
    if "://" in text:
        from urllib.parse import urlparse

        with contextlib.suppress(ValueError):
            return urlparse(text).path.lstrip("/").split("?")[0]
        return ""
    found = re.search(r"(?:^|\s)dbname\s*=\s*('[^']*'|\"[^\"]*\"|\S+)", text)
    return found.group(1).strip("'\"") if found else ""


def operational_db_refusal(dsn: str) -> str:
    """돌려도 되는가. 안 되면 사유를 돌려준다."""
    if os.environ.get("DATABASE_URL", "").strip():
        return (
            "DATABASE_URL 이 설정돼 있어 시작하지 않습니다. 이 스크립트는 역할과 권한을"
            " 바꾸므로, 잘못된 DB 에 돌면 되돌리는 절차가 사람 손입니다.\n"
            "  이 셸에서 DATABASE_URL 을 비우고 --dsn 만 주세요."
        )
    name = dsn_database(dsn)
    if not name:
        return "--dsn 에서 DB 이름을 읽지 못했습니다. 격리 DB 인지 확인할 수 없습니다"
    if not any(marker in name.lower() for marker in SAFE_DB_MARKERS):
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


def ensure_roles(conn) -> None:
    """검증용 역할. **LOGIN 도 비밀번호도 주지 않는다** — 권한만 본다."""
    with conn.cursor() as cur:
        for role in ("tyslackai", "tybot_archiver"):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if not cur.fetchone():
                cur.execute(f"CREATE ROLE {role} NOLOGIN")


def reset(conn) -> None:
    """빈 상태로 되돌린다. 격리 DB 라 통째로 지운다."""
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
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="격리된 DB 의 DSN")
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
        # 1. 빈 DB 에 최초 적용
        reset(conn)
        ensure_roles(conn)
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
    print(f"\n네 가지 모두 통과했습니다 (금지 {len(FORBIDDEN)}건 · 필수 {len(REQUIRED)}건).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
