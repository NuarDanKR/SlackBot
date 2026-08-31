"""조직·인사 스냅샷 반영 — 내부망이 밀어 넣은 파일을 PostgreSQL 로 옮긴다.

방식 B(push)의 받는 쪽이다. 보내는 쪽은 `scripts/oracle_export.py`(내부망 실행).
**이 파일은 Oracle 에 접속하지 않는다.** 봇 서버에 Oracle 자격증명이 없는 것이 방식 B 의
전부이므로, 여기서 Oracle 을 import 하는 순간 그 이점이 사라진다.

    python -m tybot.orgsync                 # inbox 에 새 스냅샷이 있으면 반영
    python -m tybot.orgsync --dry-run       # 검사만 하고 쓰지 않는다
    python -m tybot.orgsync --dir <경로>    # 특정 스냅샷 폴더를 지정

## 실패하는 쪽이 안전한 방향이다
조직 데이터는 **권한 판정의 근거**다. 잘못 반영되면 못 볼 자료가 보이거나, 봐야 할 자료가
안 보인다. 둘 다 조용히 일어나서 몇 달 뒤에나 드러난다. 그래서 의심스러우면 반영하지 않고
직전 스냅샷을 그대로 둔다. "일단 넣고 나중에 고친다"를 하지 않는다.

## 막는 것들
- 체크섬 불일치 → 반영 안 함 (전송 중 손상·부분 업로드)
- 행 수가 직전의 90% 미만 → 반영 안 함 (조회 실패를 '전원 퇴직'으로 오인하지 않기)
- 순환 참조·고아 부모 → 반영 안 함 (권한 상속이 무한히 돌거나 끊긴다)
- PII 패턴 유입 → 반영 안 함 (뷰에서 걸렀어야 하는 것이 새어 들어왔다는 뜻)
- 사라진 행 → **지우지 않고 active=false** (과거 원문의 발화자를 해석하려면 이력이 필요하다)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("tybot.orgsync")

KST = timezone(timedelta(hours=9))

# 스냅샷이 직전보다 이만큼 밑으로 줄면 사고로 본다.
MIN_RATIO = 0.9

# 뷰에서 이미 걸렀어야 하는 것들. 여기서 걸리면 뷰 정의가 바뀐 것이다.
PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d{6}\s*[-–]\s*[1-4]\d{6}"), "주민등록번호 형식"),
    (re.compile(r"^\s*01[016789][-\s]?\d{3,4}[-\s]?\d{4}\s*$"), "휴대전화번호 형식"),
]

ORG_KINDS = {"hq", "team", "site", "project"}


class SnapshotError(Exception):
    """반영을 중단시키는 문제. 직전 스냅샷은 그대로 둔다."""


@dataclass(frozen=True)
class Snapshot:
    org: list[dict]
    emp: list[dict]
    source: Path
    taken_at: str | None = None


@dataclass
class SyncResult:
    ok: bool
    org_rows: int = 0
    emp_rows: int = 0
    org_deactivated: int = 0
    emp_deactivated: int = 0
    message: str = ""
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 읽기·검증 — DB 없이 동작한다(테스트가 쉬워지고, 검증이 DB 상태에 얽히지 않는다)
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{path.name} {i}행: JSON 아님 — {exc}") from exc
        if not isinstance(obj, dict):
            raise SnapshotError(f"{path.name} {i}행: 객체가 아님")
        rows.append(obj)
    return rows


def load_snapshot(directory: Path | str) -> Snapshot:
    """manifest 의 체크섬을 확인하고 두 파일을 읽는다.

    체크섬을 먼저 본다. 파일이 반쯤 올라온 상태에서 읽으면 '조직이 절반 사라졌다'로
    보이고, 그게 그대로 대량 비활성화로 이어진다.
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError(f"manifest.json 이 없다: {directory}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") or {}
    if not files:
        raise SnapshotError("manifest 에 files 가 없다")

    for name, expected in files.items():
        target = directory / name
        if not target.is_file():
            raise SnapshotError(f"{name} 이 없다(manifest 에는 있다)")
        actual = _sha256(target)
        if actual != expected:
            raise SnapshotError(
                f"{name} 체크섬 불일치 — 전송이 손상됐거나 아직 끝나지 않았다"
            )

    return Snapshot(
        org=_read_jsonl(directory / "org.jsonl"),
        emp=_read_jsonl(directory / "emp.jsonl"),
        source=directory,
        taken_at=manifest.get("taken_at"),
    )


