#!/usr/bin/env python3
"""팀 일정을 Oracle 뷰에서 뽑아 JSONL 로 저장한다 — **봇 서버에서 실행한다.**

    python scripts/schedule_export.py --out /var/tmp/tyslack-schedule --mode live --horizon-hours 48
    python scripts/schedule_export.py --out /var/tmp/tyslack-schedule --mode reconcile --horizon-days 30

만들어지는 것:
    <out>/<모드>-<YYYY-MM-DD_HHMMSS>/schedule.jsonl
    <out>/<모드>-<YYYY-MM-DD_HHMMSS>/manifest.json

`live` 는 가까운 일정을 자주(1분) 가져와 알림 직전까지 반영하고,
`reconcile` 은 넓은 범위를 매시간 훑어 놓친 것을 맞춘다.

## 왜 바로 PostgreSQL 에 넣지 않고 파일을 거치나
봇 서버가 Oracle 을 직접 조회하는 구성(방식 A)이라 한 번에 밀어 넣을 수도 있다.
그런데 그러면 **조회가 반쯤 실패한 결과를 검사 없이 반영**하게 된다.
파일 사이에 체크섬·행수·범위 검사가 들어간다. 조직 반영에서 이 검사가 실제로
이름 없는 3건을 잡아 전량 롤백을 막은 적이 있다.

## 화면용 쿼리를 쓰지 않는다
그룹웨어에는 사용자별 달력을 그리는 `V_SYS_CALENDAR` 같은 뷰가 있다. 그건 **한 사람이
볼 수 있는 것을 모으는** 쿼리라 개인 일정·공유 일정을 UNION 으로 붙인다. 그걸 1분마다
돌리면 (1) 개인 일정이 딸려 들어오고 (2) 사람 수만큼 조회가 늘어난다.
여기서는 `V_TYSLACK_SCHEDULE` 하나만 **기간으로 한 번** 조회한다.

## 로그에 남기지 않는 것
**일정 제목과 장소는 출력하지 않는다.** 배치 로그는 대개 오래 남고 여러 사람이 본다.
행 수·기간·폴더 수·해시만 남긴다.
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

# 원본이 쓰는 형식. `'2026-08-31 14:30'` 16자.
# ISO 와 자릿수 순서가 같아 문자열 비교로 기간을 잘라도 정확하고, 인덱스를 탄다.
ORACLE_DT = "%Y-%m-%d %H:%M"

# 폴더가 어느 부서에 열려 있는지까지 가져온다. 받는 쪽은 그 부서 코드로
# 워크스페이스·공지 채널을 찾는다. 폴더를 손으로 등록하지 않는 이유가 이것이다.
# 한 폴더가 여러 부서에 열려 있으면 행이 여럿이 된다.
FOLDER_SQL = ("select folder_id, folder_name, org_code, org_name"
              "  from {schema}.V_TYSLACK_SCHEDULE_FOLDER"
              " order by folder_id, org_code")

# 기간이 겹치는 것을 모두 가져온다. **시작 시각만 보면 안 된다** —
# 어제 시작해 오늘까지 이어지는 일정이 빠진다.
SCHEDULE_SQL = (
    "select folder_id, event_id, occurrence_id, title, place,"
    "       starts_at, ends_at, all_day_yn, repeat_yn, updated_at"
    "  from {schema}.V_TYSLACK_SCHEDULE"
    " where starts_at <= :range_end"
    "   and ends_at   >= :range_start"
    " order by starts_at, occurrence_id"
)


class ExportError(Exception):
    """파일을 만들지 않고 중단시키는 문제."""


def connect():
    load_env_file()
    try:
        import oracledb
    except ImportError as exc:
        raise ExportError("oracledb 가 없다:  pip install oracledb") from exc

    user = os.environ.get("ORACLE_USER")
    password = os.environ.get("ORACLE_PASSWORD")
    if not user or not password:
        raise ExportError("ORACLE_USER / ORACLE_PASSWORD 가 없다(.env.example 참고).")
    if user.upper() == "TYSLACK":
        raise ExportError(
            "뷰 소유자 계정이다. TYSLACK_BOT 으로 바꾼다"
            " — 소유자는 원본 테이블까지 읽을 수 있다."
        )

    dsn = os.environ.get("ORACLE_DSN")
    if not dsn:
        host = os.environ.get("ORACLE_HOST")
        sid = os.environ.get("ORACLE_SID") or None
        service = os.environ.get("ORACLE_SERVICE") or None
        if not host or not (sid or service):
            raise ExportError("ORACLE_HOST 와 ORACLE_SID(또는 ORACLE_SERVICE)가 필요하다.")
        port = int(os.environ.get("ORACLE_PORT", 1521))
        dsn = (oracledb.makedsn(host, port, sid=sid) if sid
               else oracledb.makedsn(host, port, service_name=service))
    return oracledb.connect(user=user, password=password, dsn=dsn)


def to_iso(value: str | None) -> str | None:
    """`'2026-08-31 14:30'` → `'2026-08-31T14:30:00+09:00'`.

    원본에는 시간대가 없다. 그룹웨어는 KST 로만 돌아가므로 KST 로 못 박는다.
    받는 쪽이 "몇 분 전 알림" 을 계산하려면 시간대가 분명해야 한다.
    """
    text = (value or "").strip()
    if not text:
        # CHAR 컬럼이라 값이 없어도 공백으로 채워져 온다. 빈 값과 같이 다룬다.
        return None
    for fmt in (ORACLE_DT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST).isoformat()
        except ValueError:
            continue
    raise ExportError(f"시각 형식을 모르겠다: {text!r}")


def to_record(row: tuple) -> dict:
    """뷰 1행 → JSONL 1줄.

    필드 이름은 받는 쪽 `deploy/sql/schedule_schema.sql` 의 `schedule_occurrence`
    컬럼과 **같게** 맞췄다. 이름이 다르면 수신 코드가 매번 번역해야 하고,
    거기서 한 칸 밀리는 실수가 조용히 잘못된 시각의 알림으로 나간다.
    """
    (folder_id, event_id, date_id, subject, place,
     starts_at, ends_at, all_day_yn, repeat_yn, modified_at) = row
    return {
        "source_folder_id": int(folder_id),
        "date_id": int(date_id),
        "event_id": int(event_id),
        "subject": subject,
        "place": (place or None),
        "starts_at": to_iso(starts_at),
        "ends_at": to_iso(ends_at),
        "is_all_day": all_day_yn == "Y",
        "is_repeat": repeat_yn == "Y",
        "source_modified_at": to_iso(modified_at),
    }


def fetch(conn, schema: str, range_start: str, range_end: str) -> tuple[list, list]:
    with conn.cursor() as cur:
        cur.execute(FOLDER_SQL.format(schema=schema))
        folders = [
            {"folder_id": int(r[0]), "folder_name": r[1],
             "org_code": r[2], "org_name": r[3]}
            for r in cur.fetchall()
        ]

        cur.execute(
            SCHEDULE_SQL.format(schema=schema),
            {"range_start": range_start, "range_end": range_end},
        )
        rows = [to_record(r) for r in cur.fetchall()]
    return folders, rows


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_snapshot(
    outdir: pathlib.Path,
    rows: list[dict],
    *,
    mode: str,
    range_start: str,
    range_end: str,
    folders: list[dict],
    taken_at: datetime,
) -> pathlib.Path:
    """임시 이름으로 쓰고 rename 한다 — 반쯤 쓰인 파일을 남기지 않는다."""
    outdir.mkdir(parents=True, exist_ok=True)

    target = outdir / "schedule.jsonl"
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(target)

    manifest = {
        # 받는 쪽 schedule_sync_run.snapshot_id. 폴더 이름과 같게 둔다 —
        # 수신기가 디렉터리 이름을 파싱하지 않아도 되게.
        "snapshot_id": outdir.name,
        "mode": mode,
        "generated_at": taken_at.isoformat(),
        # 시간대를 붙여 보낸다. 받는 쪽 컬럼이 timestamptz 라
        # 시간대 없는 문자열을 넣으면 서버 시간대로 해석돼 9시간 어긋난다.
        "horizon_start": to_iso(range_start),
        "horizon_end": to_iso(range_end),
        # 일정이 하나도 없는 폴더도 반드시 넣는다. 받는 쪽은 이 목록과 기간을 합쳐
        # "있어야 했는데 안 온 행" 을 삭제로 판정한다. 목록을 데이터에서 유추하면
        # 빈 폴더의 묵은 일정이 영영 지워지지 않는다.
        # 받는 쪽 schedule_sync_run.source_folders 로 그대로 들어간다(비어 있으면 거부).
        "source_folders": sorted({f["folder_id"] for f in folders}),
        # 폴더 ↔ 부서. 받는 쪽은 org_code 로 워크스페이스·공지 채널을 찾는다.
        # 한 폴더가 여러 부서에 열려 있으면 여기 여러 행으로 나온다.
        "folders": folders,
        "counts": {"schedule": len(rows)},
        "files": {"schedule.jsonl": _sha256(target)},
    }
    # manifest 를 마지막에 쓴다. 받는 쪽은 manifest 가 있어야 반영을 시작하므로,
    # 전송이 중간에 끊겨도 반쪽 스냅샷을 반영하지 않는다.
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return outdir


def resolve_range(args, now: datetime) -> tuple[str, str]:
    if args.mode == "live":
        hours = args.horizon_hours if args.horizon_hours is not None else 48
        end = now + timedelta(hours=hours)
    else:
        days = args.horizon_days if args.horizon_days is not None else 30
        end = now + timedelta(days=days)
    # 시작을 조금 앞으로 당긴다. 지금 진행 중인 일정과, 방금 시작 시각이 바뀐 일정을
    # 범위 밖으로 흘리지 않기 위해서다.
    start = now - timedelta(hours=1)
    return start.strftime(ORACLE_DT), end.strftime(ORACLE_DT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="승인된 팀 일정을 JSONL 로 추출한다")
    ap.add_argument("--out", required=True, help="스냅샷을 만들 상위 폴더")
    ap.add_argument("--mode", choices=("live", "reconcile"), default="live")
    ap.add_argument("--horizon-hours", type=int, help="live 기본 48")
    ap.add_argument("--horizon-days", type=int, help="reconcile 기본 30")
    ap.add_argument("--schema", default=os.environ.get("ORACLE_SCHEMA") or "TYSLACK")
    args = ap.parse_args(argv)

    if args.horizon_hours is not None and args.horizon_days is not None:
        print("--horizon-hours 와 --horizon-days 를 함께 쓸 수 없다.")
        return 2

    now = datetime.now(KST)
    range_start, range_end = resolve_range(args, now)

    try:
        with connect() as conn:
            folders, rows = fetch(conn, args.schema, range_start, range_end)
    except ExportError as exc:
        print(f"중단: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        # 조회 실패를 "일정 없음" 으로 넘기면 받는 쪽이 전부 삭제로 판정한다.
        print(f"중단: 조회 실패 — {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1

    if not folders:
        # 폴더가 비면 받는 쪽이 판정할 범위가 없다. 파일을 만들지 않는다.
        print("중단: 승인된 일정 폴더가 없다."
              " V_TYSLACK_SCHEDULE_FOLDER 의 허용 목록을 확인한다.")
        return 1

    # **행 0건은 정상이다.** 앞으로 이틀간 회의가 없을 수 있다.
    # 조직 스냅샷과 달리 여기서 0건을 사고로 보면 안 된다.
    outdir = pathlib.Path(args.out) / f"{args.mode}-{now.strftime('%Y-%m-%d_%H%M%S')}"
    write_snapshot(
        outdir, rows,
        mode=args.mode, range_start=range_start, range_end=range_end,
        folders=folders, taken_at=now,
    )

    # 제목·장소는 찍지 않는다.
    folder_count = len({f["folder_id"] for f in folders})
    org_count = len({f["org_code"] for f in folders})
    print(f"{args.mode}: {len(rows)}건 · 폴더 {folder_count}개 · 부서 {org_count}개"
          f" · {range_start} ~ {range_end}")
    print(f"만들었다: {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
