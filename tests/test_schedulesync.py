"""일정 스냅샷 수신기.

설계: docs/design/schedule-command.md §4-1

가장 위험한 것은 **삭제 판정**이다. 스냅샷은 `horizon_start~horizon_end` 구간만 담으므로,
"안 온 행 = 삭제" 를 구간 밖까지 적용하면 live 스냅샷(앞으로 48시간) 하나가 한 달 뒤
일정을 전부 지운다. 여기 테스트가 그 경계를 고정한다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

import pytest

from tybot.schedulesync import (
    DETAIL_RETENTION,
    KST,
    MAX_DELETE_FLOOR,
    Snapshot,
    SnapshotError,
    apply_snapshot,
    check_snapshot,
    content_sha256,
    load_snapshot,
    manifest_sha256,
    newest_snapshot,
    pending_snapshots,
)

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=KST)


def _row(folder=654, date_id=1, **kw) -> dict:
    base = {
        "source_folder_id": folder,
        "date_id": date_id,
        "event_id": 1000 + date_id,
        "subject": "주간 회의",
        "place": "본사 3층",
        "starts_at": "2026-08-31T14:00:00+09:00",
        "ends_at": "2026-08-31T15:00:00+09:00",
        "is_all_day": False,
        "is_repeat": False,
        "source_modified_at": "2026-08-30T10:00:00+09:00",
    }
    base.update(kw)
    return base


def _manifest(rows, *, mode="live", folders=(654,), **kw) -> dict:
    m = {
        "snapshot_id": "live-2026-08-31_090000",
        "mode": mode,
        "generated_at": NOW.isoformat(),
        "horizon_start": NOW.isoformat(),
        "horizon_end": (NOW + timedelta(hours=48)).isoformat(),
        "source_folders": list(folders),
        "folders": [{"folder_id": f, "folder_name": "업무", "org_code": "ABB155"} for f in folders],
        "counts": {"schedule": len(rows)},
    }
    m.update(kw)
    return m


def _snap(rows=None, **kw) -> Snapshot:
    rows = [_row()] if rows is None else rows
    from pathlib import Path

    return Snapshot(rows=rows, manifest=_manifest(rows, **kw), source=Path("live-x"))


def _write_snapshot(tmp_path, rows, manifest=None, *, corrupt=False):
    d = tmp_path / "live-2026-08-31_090000"
    d.mkdir()
    jsonl = d / "schedule.jsonl"
    jsonl.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    m = manifest or _manifest(rows)
    digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    m["files"] = {"schedule.jsonl": "0" * 64 if corrupt else digest}
    (d / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return d


# --- 읽기 -------------------------------------------------------------------
def test_load_reads_rows_and_manifest(tmp_path):
    d = _write_snapshot(tmp_path, [_row(), _row(date_id=2)])
    snap = load_snapshot(d)
    assert len(snap.rows) == 2
    assert snap.mode == "live"
    assert snap.folders == [654]


def test_checksum_mismatch_is_refused(tmp_path):
    """반쯤 전송된 파일을 읽으면 '일정이 절반 사라졌다' 로 보이고 대량 삭제가 된다."""
    d = _write_snapshot(tmp_path, [_row()], corrupt=True)
    with pytest.raises(SnapshotError, match="체크섬 불일치"):
        load_snapshot(d)


def test_missing_manifest_is_refused(tmp_path):
    d = tmp_path / "live-x"
    d.mkdir()
    (d / "schedule.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(SnapshotError, match=re.escape("manifest.json 이 없다")):
        load_snapshot(d)


def test_manifest_without_files_is_refused(tmp_path):
    d = tmp_path / "live-x"
    d.mkdir()
    (d / "schedule.jsonl").write_text("", encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"snapshot_id": "x"}), encoding="utf-8")
    with pytest.raises(SnapshotError, match="files 가 없다"):
        load_snapshot(d)


def test_broken_jsonl_line_is_refused(tmp_path):
    d = tmp_path / "live-x"
    d.mkdir()
    (d / "schedule.jsonl").write_text("{not json}\n", encoding="utf-8")
    digest = hashlib.sha256((d / "schedule.jsonl").read_bytes()).hexdigest()
    (d / "manifest.json").write_text(
        json.dumps({"files": {"schedule.jsonl": digest}}), encoding="utf-8"
    )
    with pytest.raises(SnapshotError, match="JSON 아님"):
        load_snapshot(d)


def test_manifest_fingerprint_is_stable():
    a, b = _snap(), _snap()
    assert manifest_sha256(a) == manifest_sha256(b)


def test_occurrence_content_fingerprint_is_stable_and_sensitive():
    row = _row()
    assert content_sha256(row) == content_sha256(dict(reversed(list(row.items()))))
    assert re.fullmatch(r"[0-9a-f]{64}", content_sha256(row))
    assert content_sha256(row) != content_sha256({**row, "place": "changed"})


# --- 검사 -------------------------------------------------------------------
def test_valid_snapshot_has_no_problems():
    assert check_snapshot(_snap()) == []


def test_unknown_mode_is_caught():
    assert any("mode" in p for p in check_snapshot(_snap(mode="이상한값")))


def test_empty_folder_list_is_caught():
    """폴더 목록이 없으면 삭제 판정 범위를 만들 수 없다."""
    problems = check_snapshot(_snap(folders=()))
    assert any("source_folders 가 비었다" in p for p in problems)


def test_backwards_horizon_is_caught():
    snap = _snap()
    snap.manifest["horizon_end"] = snap.manifest["horizon_start"]
    assert any("horizon_end" in p for p in check_snapshot(snap))


def test_row_count_mismatch_is_caught():
    """행이 잘려 왔는데 반영하면 나머지가 전부 삭제로 판정된다."""
    snap = _snap()
    snap.manifest["counts"]["schedule"] = 99
    assert any("행 수 불일치" in p for p in check_snapshot(snap))


def test_duplicate_primary_key_is_caught():
    snap = _snap([_row(date_id=1), _row(date_id=1)])
    assert any("중복 키" in p for p in check_snapshot(snap))


def test_row_from_folder_outside_manifest_is_caught():
    """폴더가 목록 밖이면 그 폴더는 삭제 판정 대상이 아니어서 오래된 행이 남는다."""
    snap = _snap([_row(folder=999)], folders=(654,))
    assert any("source_folders 에 없다" in p for p in check_snapshot(snap))


def test_end_before_start_is_caught():
    snap = _snap([_row(ends_at="2026-08-31T13:00:00+09:00")])
    assert any("종료가 시작보다 빠르다" in p for p in check_snapshot(snap))


def test_missing_field_is_caught():
    row = _row()
    del row["event_id"]
    assert any("필드 누락" in p for p in check_snapshot(_snap([row])))


def test_problem_messages_never_contain_subject_or_place():
    """검사 결과는 이력(schedule_sync_run.error)에 들어간다. 제목·장소를 남기지 않는다."""
    rows = [_row(subject="비밀 회의", place="비밀 장소", ends_at="2026-08-31T13:00:00+09:00")]
    text = " ".join(check_snapshot(_snap(rows)))
    assert "비밀 회의" not in text
    assert "비밀 장소" not in text
    assert "date_id" in text


# --- 가짜 DB ----------------------------------------------------------------
class FakeCopy:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write_row(self, row):
        self.sink.append(row)


class FakeCursor:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.executed.append((sql, params))
        self.c._last = sql
        self.rowcount = self.c.rowcounts.get(self.c._key(), 0)

    def copy(self, sql):
        self.c.executed.append((sql, None))
        return FakeCopy(self.c.copied)

    def fetchone(self):
        return self.c.answers.get(self.c._key())

    def fetchall(self):
        return self.c.answers.get(self.c._key()) or []

    @property
    def rowcount(self):
        return self.c.rowcounts.get(self.c._key(), 0)

    @rowcount.setter
    def rowcount(self, _v):
        pass


class FakeConn:
    def __init__(self, **answers):
        self.answers = answers
        self.rowcounts = answers.pop("rowcounts", {}) if "rowcounts" in answers else {}
        self.executed: list[tuple] = []
        self.copied: list[tuple] = []
        self.committed = False
        self.rolled_back = False
        self._last = ""

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def _key(self) -> str:
        s = self._last
        if "from schedule_sync_run" in s and "select status" in s:
            return "run_status"
        if "left join schedule_folder" in s:
            return "unknown"
        # 복구 건수 조회가 삭제 후보 조회보다 먼저 걸려야 한다 - 둘 다 count(*) 다.
        if "where o.source_deleted_at is not null" in s:
            return "restored"
        if "select count(*) as n" in s and "schedule_occurrence" in s:
            return "live_in_horizon"
        if "insert into schedule_occurrence" in s:
            return "upserted"
        if "update schedule_occurrence o" in s:
            return "deleted"
        if "details_purged_at = now()" in s:
            return "purged"
        return "?"

    def sql_for(self, needle: str) -> str:
        for sql, _ in self.executed:
            if needle in sql:
                return sql
        raise AssertionError(f"{needle} 를 실행하지 않았다")

    def params_for(self, needle: str):
        for sql, params in self.executed:
            if needle in sql:
                return params
        raise AssertionError(f"{needle} 를 실행하지 않았다")


def _conn(**kw) -> FakeConn:
    base = {
        "run_status": None,
        "unknown": {"n": 0},
        "live_in_horizon": {"n": 1},
        "restored": {"n": 0},
        "upserted": [{"inserted": True}],
        "rowcounts": {"deleted": 0, "purged": 0},
    }
    base.update(kw)
    return FakeConn(**base)


# --- 반영 -------------------------------------------------------------------
def test_check_failure_writes_nothing():
    conn = _conn()
    result = apply_snapshot(conn, _snap(folders=()))
    assert not result.ok
    assert conn.rolled_back and not conn.committed


def test_already_applied_snapshot_is_skipped():
    """같은 스냅샷을 두 번 반영하면 삭제 판정이 두 번 돌아 위험하다."""
    conn = _conn(run_status={"status": "applied"})
    result = apply_snapshot(conn, _snap())
    assert result.ok and result.already_applied
    assert not conn.committed


def test_force_reapplies_an_applied_snapshot():
    conn = _conn(run_status={"status": "applied"})
    result = apply_snapshot(conn, _snap(), force=True)
    assert result.ok and not result.already_applied


def test_dry_run_writes_nothing():
    conn = _conn(live_in_horizon={"n": 3})
    result = apply_snapshot(conn, _snap(), dry_run=True)
    assert result.ok
    assert conn.rolled_back and not conn.committed
    assert all("insert into schedule_occurrence" not in s for s, _ in conn.executed)


def test_delete_is_limited_to_horizon_and_folders():
    """이 조건이 빠지면 live 스냅샷 하나가 먼 미래 일정을 전부 지운다."""
    conn = _conn()
    apply_snapshot(conn, _snap())
    sql = conn.sql_for("update schedule_occurrence o")
    assert "source_folder_id = any(%(folders)s)" in sql
    assert "o.starts_at < %(horizon_end)s" in sql
    assert "o.ends_at   > %(horizon_start)s" in sql
    params = conn.params_for("update schedule_occurrence o")
    assert params["folders"] == [654]


def test_delete_is_soft_only():
    """물리 삭제하면 발송 이력이 가리킬 대상이 사라진다."""
    conn = _conn()
    apply_snapshot(conn, _snap())
    assert all("delete from schedule_occurrence" not in s.lower() for s, _ in conn.executed)
    assert "set source_deleted_at = now()" in conn.sql_for("update schedule_occurrence o")


def test_mass_delete_is_refused():
    """추출이 절반만 성공한 스냅샷을 '전부 취소됨' 으로 반영하지 않는다."""
    conn = _conn(live_in_horizon={"n": 100})
    result = apply_snapshot(conn, _snap([_row()]))
    assert not result.ok
    assert "삭제 판정이 지나치다" in result.message


def test_mass_delete_can_be_forced():
    conn = _conn(live_in_horizon={"n": 100})
    assert apply_snapshot(conn, _snap([_row()]), force=True).ok


def test_small_tables_are_not_blocked_by_the_ratio():
    """작은 표에서는 비율이 과민해진다 - 바닥 건수 아래는 통과시킨다."""
    conn = _conn(live_in_horizon={"n": MAX_DELETE_FLOOR})
    assert apply_snapshot(conn, _snap([])).ok


def test_upsert_does_not_restore_purged_details():
    """보존 기간이 지나 비운 제목을 스냅샷 한 번으로 되살리면 안 된다."""
    conn = _conn()
    apply_snapshot(conn, _snap())
    sql = conn.sql_for("insert into schedule_occurrence")
    assert "details_purged_at is null" in sql
    assert "else null end" in sql


def test_upsert_supplies_and_updates_content_hash():
    conn = _conn()
    apply_snapshot(conn, _snap())
    sql = conn.sql_for("insert into schedule_occurrence")
    assert "s.content_sha256" in sql
    assert "content_sha256 = excluded.content_sha256" in sql


def test_upsert_only_touches_approved_folders():
    """schedule_folder 가 허용 목록이다. 승인 전 폴더는 넣지 않는다."""
    conn = _conn(unknown={"n": 2})
    result = apply_snapshot(conn, _snap())
    assert "join schedule_folder f" in conn.sql_for("insert into schedule_occurrence")
    assert result.skipped_unknown_folder == 2


def test_returning_row_is_deleted_flag_reset():
    """다시 온 행은 되살린다 - 소프트 삭제가 영구가 되면 안 된다."""
    conn = _conn()
    apply_snapshot(conn, _snap())
    assert "source_deleted_at = null" in conn.sql_for("insert into schedule_occurrence")


def test_restore_count_is_measured_before_the_upsert():
    """ON CONFLICT DO UPDATE 의 RETURNING 은 새 값을 준다 - 거기서 세면 항상 0 이다."""
    conn = _conn(restored={"n": 3})
    result = apply_snapshot(conn, _snap())
    assert result.restored == 3
    order = [i for i, (s, _) in enumerate(conn.executed)
             if "where o.source_deleted_at is not null" in s
             or "insert into schedule_occurrence" in s]
    assert len(order) == 2 and order[0] < order[1]


def test_detail_retention_is_applied_after_upsert():
    conn = _conn()
    apply_snapshot(conn, _snap())
    params = conn.params_for("details_purged_at = now()")
    assert params["retention"] == DETAIL_RETENTION


def test_history_is_marked_applied_and_committed():
    conn = _conn()
    result = apply_snapshot(conn, _snap())
    assert result.ok and conn.committed
    assert "status = 'applied'" in conn.sql_for("update schedule_sync_run")


def test_staged_rows_carry_parsed_timestamps():
    conn = _conn()
    apply_snapshot(conn, _snap())
    (row,) = conn.copied
    assert row[0] == 654 and row[1] == 1
    assert row[5].astimezone(KST).hour == 14   # starts_at
    assert row[6].astimezone(KST).hour == 15   # ends_at


# --- inbox ------------------------------------------------------------------
def test_pending_returns_oldest_first(tmp_path):
    """live 스냅샷이 밀렸을 때 순서대로 반영해야 한다."""
    for name in ("live-2026-08-31_090000", "live-2026-08-31_090100"):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in pending_snapshots(tmp_path)] == [
        "live-2026-08-31_090000",
        "live-2026-08-31_090100",
    ]


def test_in_flight_and_processed_dirs_are_skipped(tmp_path):
    for name in (".live-uploading", "processed", "live-2026-08-31_090000"):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in pending_snapshots(tmp_path)] == ["live-2026-08-31_090000"]
    assert newest_snapshot(tmp_path).name == "live-2026-08-31_090000"


def test_dir_without_manifest_is_skipped(tmp_path):
    (tmp_path / "live-half").mkdir()
    assert pending_snapshots(tmp_path) == []
    assert newest_snapshot(tmp_path) is None


def test_missing_inbox_is_not_an_error(tmp_path):
    assert pending_snapshots(tmp_path / "없음") == []
    assert newest_snapshot(tmp_path / "없음") is None