def _check_tree(org: list[dict]) -> list[str]:
    """고아 부모와 순환 참조를 찾는다. 둘 다 권한 상속을 망가뜨린다."""
    problems: list[str] = []
    parents = {r.get("org_code"): r.get("parent_code") for r in org}

    missing = sorted(
        {p for p in parents.values() if p is not None and p not in parents}
    )
    if missing:
        problems.append(
            f"부모가 스냅샷에 없는 조직코드 {len(missing)}종: {', '.join(missing[:5])}"
        )

    # 각 노드에서 위로 올라가며 이미 지난 노드를 다시 만나면 순환이다.
    seen_ok: set[str] = set()
    for start in parents:
        path: list[str] = []
        node = start
        while node is not None and node not in seen_ok:
            if node in path:
                cycle = " → ".join([*path[path.index(node) :], node])
                problems.append(f"순환 참조: {cycle}")
                break
            path.append(node)
            node = parents.get(node)
        seen_ok.update(path)
    return problems


def _check_pii(rows: list[dict], label: str) -> list[str]:
    problems = []
    for row in rows:
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            for pattern, name in PII_PATTERNS:
                if pattern.search(value):
                    problems.append(f"{label}.{key} 에 {name} 이 들어 있다")
                    return problems  # 값 자체는 로그에 남기지 않는다
    return problems


def check_snapshot(
    snapshot: Snapshot,
    *,
    previous_org: int = 0,
    previous_emp: int = 0,
) -> list[str]:
    """반영해도 되는지 본다. 빈 목록이면 통과."""
    problems: list[str] = []

    if not snapshot.org:
        problems.append("조직 스냅샷이 비어 있다")
    if not snapshot.emp:
        problems.append("인사 스냅샷이 비어 있다")

    for label, rows, keys in (
        ("org", snapshot.org, ("org_code", "org_name")),
        ("emp", snapshot.emp, ("emp_no", "name")),
    ):
        for i, row in enumerate(rows, 1):
            for key in keys:
                if not row.get(key):
                    problems.append(f"{label}.jsonl {i}행: {key} 가 비었다")
                    break
            if len(problems) > 20:
                break

    codes = {r.get("org_code") for r in snapshot.org}
    dup_org = len(snapshot.org) - len(codes)
    if dup_org:
        problems.append(f"조직코드 중복 {dup_org}건")

    emp_nos = {r.get("emp_no") for r in snapshot.emp}
    dup_emp = len(snapshot.emp) - len(emp_nos)
    if dup_emp:
        problems.append(f"사번 중복 {dup_emp}건")

    bad_kind = sorted({
        str(r.get("kind")) for r in snapshot.org if r.get("kind") not in ORG_KINDS
    })
    if bad_kind:
        problems.append(f"모르는 조직 구분: {', '.join(bad_kind[:5])}")

    orphan_emp = [
        r for r in snapshot.emp if r.get("org_code") and r.get("org_code") not in codes
    ]
    if orphan_emp:
        problems.append(f"소속 조직이 스냅샷에 없는 직원 {len(orphan_emp)}명")

    problems += _check_tree(snapshot.org)
    problems += _check_pii(snapshot.org, "org")
    problems += _check_pii(snapshot.emp, "emp")

    # 급감 방어. 원본 조회가 반쯤 실패한 스냅샷을 '전원 퇴직'으로 오인하지 않게 한다.
    for label, now, before in (
        ("조직", len(snapshot.org), previous_org),
        ("인사", len(snapshot.emp), previous_emp),
    ):
        if before and now < before * MIN_RATIO:
            problems.append(
                f"{label} 행 수가 급감했다: {before} → {now}"
                f" ({MIN_RATIO:.0%} 미만). 원본 조회 실패를 의심한다"
            )
    return problems


