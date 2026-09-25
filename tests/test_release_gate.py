"""스키마 검증 게이트 — **「했다고 기억하는」 것을 막는다.**

결정: 2026-09-25 오너 §9·§10.

회사 PostgreSQL 의 DBA 권한이 없어 격리 DB 검증을 못 하는 동안에도 개발은
계속한다. 그러면 검증이 끝나지 않았다는 사실이 **코드에 남아 있어야** 한다.

안 남기면 두 가지가 일어난다. 사람은 언젠가 검증을 했다고 기억하고, 콘솔은
`active` 버튼을 그냥 눌러 준다. 그 순간 운영 원문의 주인이 검증 안 된 표를 보고
바뀐다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tybot.console import release_gate as gate


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    return tmp_path


# --- 기본은 닫혀 있다 ---------------------------------------------------------

def test_the_gate_is_closed_before_any_verification():
    status = gate.gate_status()

    assert status.verified is False
    assert "격리 DB 검증" in status.reason


def test_a_closed_gate_says_what_to_do():
    """「검증 안 됨」 만 보여 주면 사람이 무엇을 해야 하는지 모르고, 모르면 안 한다."""
    assert "verify_schema_isolated.py" in gate.gate_status().reason


def test_recording_a_pass_opens_the_gate():
    gate.record_pass(by="dba", dsn_label="tybot_schema_test")

    status = gate.gate_status()

    assert status.verified is True
    assert status.verified_by == "dba"
    assert status.verified_dsn_label == "tybot_schema_test"


def test_a_portable_artifact_does_not_open_the_local_gate(tmp_path):
    artifact = tmp_path / "verification.json"

    gate.record_pass(
        by="developer", dsn_label="tybot_schema_test", destination=artifact
    )

    assert artifact.is_file()
    assert gate.gate_status().verified is False


def test_matching_artifact_can_be_installed(tmp_path):
    artifact = tmp_path / "verification.json"
    gate.record_pass(
        by="developer", dsn_label="tybot_schema_test", destination=artifact
    )

    installed = gate.install_verified_artifact(artifact, installed_by="operator")

    assert installed == gate.marker_path()
    assert gate.gate_status().verified is True
    payload = json.loads(installed.read_text(encoding="utf-8"))
    assert payload["installed_by"] == "operator"


def test_artifact_for_another_schema_is_refused(tmp_path, monkeypatch):
    artifact = tmp_path / "verification.json"
    gate.record_pass(
        by="developer", dsn_label="tybot_schema_test", destination=artifact
    )
    monkeypatch.setattr(gate, "schema_fingerprint", lambda *_: "f" * 64)

    with pytest.raises(gate.GateClosed, match="스키마가 다릅니다"):
        gate.install_verified_artifact(artifact, installed_by="operator")

    assert not gate.marker_path().is_file()


# --- 지문에 묶인다 -----------------------------------------------------------

def test_changing_the_schema_closes_the_gate_again(monkeypatch, tmp_path):
    """**이게 이 파일의 이유다.**

    「한 번 검증했으니 됐다」 로 두면, 검증 뒤에 고친 부분은 아무도 확인하지 않은
    채로 운영에 간다. 그리고 고치는 것은 늘 검증 뒤다.
    """
    gate.record_pass(by="dba", dsn_label="tybot_schema_test")
    assert gate.gate_status().verified is True

    # 스키마가 한 글자 바뀐 상태를 만든다
    monkeypatch.setattr(gate, "schema_fingerprint", lambda *_: "f" * 64)

    status = gate.gate_status()

    assert status.verified is False
    assert "검증한 뒤 스키마가 바뀌었습니다" in status.reason


def test_the_reason_shows_both_fingerprints(monkeypatch):
    """어느 쪽이 바뀌었는지 못 보면 사람이 원인을 코드에서 찾는다."""
    gate.record_pass(by="dba", dsn_label="db")
    monkeypatch.setattr(gate, "schema_fingerprint", lambda *_: "a" * 64)

    reason = gate.gate_status().reason

    assert "aaaaaaaaaaaa" in reason
    assert "다시 검증하세요" in reason


def test_a_missing_sql_file_changes_the_fingerprint(tmp_path):
    """조용히 건너뛰면 **파일이 사라진 것과 검증된 것이 구분되지 않는다.**"""
    full = tmp_path / "full"
    partial = tmp_path / "partial"
    for base in (full, partial):
        base.mkdir()
    for name in gate.GATED_SQL:
        (full / name).write_text("-- x", encoding="utf-8")
        if name != "archiving_schema.sql":
            (partial / name).write_text("-- x", encoding="utf-8")

    assert gate.schema_fingerprint(full) != gate.schema_fingerprint(partial)


def test_swapping_file_contents_changes_the_fingerprint(tmp_path):
    """길이 접두어가 없으면 파일을 갈라 붙인 다른 조합이 같은 지문을 낸다."""
    a, b = tmp_path / "a", tmp_path / "b"
    for base in (a, b):
        base.mkdir()
        for name in gate.GATED_SQL:
            (base / name).write_text("-- same", encoding="utf-8")
    (b / "archiving_schema.sql").write_text("-- different", encoding="utf-8")

    assert gate.schema_fingerprint(a) != gate.schema_fingerprint(b)


def test_the_fingerprint_is_stable_for_the_same_content(tmp_path):
    base = tmp_path / "s"
    base.mkdir()
    for name in gate.GATED_SQL:
        (base / name).write_text("-- x", encoding="utf-8")

    assert gate.schema_fingerprint(base) == gate.schema_fingerprint(base)


# --- 기록이 깨져 있으면 닫는다 ------------------------------------------------

def test_a_corrupt_marker_closes_the_gate(state):
    path = gate.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    status = gate.gate_status()

    assert status.verified is False
    assert "읽지 못했습니다" in status.reason


def test_a_marker_without_a_fingerprint_closes_the_gate(state):
    path = gate.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"by": "dan"}), encoding="utf-8")

    assert gate.gate_status().verified is False


# --- 비밀이 상태 파일에 남지 않는다 -------------------------------------------

def test_the_marker_stores_a_database_name_not_a_dsn():
    """DSN 전체를 적으면 비밀번호가 상태 파일에 남고, 상태 파일은 로그처럼 복사된다."""
    path = gate.record_pass(by="dba", dsn_label="tybot_schema_test")

    body = path.read_text(encoding="utf-8")

    assert "tybot_schema_test" in body
    for secret in ("://", "@", "password", "postgresql"):
        assert secret not in body, secret


def test_the_marker_lives_outside_the_database():
    """검증 대상이 DB 인데 결과를 그 DB 에 적으면, 표가 잘못 섰을 때 결과도 못 읽는다."""
    source = Path(gate.__file__).read_text(encoding="utf-8")

    for keyword in ("SELECT ", "INSERT ", "psycopg", "_connect"):
        assert keyword not in source, keyword


# --- 막는 쪽 ------------------------------------------------------------------

def test_require_raises_with_the_action_name():
    """「게이트가 닫혔습니다」 만 보여 주면 무엇을 하려다 막혔는지 화면에서 못 읽는다."""
    with pytest.raises(gate.GateClosed) as caught:
        gate.require_verified_schema("active 전환")

    assert str(caught.value).startswith("active 전환:")


def test_require_passes_once_verified():
    gate.record_pass(by="dba", dsn_label="db")

    gate.require_verified_schema("active 전환")


def test_the_gated_files_match_what_the_verifier_applies():
    """게이트가 잰 것과 검증이 돌린 것이 다르면, 게이트가 엉뚱한 것을 지킨다."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import verify_schema_isolated as verify

    assert set(gate.GATED_SQL) == set(verify.TARGET_FILES)


def test_the_verifier_records_the_pass_only_after_success():
    """실패한 검증이 게이트를 열면 게이트가 없는 것과 같다."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import verify_schema_isolated as verify

    source = Path(verify.__file__).read_text(encoding="utf-8")
    body = source[source.index("def main()"):]

    assert body.index("if failures:") < body.index("record_pass(")


def test_the_status_is_json_ready():
    """화면이 그대로 보여 줄 수 있어야 한다. 가공을 화면에 맡기면 화면마다 달라진다."""
    gate.record_pass(by="dba", dsn_label="tybot_schema_test")

    payload = gate.gate_status().as_json()

    assert payload["verified"] is True
    assert len(payload["currentFingerprint"]) == 12
    assert json.dumps(payload)
