"""조직·인사 스냅샷 반영 — 막아야 하는 것이 실제로 막히는지 본다.

이 잡이 잘못 돌면 권한이 조용히 넓어지거나 좁아진다. 그래서 '되는 경우'보다
**'안 되어야 하는 경우'**를 더 많이 검사한다.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tybot.orgsync import (
    MIN_RATIO,
    Snapshot,
    SnapshotError,
    check_snapshot,
    load_snapshot,
)


def _org(code, name="조직", parent=None, kind="team", company="TY", active=True):
    return {
        "org_code": code, "org_name": name, "parent_code": parent,
        "kind": kind, "company_code": company, "active": active,
    }


def _emp(no, name="홍길동", email=None, org="A", position="공통", active=True):
    return {
        "emp_no": no, "name": name, "email": email,
        "org_code": org, "position": position, "active": active,
    }


def _snap(org=None, emp=None, tmp_path=None):
    return Snapshot(
        org=org if org is not None else [_org("A")],
        emp=emp if emp is not None else [_emp("1")],
        source=tmp_path or __import__("pathlib").Path("."),
    )


# ---------------------------------------------------------------------------
# 파일 읽기 — 체크섬
# ---------------------------------------------------------------------------


def _make_dir(tmp_path, org_rows, emp_rows, *, break_checksum=False, omit=None):
    def write(name, rows):
        path = tmp_path / name
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        return hashlib.sha256(path.read_bytes()).hexdigest()

    files = {"org.jsonl": write("org.jsonl", org_rows),
             "emp.jsonl": write("emp.jsonl", emp_rows)}
    if break_checksum:
        files["org.jsonl"] = "0" * 64
    if omit:
        (tmp_path / omit).unlink()
    (tmp_path / "manifest.json").write_text(
        json.dumps({"taken_at": "2026-08-31T03:30:00+09:00", "files": files}),
        encoding="utf-8",
    )
    return tmp_path


def test_load_reads_snapshot(tmp_path):
    _make_dir(tmp_path, [_org("A")], [_emp("1", org="A")])
    snap = load_snapshot(tmp_path)
    assert len(snap.org) == 1 and len(snap.emp) == 1
    assert snap.taken_at == "2026-08-31T03:30:00+09:00"


def test_checksum_mismatch_is_refused(tmp_path):
    """전송이 손상되거나 아직 안 끝난 파일을 반영하면 대량 비활성화로 이어진다."""
    _make_dir(tmp_path, [_org("A")], [_emp("1", org="A")], break_checksum=True)
    with pytest.raises(SnapshotError, match="체크섬"):
        load_snapshot(tmp_path)


def test_missing_file_is_refused(tmp_path):
    _make_dir(tmp_path, [_org("A")], [_emp("1", org="A")], omit="emp.jsonl")
    with pytest.raises(SnapshotError, match=r"emp\.jsonl"):
        load_snapshot(tmp_path)


def test_missing_manifest_is_refused(tmp_path):
    (tmp_path / "org.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="manifest"):
        load_snapshot(tmp_path)


def test_broken_json_line_is_refused(tmp_path):
    _make_dir(tmp_path, [_org("A")], [_emp("1", org="A")])
    path = tmp_path / "org.jsonl"
    path.write_text('{"org_code": "A"\n', encoding="utf-8")
    files = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    files["files"]["org.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(files), encoding="utf-8")
    with pytest.raises(SnapshotError, match="JSON 아님"):
        load_snapshot(tmp_path)


# ---------------------------------------------------------------------------
# 검사 — 통과해야 하는 것
# ---------------------------------------------------------------------------


def test_clean_snapshot_passes():
    snap = _snap(
        org=[_org("HQ", kind="hq"), _org("T1", parent="HQ")],
        emp=[_emp("1", org="T1")],
    )
    assert check_snapshot(snap) == []


def test_employee_without_org_is_allowed():
    """소속이 비어 있는 사람은 있다. 권한을 넓히지 않을 뿐 반영은 된다."""
    snap = _snap(org=[_org("A")], emp=[_emp("1", org=None)])
    assert check_snapshot(snap) == []


# ---------------------------------------------------------------------------
# 검사 — 막아야 하는 것
# ---------------------------------------------------------------------------


def test_empty_snapshot_is_refused():
    problems = check_snapshot(_snap(org=[], emp=[]))
    assert any("조직 스냅샷이 비어" in p for p in problems)
    assert any("인사 스냅샷이 비어" in p for p in problems)


def test_sudden_shrink_is_refused():
    """원본 조회가 반쯤 실패한 스냅샷을 '전원 퇴직'으로 오인하지 않게 한다."""
    snap = _snap(org=[_org(f"O{i}") for i in range(50)],
                 emp=[_emp(str(i), org="O0") for i in range(50)])
    assert check_snapshot(snap, previous_org=50, previous_emp=50) == []

    problems = check_snapshot(snap, previous_org=1000, previous_emp=1000)
    assert any("조직 행 수가 급감" in p for p in problems)
    assert any("인사 행 수가 급감" in p for p in problems)


def test_shrink_just_within_threshold_passes():
    n = 100
    snap = _snap(org=[_org(f"O{i}") for i in range(int(n * MIN_RATIO))],
                 emp=[_emp(str(i), org="O0") for i in range(n)])
    assert not any("급감" in p for p in check_snapshot(snap, previous_org=n))


def test_cycle_is_refused():
    """순환이 있으면 권한 상속 재귀 조회가 무한히 돈다."""
    snap = _snap(org=[_org("A", parent="B"), _org("B", parent="A")], emp=[_emp("1")])
    assert any("순환 참조" in p for p in check_snapshot(snap))


def test_self_parent_is_refused():
    snap = _snap(org=[_org("A", parent="A")], emp=[_emp("1")])
    assert any("순환 참조" in p for p in check_snapshot(snap))


def test_orphan_parent_is_refused():
    """부모가 없으면 트리가 끊겨 상속이 조용히 어긋난다."""
    snap = _snap(org=[_org("A", parent="없는코드")], emp=[_emp("1")])
    assert any("부모가 스냅샷에 없는" in p for p in check_snapshot(snap))


def test_employee_pointing_at_unknown_org_is_refused():
    snap = _snap(org=[_org("A")], emp=[_emp("1", org="B")])
    assert any("소속 조직이 스냅샷에 없는" in p for p in check_snapshot(snap))


def test_duplicate_keys_are_refused():
    dup_org = _snap(org=[_org("A"), _org("A")], emp=[_emp("1")])
    assert any("조직코드 중복" in p for p in check_snapshot(dup_org))

    dup_emp = _snap(org=[_org("A")], emp=[_emp("1"), _emp("1")])
    assert any("사번 중복" in p for p in check_snapshot(dup_emp))


def test_unknown_kind_is_refused():
    """받는 쪽 org_unit.kind 에 CHECK 제약이 있어 반영이 통째로 실패한다."""
    snap = _snap(org=[_org("A", kind="본부")], emp=[_emp("1")])
    assert any("모르는 조직 구분" in p for p in check_snapshot(snap))


def test_missing_name_is_refused():
    """employee.name 은 NOT NULL 이다. 한 건 때문에 전량 반영이 롤백된다."""
    snap = _snap(org=[_org("A")], emp=[_emp("1", name=None)])
    assert any("name 가 비었다" in p for p in check_snapshot(snap))


def test_pii_in_snapshot_is_refused():
    """뷰에서 걸렀어야 하는 것이 새어 들어왔다는 뜻이다. 반영하지 않는다."""
    snap = _snap(org=[_org("A")], emp=[_emp("1", position="900101-1234567")])
    assert any("주민등록번호" in p for p in check_snapshot(snap))

    phone = _snap(org=[_org("A")], emp=[_emp("1", position="010-1234-5678")])
    assert any("휴대전화번호" in p for p in check_snapshot(phone))


def test_pii_check_does_not_leak_the_value():
    """경고 문구에 원본 값이 실려 나가면 그것 자체가 유출이다."""
    snap = _snap(org=[_org("A")], emp=[_emp("1", position="900101-1234567")])
    problems = check_snapshot(snap)
    assert all("900101" not in p for p in problems)


# ---------------------------------------------------------------------------
# 실제 DB 반영 — DATABASE_URL 이 있을 때만
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn():
    import os

    psycopg = pytest.importorskip("psycopg", reason="psycopg 없음")
    from tybot.envfile import load_env_file

    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL 없음")
    conn = psycopg.connect(url)
    yield conn
    conn.rollback()
    conn.close()


def test_dry_run_passes_and_writes_nothing(db_conn):
    """검사는 실 DB 의 현재 행 수와 비교한다. 그래서 스냅샷 크기를 거기에 맞춘다.

    apply_snapshot 은 스스로 commit 하므로 실 DB 를 건드리지 않도록 dry-run 만 쓴다.
    """
    from tybot.orgsync import apply_snapshot

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from org_unit where active")
        live_org = cur.fetchone()[0]
        cur.execute("select count(*) from employee where active")
        live_emp = cur.fetchone()[0]

    org = [_org("ZZTEST_HQ", kind="hq")]
    org += [_org(f"ZZTEST_{i}", parent="ZZTEST_HQ") for i in range(live_org)]
    emp = [_emp(f"ZZTEST_{i}", org="ZZTEST_HQ") for i in range(live_emp + 1)]

    result = apply_snapshot(db_conn, _snap(org=org, emp=emp), dry_run=True)
    assert result.ok, result.problems
    assert "쓰지 않았다" in result.message

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from org_unit where code like 'ZZTEST%'")
        assert cur.fetchone()[0] == 0


def test_apply_refuses_shrunken_snapshot_against_live_counts(db_conn):
    from tybot.orgsync import apply_snapshot

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from org_unit where active")
        live = cur.fetchone()[0]
    if live < 10:
        pytest.skip("실 데이터가 적어 급감 판정을 시험할 수 없다")

    result = apply_snapshot(db_conn, _snap(org=[_org("A")], emp=[_emp("1")]))
    assert not result.ok
    assert any("급감" in p for p in result.problems)