# ---------------------------------------------------------------------------
# 반영 — 트랜잭션 하나. 실패하면 직전 스냅샷이 그대로 살아 있다.
# ---------------------------------------------------------------------------


def apply_snapshot(conn, snapshot: Snapshot, *, dry_run: bool = False) -> SyncResult:
    """스테이징 후 원자적 교체. `conn` 은 psycopg 연결(autocommit=False)."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from org_unit where active")
        previous_org = cur.fetchone()[0]
        cur.execute("select count(*) from employee where active")
        previous_emp = cur.fetchone()[0]

    problems = check_snapshot(
        snapshot, previous_org=previous_org, previous_emp=previous_emp
    )
    if problems:
        conn.rollback()
        return SyncResult(
            ok=False,
            message="검사에서 걸렸다. 반영하지 않았고 직전 스냅샷은 그대로다.",
            problems=problems,
        )

    if dry_run:
        conn.rollback()
        return SyncResult(
            ok=True,
            org_rows=len(snapshot.org),
            emp_rows=len(snapshot.emp),
            message="검사만 했다(--dry-run). 아무것도 쓰지 않았다.",
        )

    with conn.cursor() as cur:
        cur.execute(
            "create temp table org_stage ("
            " code text, name text, kind text, parent_code text,"
            " company_code text, active boolean) on commit drop"
        )
        with cur.copy(
            "copy org_stage (code, name, kind, parent_code, company_code, active)"
            " from stdin"
        ) as cp:
            for r in snapshot.org:
                cp.write_row((
                    r["org_code"], r["org_name"], r["kind"],
                    r.get("parent_code"), r.get("company_code"), bool(r.get("active")),
                ))

        cur.execute(
            "create temp table emp_stage ("
            " emp_no text, name text, email text, org_code text,"
            " position text, active boolean) on commit drop"
        )
        with cur.copy(
            "copy emp_stage (emp_no, name, email, org_code, position, active)"
            " from stdin"
        ) as cp:
            for r in snapshot.emp:
                cp.write_row((
                    r["emp_no"], r["name"], r.get("email"), r.get("org_code"),
                    r.get("position"), bool(r.get("active")),
                ))

        # org_unit.parent_code 는 자기 참조 외래키다. 부모가 아직 없는 상태로 넣으면
        # 순서에 따라 실패하므로, 먼저 부모 없이 전부 넣고 나서 부모를 채운다.
        cur.execute(
            "insert into org_unit (code, name, kind, parent_code, company_code,"
            "                      org_path, active, synced_at)"
            " select code, name, kind, null, company_code, null, active, now()"
            "   from org_stage"
            " on conflict (code) do update"
            "    set name = excluded.name, kind = excluded.kind,"
            "        company_code = excluded.company_code,"
            "        active = excluded.active, synced_at = now()"
        )
        cur.execute(
            "update org_unit o set parent_code = s.parent_code"
            "  from org_stage s where s.code = o.code"
            "   and o.parent_code is distinct from s.parent_code"
        )
        # 스냅샷에서 사라진 조직은 지우지 않고 끈다.
        cur.execute(
            "update org_unit set active = false, synced_at = now()"
            " where active and code not in (select code from org_stage)"
        )
        org_deactivated = cur.rowcount

        cur.execute(
            "insert into employee (emp_no, name, email, org_code, position,"
            "                      active, synced_at)"
            " select emp_no, name, email, org_code, position, active, now()"
            "   from emp_stage"
            " on conflict (emp_no) do update"
            "    set name = excluded.name, email = excluded.email,"
            "        org_code = excluded.org_code, position = excluded.position,"
            "        active = excluded.active, synced_at = now()"
        )
        # **퇴직자는 스냅샷에서 사라진다**(그룹웨어가 ISHR 을 'N' 으로 바꾼다).
        # 그래서 '없어진 사번 = 퇴직'으로 봐야 한다. 안 그러면 조회 권한이 남는다.
        cur.execute(
            "update employee set active = false, synced_at = now()"
            " where active and emp_no not in (select emp_no from emp_stage)"
        )
        emp_deactivated = cur.rowcount

        cur.execute(
            "insert into sync_run (source, ended_at, ok, org_rows, emp_rows, message)"
            " values ('snapshot_push', now(), true, %s, %s, %s)",
            (len(snapshot.org), len(snapshot.emp),
             f"{snapshot.source.name} 반영"),
        )

    conn.commit()
    return SyncResult(
        ok=True,
        org_rows=len(snapshot.org),
        emp_rows=len(snapshot.emp),
        org_deactivated=org_deactivated,
        emp_deactivated=emp_deactivated,
        message="반영 완료",
    )


def record_failure(conn, message: str, problems: list[str]) -> None:
    """왜 반영하지 않았는지 남긴다. 조용한 미반영이 가장 위험하다."""
    detail = message + " / " + " · ".join(problems[:10])
    with conn.cursor() as cur:
        cur.execute(
            "insert into sync_run (source, ended_at, ok, message)"
            " values ('snapshot_push', now(), false, %s)",
            (detail[:2000],),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _inbox() -> Path:
    state = os.environ.get("STATE_DIR") or "/var/lib/tybot"
    return Path(os.environ.get("SNAPSHOT_INBOX") or (Path(state) / "inbox"))


def _newest_snapshot(inbox: Path) -> Path | None:
    if not inbox.is_dir():
        return None
    # 점으로 시작하는 이름은 전송 중인 폴더다(업로드 쪽이 `.이름` 으로 올린 뒤 rename 한다).
    # manifest 를 마지막에 올리더라도, 올린 직후 rename 전에 잡히는 틈이 남는다.
    candidates = [
        d for d in inbox.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and d.name != "processed"
        and (d / "manifest.json").is_file()
    ]
    return max(candidates, key=lambda d: d.name) if candidates else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="조직·인사 스냅샷을 PostgreSQL 에 반영한다")
    ap.add_argument("--dir", help="스냅샷 폴더. 비우면 inbox 에서 가장 최근 것")
    ap.add_argument("--dry-run", action="store_true", help="검사만 하고 쓰지 않는다")
    ap.add_argument("--keep", action="store_true", help="반영 후 파일을 옮기지 않는다")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from .envfile import load_env_file

    load_env_file()

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL 이 없다. 반영할 곳을 모른다.")
        return 2

    inbox = _inbox()
    directory = Path(args.dir) if args.dir else _newest_snapshot(inbox)
    if directory is None:
        log.info("반영할 스냅샷이 없다: %s", inbox)
        return 0

    try:
        import psycopg
    except ImportError:
        log.error("psycopg 가 없다:  pip install 'psycopg[binary]'")
        return 2

    with psycopg.connect(url) as conn:
        try:
            snapshot = load_snapshot(directory)
        except SnapshotError as exc:
            log.error("스냅샷을 읽지 못했다: %s", exc)
            record_failure(conn, str(exc), [])
            return 1

        result = apply_snapshot(conn, snapshot, dry_run=args.dry_run)

        if not result.ok:
            log.error("%s", result.message)
            for p in result.problems:
                log.error("  - %s", p)
            record_failure(conn, result.message, result.problems)
            return 1

        log.info(
            "%s — 조직 %d행 · 인사 %d행 (비활성 전환 조직 %d · 인사 %d)",
            result.message, result.org_rows, result.emp_rows,
            result.org_deactivated, result.emp_deactivated,
        )

    if not args.dry_run and not args.keep:
        done = inbox / "processed" / datetime.now(KST).strftime("%Y-%m-%d")
        done.mkdir(parents=True, exist_ok=True)
        shutil.move(str(directory), str(done / directory.name))
        log.info("처리한 파일을 옮겼다: %s", done / directory.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
