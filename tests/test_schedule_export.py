"""팀 일정 추출기 — 새면 안 되는 것과 놓치면 안 되는 것을 검사한다.

Oracle 없이 돌아간다. 가짜 커서로 뷰가 돌려줄 행을 흉내 낸다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "schedule_export", ROOT / "scripts" / "schedule_export.py"
)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

KST = timezone(timedelta(hours=9))


def _row(
    folder_id=100,
    event_id=1,
    occurrence_id=11,
    title="주간 공정회의",
    place="본사 3층 회의실",
    starts_at="2026-09-01 10:00",
    ends_at="2026-09-01 11:00",
    all_day="N",
    repeat="N",
    updated_at="2026-08-30 09:15:00",
):
    return (folder_id, event_id, occurrence_id, title, place,
            starts_at, ends_at, all_day, repeat, updated_at)


class FakeCursor:
    """뷰 두 개를 순서대로 돌려준다."""

    def __init__(self, folders, rows):
        self._folders = folders
        self._rows = rows
        self._pending: list = []
        self.executed: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))
        self._pending = self._folders if "FOLDER" in sql else self._rows

    def fetchall(self):
        return self._pending

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


# ---------------------------------------------------------------------------
# 시각 변환
# ---------------------------------------------------------------------------


def test_naive_time_becomes_kst():
    """원본에 시간대가 없다. 못 박지 않으면 받는 쪽이 9시간 어긋나게 알린다."""
    assert se.to_iso("2026-09-01 10:00") == "2026-09-01T10:00:00+09:00"
    assert se.to_iso("2026-08-30 09:15:00") == "2026-08-30T09:15:00+09:00"


def test_empty_time_is_none():
    assert se.to_iso(None) is None
    assert se.to_iso("  ") is None


def test_unknown_time_format_raises():
    """조용히 넘기면 알림 시각이 틀린 채로 전달된다."""
    with pytest.raises(se.ExportError):
        se.to_iso("2026/09/01 10:00")


# ---------------------------------------------------------------------------
# 행 변환
# ---------------------------------------------------------------------------


def test_record_shape():
    rec = se.to_record(_row())
    # 이름은 받는 쪽 schedule_occurrence 컬럼과 같다.
    assert rec == {
        "source_folder_id": 100,
        "date_id": 11,
        "event_id": 1,
        "subject": "주간 공정회의",
        "place": "본사 3층 회의실",
        "starts_at": "2026-09-01T10:00:00+09:00",
        "ends_at": "2026-09-01T11:00:00+09:00",
        "is_all_day": False,
        "is_repeat": False,
        "source_modified_at": "2026-08-30T09:15:00+09:00",
    }


def test_excluded_fields_never_appear():
    """설명·참석자·첨부는 뷰에 없다. 혹시 늘어나도 여기서 걸린다."""
    rec = se.to_record(_row())
    for banned in ("description", "attendees", "attachments", "owner", "share"):
        assert banned not in rec


def test_flags_are_booleans():
    rec = se.to_record(_row(all_day="Y", repeat="Y"))
    assert rec["is_all_day"] is True and rec["is_repeat"] is True


def test_blank_place_becomes_none():
    assert se.to_record(_row(place=""))["place"] is None


# ---------------------------------------------------------------------------
# 조회 — 기간이 겹치는 일정을 놓치지 않는가
# ---------------------------------------------------------------------------


def test_query_uses_overlap_not_start_only():
    """어제 시작해 오늘까지 이어지는 일정이 빠지면 안 된다."""
    cur = FakeCursor(folders=[(100, "팀 일정", "ABB155", "전산팀")], rows=[_row()])
    se.fetch(FakeConn(cur), "TYSLACK", "2026-09-01 00:00", "2026-09-03 00:00")

    sql = cur.executed[-1][0]
    assert "starts_at <= :range_end" in sql
    assert "ends_at   >= :range_start" in sql


def test_query_reads_only_the_dedicated_view():
    """화면용 사용자별 UNION 뷰를 쓰지 않는다."""
    cur = FakeCursor(folders=[(100, "팀 일정", "ABB155", "전산팀")], rows=[])
    se.fetch(FakeConn(cur), "TYSLACK", "a", "b")
    joined = " ".join(sql for sql, _ in cur.executed)
    assert "V_TYSLACK_SCHEDULE" in joined
    assert "V_SYS_CALENDAR" not in joined
    assert "UNION" not in joined.upper()
    assert "COVI_SMART4J" not in joined


# ---------------------------------------------------------------------------
# 스냅샷 파일
# ---------------------------------------------------------------------------


def _write(tmp_path, rows, *, folders=None, mode="live"):
    return se.write_snapshot(
        tmp_path / "snap",
        rows,
        mode=mode,
        range_start="2026-09-01 00:00",
        range_end="2026-09-03 00:00",
        folders=folders if folders is not None else [
            {"folder_id": 100, "folder_name": "팀 일정",
             "org_code": "ABB155", "org_name": "전산팀"},
            # 같은 폴더가 두 부서에 열려 있는 경우
            {"folder_id": 100, "folder_name": "팀 일정",
             "org_code": "C11502", "org_name": "전산팀 협력사"},
            {"folder_id": 200, "folder_name": "빈 폴더",
             "org_code": "ABB155", "org_name": "전산팀"},
        ],
        taken_at=datetime(2026, 8, 31, 14, 0, tzinfo=KST),
    )


def test_manifest_checksum_matches(tmp_path):
    out = _write(tmp_path, [se.to_record(_row())])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    actual = hashlib.sha256((out / "schedule.jsonl").read_bytes()).hexdigest()
    assert manifest["files"]["schedule.jsonl"] == actual


def test_manifest_lists_folders_with_no_events(tmp_path):
    """빈 폴더가 목록에서 빠지면 그 폴더의 묵은 일정이 영영 안 지워진다."""
    out = _write(tmp_path, [se.to_record(_row(folder_id=100))])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_folders"] == [100, 200]


def test_manifest_carries_folder_to_org_mapping(tmp_path):
    """어느 팀 채널에 알릴지는 이 매핑에서 나온다. 폴더를 손으로 등록하지 않는다."""
    out = _write(tmp_path, [se.to_record(_row())])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    pairs = {(f["folder_id"], f["org_code"]) for f in manifest["folders"]}
    assert pairs == {(100, "ABB155"), (100, "C11502"), (200, "ABB155")}


def test_folder_ids_are_deduped(tmp_path):
    """한 폴더가 두 부서에 열려 있어도 폴더는 하나다."""
    out = _write(tmp_path, [])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_folders"] == [100, 200]


def test_manifest_snapshot_id_matches_folder_name(tmp_path):
    """받는 쪽이 디렉터리 이름을 파싱하지 않아도 되게 manifest 에 넣는다."""
    out = _write(tmp_path, [])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == out.name


def test_schedule_rows_carry_no_org(tmp_path):
    """일정 행에 부서를 넣으면 한 일정이 부서 수만큼 중복된다.

    부서는 manifest 의 폴더 매핑에만 둔다.
    """
    rec = se.to_record(_row())
    assert "org_code" not in rec


def test_manifest_carries_mode_and_range(tmp_path):
    out = _write(tmp_path, [], mode="reconcile")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "reconcile"
    # 시간대가 붙어야 한다. 없으면 받는 쪽 timestamptz 가 서버 시간대로 읽는다.
    assert manifest["horizon_start"] == "2026-09-01T00:00:00+09:00"
    assert manifest["horizon_end"] == "2026-09-03T00:00:00+09:00"
    assert manifest["counts"]["schedule"] == 0


def test_empty_result_still_writes_files(tmp_path):
    """앞으로 이틀간 회의가 없을 수 있다. 0건은 사고가 아니다."""
    out = _write(tmp_path, [])
    assert (out / "schedule.jsonl").read_text(encoding="utf-8") == ""
    assert (out / "manifest.json").is_file()


def test_no_temp_file_left_behind(tmp_path):
    out = _write(tmp_path, [se.to_record(_row())])
    assert not list(out.glob("*.tmp"))


def test_jsonl_is_one_object_per_line(tmp_path):
    rows = [se.to_record(_row(occurrence_id=i)) for i in range(3)]
    out = _write(tmp_path, rows)
    lines = (out / "schedule.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["date_id"] for line in lines] == [0, 1, 2]


def test_korean_is_not_escaped(tmp_path):
    """받는 쪽이 사람 눈으로 확인할 수 있어야 한다."""
    out = _write(tmp_path, [se.to_record(_row())])
    assert "주간 공정회의" in (out / "schedule.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 기간 계산
# ---------------------------------------------------------------------------


def _args(mode, hours=None, days=None):
    return type("A", (), {"mode": mode, "horizon_hours": hours, "horizon_days": days})()


def test_live_defaults_to_48_hours():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=KST)
    start, end = se.resolve_range(_args("live"), now)
    assert end == "2026-09-03 12:00"
    # 시작을 1시간 앞으로 당긴다 — 진행 중인 일정을 흘리지 않기 위해서다.
    assert start == "2026-09-01 11:00"


def test_reconcile_defaults_to_30_days():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=KST)
    _, end = se.resolve_range(_args("reconcile"), now)
    assert end == "2026-10-01 12:00"


def test_explicit_horizon_wins():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=KST)
    _, end = se.resolve_range(_args("live", hours=6), now)
    assert end == "2026-09-01 18:00"


def test_range_format_matches_oracle_column():
    """`TO_DATE` 없이 문자열로 비교하므로 형식이 정확히 같아야 인덱스를 탄다."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=KST)
    start, end = se.resolve_range(_args("live"), now)
    for value in (start, end):
        assert len(value) == 16
        datetime.strptime(value, "%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 로그에 제목·장소가 새지 않는가
# ---------------------------------------------------------------------------


def test_cli_output_has_no_title_or_place(tmp_path, capsys, monkeypatch):
    cur = FakeCursor(folders=[(100, "팀 일정", "ABB155", "전산팀")], rows=[_row()])

    class Ctx:
        def __enter__(self):
            return FakeConn(cur)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(se, "connect", lambda: Ctx())
    rc = se.main(["--out", str(tmp_path), "--mode", "live"])
    assert rc == 0

    printed = capsys.readouterr().out
    assert "주간 공정회의" not in printed
    assert "본사 3층 회의실" not in printed
    assert "1건" in printed


def test_cli_refuses_when_no_folder_approved(tmp_path, capsys, monkeypatch):
    """허용 목록이 비면 받는 쪽이 판정할 범위가 없다. 파일을 만들지 않는다."""
    cur = FakeCursor(folders=[], rows=[])

    class Ctx:
        def __enter__(self):
            return FakeConn(cur)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(se, "connect", lambda: Ctx())
    rc = se.main(["--out", str(tmp_path), "--mode", "live"])
    assert rc == 1
    assert "승인된 일정 폴더가 없다" in capsys.readouterr().out
    assert not list(tmp_path.glob("*/manifest.json"))


def test_cli_refuses_when_query_fails(tmp_path, capsys, monkeypatch):
    """조회 실패를 '일정 없음' 으로 넘기면 받는 쪽이 전부 삭제로 판정한다."""

    class Ctx:
        def __enter__(self):
            raise RuntimeError("ORA-00942: table or view does not exist")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(se, "connect", lambda: Ctx())
    rc = se.main(["--out", str(tmp_path), "--mode", "live"])
    assert rc == 1
    assert "조회 실패" in capsys.readouterr().out
    assert not list(tmp_path.glob("*/manifest.json"))


def test_cli_rejects_conflicting_horizons(tmp_path, capsys):
    rc = se.main(["--out", str(tmp_path), "--mode", "live",
                  "--horizon-hours", "48", "--horizon-days", "30"])
    assert rc == 2
    assert "함께 쓸 수 없다" in capsys.readouterr().out
