"""일정 스냅샷 → PostgreSQL 수신기.

설계: [`docs/design/schedule-command.md`](../../docs/design/schedule-command.md) §4-1

    schedule_export.py  →  <inbox>/<모드>-<시각>/{schedule.jsonl, manifest.json}
                        →  이 모듈  →  schedule_occurrence · schedule_sync_run

`orgsync.py` 와 같은 골격이다: 체크섬 검증 → 검사 → 트랜잭션 1개 → 이력.
일정에만 있는 위험 세 가지를 따로 막는다.

## 1. 범위 밖을 지우면 안 된다
스냅샷은 `horizon_start~horizon_end` 구간만 담는다. "안 온 행 = 삭제" 판정을 그 구간
**밖까지** 적용하면, live 스냅샷(앞으로 48시간) 하나가 한 달 뒤 일정을 전부 지운다.
삭제 판정은 **구간 안 × manifest 의 `source_folders` 안** 으로만 한정한다.

## 2. 지운 뒤 되살아나는 일
소프트 삭제만 한다(`source_deleted_at`). 다음 스냅샷에 다시 오면 되살린다.
행을 물리적으로 지우면 발송 이력(`schedule_delivery`)이 가리킬 대상이 사라진다.

## 3. 보존 기간이 지난 제목을 되살리지 않는다
종료 7일 뒤 제목·장소를 비운다(`details_purged_at`). 그 뒤 같은 행이 다시 오더라도
제목을 채우지 않는다 — 채우면 보존 정책이 스냅샷 한 번으로 무효가 된다.

## 로그
`subject`·`place` 를 로그·`schedule_sync_run.error` 에 남기지 않는다. 남길 것은
건수·`date_id`·`source_folder_id` 다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("tybot.schedulesync")
KST = timezone(timedelta(hours=9))

# 스냅샷 하나가 구간 안 일정의 이 비율을 넘게 지우려 하면 멈춘다.
# 추출이 절반만 성공한 경우를 '전부 취소됨' 으로 반영하지 않기 위한 문턱이다.
MAX_DELETE_RATIO = 0.5
# 그 아래 건수는 비율과 무관하게 허용한다(작은 표에서 비율이 과민해진다).
MAX_DELETE_FLOOR = 5

# 제목·장소 보존 기간. 일정 종료 후 이 기간이 지나면 비운다.
DETAIL_RETENTION = timedelta(days=7)

MODES = ("live", "reconcile")


class SnapshotError(Exception):
    """스냅샷을 읽을 수 없다. 반영을 시작조차 하지 않는다."""


@dataclass
class Snapshot:
    rows: list[dict]
    manifest: dict
    source: Path

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest.get("snapshot_id") or self.source.name)

    @property
    def mode(self) -> str:
        return str(self.manifest.get("mode") or "")

    @property
    def folders(self) -> list[int]:
        return [int(x) for x in (self.manifest.get("source_folders") or [])]

    @property
    def horizon(self) -> tuple[datetime, datetime]:
        return (
            _parse_ts(self.manifest.get("horizon_start")),
            _parse_ts(self.manifest.get("horizon_end")),
        )


@dataclass
class SyncResult:
    ok: bool
    message: str
    problems: list[str] = field(default_factory=list)
    upserted: int = 0
    deleted: int = 0
    restored: int = 0
    skipped_unknown_folder: int = 0
    purged: int = 0
    already_applied: bool = False


# --- 읽기 -------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=KST)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # 시간대가 없으면 KST 로 본다. 추출기가 +09:00 을 붙이므로 보통은 오지 않는 경로다.
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
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
    """manifest 체크섬을 먼저 확인하고 읽는다.

    반쯤 전송된 파일을 읽으면 '일정이 절반 사라졌다' 로 보이고, 그것이 그대로
    대량 삭제 판정으로 이어진다. 추출기가 manifest 를 **마지막에** 쓰는 이유와 짝이다.
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError(f"manifest.json 이 없다: {directory}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"manifest.json 이 JSON 이 아니다 — {exc}") from exc

    files = manifest.get("files") or {}
    if not files:
        raise SnapshotError("manifest 에 files 가 없다")
    for name, expected in files.items():
        target = directory / name
        if not target.is_file():
            raise SnapshotError(f"{name} 이 없다(manifest 에는 있다)")
        if _sha256(target) != expected:
            raise SnapshotError(
                f"{name} 체크섬 불일치 — 전송이 손상됐거나 아직 끝나지 않았다"
            )

    return Snapshot(
        rows=_read_jsonl(directory / "schedule.jsonl"),
        manifest=manifest,
        source=directory,
    )


def manifest_sha256(snapshot: Snapshot) -> str:
    """이력에 남길 manifest 지문. 같은 스냅샷을 두 번 반영했는지 판별한다."""
    raw = json.dumps(snapshot.manifest, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_sha256(row: dict) -> str:
    """Return a stable fingerprint for one normalized schedule occurrence."""
    starts_at = _parse_ts(row.get("starts_at"))
    ends_at = _parse_ts(row.get("ends_at"))
    modified_at = _parse_ts(row.get("source_modified_at"))
    content = {
        "source_folder_id": int(row["source_folder_id"]),
        "date_id": int(row["date_id"]),
        "event_id": int(row["event_id"]),
        "subject": row.get("subject"),
        "place": row.get("place"),
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "is_all_day": bool(row.get("is_all_day")),
        "is_repeat": bool(row.get("is_repeat")),
        "source_modified_at": modified_at.isoformat() if modified_at else None,
    }
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- 검사 -------------------------------------------------------------------
REQUIRED_ROW_FIELDS = ("source_folder_id", "date_id", "event_id", "starts_at", "ends_at")


def check_snapshot(snapshot: Snapshot) -> list[str]:
    """반영 전에 걸러야 할 것들. 문제 문구에 제목·장소를 넣지 않는다."""
    problems: list[str] = []
    m = snapshot.manifest

    if snapshot.mode not in MODES:
        problems.append(f"mode 가 이상하다: {snapshot.mode!r} (live|reconcile)")
    if not snapshot.snapshot_id:
        problems.append("snapshot_id 가 없다")

    start, end = snapshot.horizon
    if start is None or end is None:
        problems.append("horizon_start/horizon_end 를 읽지 못했다")
    elif end <= start:
        problems.append("horizon_end 가 horizon_start 보다 뒤가 아니다")

    folders = snapshot.folders
    if not folders:
        # 폴더 목록이 비면 삭제 판정 범위를 만들 수 없다. 그대로 반영하면
        # 아무것도 지우지 못하거나 전부 지우거나 — 어느 쪽도 안전하지 않다.
        problems.append("source_folders 가 비었다 — 삭제 판정 범위를 만들 수 없다")

    expected = (m.get("counts") or {}).get("schedule")
    if expected is not None and int(expected) != len(snapshot.rows):
        problems.append(f"행 수 불일치: manifest {expected} vs 파일 {len(snapshot.rows)}")

    known = set(folders)
    seen: set[tuple[int, int]] = set()
    for i, r in enumerate(snapshot.rows, 1):
        missing = [f for f in REQUIRED_ROW_FIELDS if r.get(f) in (None, "")]
        if missing:
            problems.append(f"{i}행: 필드 누락 {missing}")
            continue
        try:
            key = (int(r["source_folder_id"]), int(r["date_id"]))
        except (TypeError, ValueError):
            problems.append(f"{i}행: source_folder_id/date_id 가 정수가 아니다")
            continue
        if key in seen:
            # (source_folder_id, date_id) 는 기본키다. 중복이 오면 어느 쪽이 맞는지
            # 우리가 고를 수 없다 — 추출 쪽 문제이므로 반영하지 않는다.
            problems.append(f"중복 키: 폴더 {key[0]} · date_id {key[1]}")
            continue
        seen.add(key)

        if known and key[0] not in known:
            problems.append(
                f"{i}행: 폴더 {key[0]} 이 source_folders 에 없다 — 삭제 판정이 어긋난다"
            )
        s, e = _parse_ts(r.get("starts_at")), _parse_ts(r.get("ends_at"))
        if s is None or e is None:
            problems.append(f"{i}행: 시각을 읽지 못했다(폴더 {key[0]} · date_id {key[1]})")
        elif e < s:
            problems.append(f"{i}행: 종료가 시작보다 빠르다(폴더 {key[0]} · date_id {key[1]})")

    if len(problems) > 20:
        problems = [*problems[:20], f"... 외 {len(problems) - 20}건"]
    return problems


# --- 반영 -------------------------------------------------------------------
STAGE_DDL = """
create temp table schedule_stage (
    source_folder_id bigint,
    date_id bigint,
    event_id bigint,
    subject text,
    place text,
    starts_at timestamptz,
    ends_at timestamptz,
    is_all_day boolean,
    is_repeat boolean,
    source_modified_at timestamptz,
    content_sha256 text
) on commit drop
"""

UPSERT_SQL = """
insert into schedule_occurrence (
    source_folder_id, date_id, event_id, subject, place,
    starts_at, ends_at, is_all_day, is_repeat, source_modified_at,
    content_sha256, last_snapshot_id, source_deleted_at
)
select s.source_folder_id, s.date_id, s.event_id, s.subject, s.place,
       s.starts_at, s.ends_at, s.is_all_day, s.is_repeat, s.source_modified_at,
       s.content_sha256, %(snapshot_id)s, NULL
  from schedule_stage s
  join schedule_folder f on f.source_folder_id = s.source_folder_id
on conflict (source_folder_id, date_id) do update set
    event_id = excluded.event_id,
    -- 보존 기간이 지나 비운 제목·장소는 되살리지 않는다.
    subject = case when schedule_occurrence.details_purged_at is null
                   then excluded.subject else null end,
    place   = case when schedule_occurrence.details_purged_at is null
                   then excluded.place else null end,
    starts_at = excluded.starts_at,
    ends_at = excluded.ends_at,
    is_all_day = excluded.is_all_day,
    is_repeat = excluded.is_repeat,
    source_modified_at = excluded.source_modified_at,
    content_sha256 = excluded.content_sha256,
    last_snapshot_id = excluded.last_snapshot_id,
    last_seen_at = now(),
    source_deleted_at = null
returning (xmax = 0) as inserted
"""

# 되살아나는 행 수는 upsert **전에** 센다.
# ON CONFLICT DO UPDATE 의 RETURNING 은 갱신된 **새 값**을 주므로,
# 거기서 source_deleted_at 을 보면 항상 NULL 이라 복구 건수가 0 으로 나온다.
RESTORE_COUNT_SQL = """
select count(*) as n
  from schedule_occurrence o
  join schedule_stage s
    on s.source_folder_id = o.source_folder_id and s.date_id = o.date_id
 where o.source_deleted_at is not null
"""

# 구간 안 · 이번 스냅샷 폴더 안에서, 이번에 오지 않은 행만 소프트 삭제한다.
DELETE_SQL = """
update schedule_occurrence o
   set source_deleted_at = now(),
       last_snapshot_id = %(snapshot_id)s,
       last_seen_at = now()
 where o.source_folder_id = any(%(folders)s)
   and o.source_deleted_at is null
   and o.starts_at < %(horizon_end)s
   and o.ends_at   > %(horizon_start)s
   and not exists (
        select 1 from schedule_stage s
         where s.source_folder_id = o.source_folder_id
           and s.date_id = o.date_id
   )
"""

DELETE_CANDIDATES_SQL = """
select count(*) as n
  from schedule_occurrence o
 where o.source_folder_id = any(%(folders)s)
   and o.source_deleted_at is null
   and o.starts_at < %(horizon_end)s
   and o.ends_at   > %(horizon_start)s
"""

UNKNOWN_FOLDER_SQL = """
select count(*) as n
  from schedule_stage s
  left join schedule_folder f on f.source_folder_id = s.source_folder_id
 where f.source_folder_id is null
"""

PURGE_SQL = """
update schedule_occurrence
   set subject = null, place = null, details_purged_at = now()
 where details_purged_at is null
   and ends_at < now() - %(retention)s
   and (subject is not null or place is not null)
"""

RUN_EXISTS_SQL = "select status from schedule_sync_run where snapshot_id = %(snapshot_id)s"

RUN_INSERT_SQL = """
insert into schedule_sync_run (
    snapshot_id, mode, generated_at, horizon_start, horizon_end,
    source_folders, row_count, manifest_sha256, status
) values (
    %(snapshot_id)s, %(mode)s, %(generated_at)s, %(horizon_start)s, %(horizon_end)s,
    %(folders)s, %(row_count)s, %(manifest_sha256)s, 'received'
)
on conflict (snapshot_id) do update set
    received_at = now(), status = 'received', applied_at = null, error = null
"""

RUN_APPLIED_SQL = """
update schedule_sync_run
   set status = 'applied', applied_at = now(), error = null
 where snapshot_id = %(snapshot_id)s
"""

RUN_REJECTED_SQL = """
update schedule_sync_run
   set status = 'rejected', applied_at = null, error = %(error)s
 where snapshot_id = %(snapshot_id)s
"""


def _scalar(cur):
    row = cur.fetchone()
    if row is None:
        return None
    return row[next(iter(row))] if isinstance(row, dict) else row[0]


def apply_snapshot(
    conn, snapshot: Snapshot, *, dry_run: bool = False, force: bool = False
) -> SyncResult:
    """검사 → 스테이징 → upsert → 삭제 판정 → 이력. 트랜잭션 하나로 끝낸다.

    `conn` 은 autocommit=False 여야 한다. 중간에 실패하면 아무것도 남지 않아야 한다.
    """
    problems = check_snapshot(snapshot)
    if problems:
        conn.rollback()
        return SyncResult(
            ok=False,
            message="검사에서 걸렸다. 반영하지 않았고 직전 상태는 그대로다.",
            problems=problems,
        )

    start, end = snapshot.horizon
    folders = snapshot.folders
    params = {
        "snapshot_id": snapshot.snapshot_id,
        "folders": folders,
        "horizon_start": start,
        "horizon_end": end,
    }

    with conn.cursor() as cur:
        cur.execute(RUN_EXISTS_SQL, {"snapshot_id": snapshot.snapshot_id})
        status = _scalar(cur)
        if status == "applied" and not force:
            conn.rollback()
            return SyncResult(
                ok=True,
                already_applied=True,
                message=f"이미 반영된 스냅샷이다: {snapshot.snapshot_id}",
            )

        cur.execute(STAGE_DDL)
        with cur.copy(
            "copy schedule_stage (source_folder_id, date_id, event_id, subject, place,"
            " starts_at, ends_at, is_all_day, is_repeat, source_modified_at,"
            " content_sha256) from stdin"
        ) as cp:
            for r in snapshot.rows:
                cp.write_row((
                    int(r["source_folder_id"]),
                    int(r["date_id"]),
                    int(r["event_id"]),
                    r.get("subject"),
                    r.get("place"),
                    _parse_ts(r.get("starts_at")),
                    _parse_ts(r.get("ends_at")),
                    bool(r.get("is_all_day")),
                    bool(r.get("is_repeat")),
                    _parse_ts(r.get("source_modified_at")),
                    content_sha256(r),
                ))

        # 승인되지 않은 폴더는 넣지 않는다. schedule_folder 가 허용 목록이다.
        cur.execute(UNKNOWN_FOLDER_SQL)
        unknown = int(_scalar(cur) or 0)

        # 지우려는 양이 지나치면 멈춘다. 추출이 절반만 성공한 스냅샷을
        # '전부 취소됨' 으로 반영하지 않기 위한 문턱이다.
        cur.execute(DELETE_CANDIDATES_SQL, params)
        live_in_horizon = int(_scalar(cur) or 0)
        would_delete = max(live_in_horizon - len(snapshot.rows), 0)
        if (
            not force
            and would_delete > MAX_DELETE_FLOOR
            and live_in_horizon
            and would_delete / live_in_horizon > MAX_DELETE_RATIO
        ):
            conn.rollback()
            msg = (
                f"삭제 판정이 지나치다: 구간 안 {live_in_horizon}건 중 최대 "
                f"{would_delete}건이 사라진다. 추출이 부분 실패했을 수 있다. "
                "확인 후 --force 로 다시 실행하라."
            )
            _record_rejection(conn, snapshot, msg)
            return SyncResult(ok=False, message=msg, problems=[msg])

        if dry_run:
            conn.rollback()
            return SyncResult(
                ok=True,
                message="검사만 했다(--dry-run). 아무것도 쓰지 않았다.",
                upserted=len(snapshot.rows) - unknown,
                deleted=would_delete,
                skipped_unknown_folder=unknown,
            )

        cur.execute(RUN_INSERT_SQL, {
            **params,
            "mode": snapshot.mode,
            "generated_at": _parse_ts(snapshot.manifest.get("generated_at")),
            "row_count": len(snapshot.rows),
            "manifest_sha256": manifest_sha256(snapshot),
        })

        cur.execute(RESTORE_COUNT_SQL)
        restored = int(_scalar(cur) or 0)

        cur.execute(UPSERT_SQL, {"snapshot_id": snapshot.snapshot_id})
        touched = cur.fetchall()

        cur.execute(DELETE_SQL, params)
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur.execute(PURGE_SQL, {"retention": DETAIL_RETENTION})
        purged = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur.execute(RUN_APPLIED_SQL, {"snapshot_id": snapshot.snapshot_id})

    conn.commit()
    return SyncResult(
        ok=True,
        message=f"반영 완료: {snapshot.snapshot_id} ({snapshot.mode})",
        upserted=len(touched),
        deleted=deleted,
        restored=restored,
        skipped_unknown_folder=unknown,
        purged=purged,
    )


def _record_rejection(conn, snapshot: Snapshot, message: str) -> None:
    """거절 사실을 이력에 남긴다. 제목·장소는 넣지 않는다."""
    start, end = snapshot.horizon
    try:
        with conn.cursor() as cur:
            cur.execute(RUN_INSERT_SQL, {
                "snapshot_id": snapshot.snapshot_id,
                "mode": snapshot.mode if snapshot.mode in MODES else "live",
                "generated_at": _parse_ts(snapshot.manifest.get("generated_at")),
                "horizon_start": start,
                "horizon_end": end,
                "folders": snapshot.folders or [0],
                "row_count": len(snapshot.rows),
                "manifest_sha256": manifest_sha256(snapshot),
            })
            cur.execute(RUN_REJECTED_SQL, {
                "snapshot_id": snapshot.snapshot_id,
                "error": message[:1000],
            })
        conn.commit()
    except Exception as e:  # noqa: BLE001 - 이력 기록 실패로 원인을 덮지 않는다
        log.warning("거절 이력을 남기지 못했다: %s", e)
        conn.rollback()


# --- CLI --------------------------------------------------------------------
def inbox() -> Path:
    state = os.environ.get("STATE_DIR") or "/var/lib/tybot"
    return Path(os.environ.get("SCHEDULE_INBOX") or (Path(state) / "inbox-schedule"))


def newest_snapshot(directory: Path) -> Path | None:
    """가장 최근 스냅샷. 점으로 시작하는 폴더는 전송 중이라 건너뛴다."""
    if not directory.is_dir():
        return None
    candidates = [
        d for d in directory.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and d.name != "processed"
        and (d / "manifest.json").is_file()
    ]
    return max(candidates, key=lambda d: d.name) if candidates else None


def pending_snapshots(directory: Path) -> list[Path]:
    """오래된 것부터 전부. live 스냅샷이 밀렸을 때 순서대로 반영해야 한다."""
    if not directory.is_dir():
        return []
    return sorted(
        (
            d for d in directory.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and d.name != "processed"
            and (d / "manifest.json").is_file()
        ),
        key=lambda d: d.name,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일정 스냅샷을 PostgreSQL 에 반영한다")
    ap.add_argument("--dir", help="스냅샷 폴더. 비우면 inbox 의 대기분 전부")
    ap.add_argument("--dry-run", action="store_true", help="검사만 하고 쓰지 않는다")
    ap.add_argument("--keep", action="store_true", help="반영 후 파일을 옮기지 않는다")
    ap.add_argument(
        "--force", action="store_true",
        help="삭제 문턱과 재반영 방지를 넘긴다. 원인을 확인한 뒤에만 쓴다",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from .envfile import load_env_file

    load_env_file()

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL 이 없다. 반영할 곳을 모른다.")
        return 2

    box = inbox()
    targets = [Path(args.dir)] if args.dir else pending_snapshots(box)
    if not targets:
        log.info("반영할 스냅샷이 없다: %s", box)
        return 0

    try:
        import psycopg
    except ImportError:
        log.error("psycopg 가 없다:  pip install 'psycopg[binary]'")
        return 2

    failed = 0
    with psycopg.connect(url) as conn:
        for directory in targets:
            try:
                snapshot = load_snapshot(directory)
            except SnapshotError as exc:
                log.error("%s: 읽지 못했다 — %s", directory.name, exc)
                failed += 1
                continue

            result = apply_snapshot(
                conn, snapshot, dry_run=args.dry_run, force=args.force
            )
            if not result.ok:
                log.error("%s: %s", directory.name, result.message)
                for p in result.problems[:10]:
                    log.error("  - %s", p)
                failed += 1
                continue
            if result.already_applied:
                log.info("%s: %s", directory.name, result.message)
            else:
                log.info(
                    "%s: %s — upsert %d · 삭제 %d · 복구 %d · 미승인폴더 %d · 제목정리 %d",
                    directory.name, result.message, result.upserted, result.deleted,
                    result.restored, result.skipped_unknown_folder, result.purged,
                )
            if not args.dry_run and not args.keep and not args.dir:
                _archive(box, directory)

    return 1 if failed else 0


def _archive(box: Path, directory: Path) -> None:
    done = box / "processed" / datetime.now(KST).strftime("%Y-%m-%d")
    try:
        done.mkdir(parents=True, exist_ok=True)
        directory.rename(done / directory.name)
    except OSError as e:
        log.warning("%s: 처리 완료 폴더로 옮기지 못했다 — %s", directory.name, e)


if __name__ == "__main__":
    raise SystemExit(main())
