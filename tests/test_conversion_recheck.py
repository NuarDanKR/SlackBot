"""B-89 야간 변환 재점검 — 바뀐 것만 다시 열고, 정책 제외는 열지 않는다."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tybot import conversion_queue as queue
from tybot import conversion_recheck as recheck
from tybot.archive import converter_bots
from tybot.archive.converter_bots import DELEGATED_CODE

NOW = datetime(2026, 11, 11, 18, 30, tzinfo=UTC)  # 03:30 KST


def _staged(
    tmp_path,
    file_id,
    *,
    status,
    error_code="",
    filetype="pdf",
    name="",
    converter_version="",
    channel_id="C1",
    staged_at="2026-11-10T00:00:00+00:00",
    original=True,
):
    d = (
        tmp_path
        / "staging"
        / "workspaces"
        / "pilot"
        / "channels"
        / channel_id
        / "attachments"
        / file_id
    )
    d.mkdir(parents=True, exist_ok=True)
    obj = d / f"original.{filetype}"
    if original:
        obj.write_bytes(b"raw")
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "name": name or f"{file_id}.{filetype}",
                "filetype": filetype,
                "status": status,
                "error_code": error_code,
                "retryable": False,
                "sha256": "abc123",
                "conversion_state": "failed",
                "converter_version": converter_version,
                "object_path": str(obj),
                "staged_at": staged_at,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def no_delegation(monkeypatch):
    monkeypatch.setattr(converter_bots, "conversion_delegated", lambda: False)


@pytest.fixture
def delegation_on(monkeypatch):
    monkeypatch.setattr(converter_bots, "conversion_delegated", lambda: True)


@pytest.fixture
def enqueued(monkeypatch):
    added: list[dict] = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: added.append(kw) or len(added))
    return added


# --- 판정 (순수 함수) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "recorded", "current", "convertible", "expected"),
    [
        # 정책 제외는 변환기가 바뀌어도 열지 않는다
        ("pii_refused", "pii_refused", "1", "4", True, False),
        ("download_or_extract_failed", "pii_refused", "1", "4", True, False),
        ("unsupported", "pii_refused", "", "4", True, False),
        ("pii_refused", "unsupported", "", "4", True, False),
        # 원본 문제는 다시 돌릴 대상이 없다
        ("download_or_extract_failed", "original_changed", "1", "4", True, False),
        ("download_or_extract_failed", "original_missing", "1", "4", True, False),
        # 미지원 → 지금 변환할 수 있으면 연다
        ("unsupported", "unsupported", "4", "4", True, True),
        ("unsupported", "", "", "4", True, True),
        ("unsupported", "unsupported", "", "4", False, False),
        # rhwp 위임 기록 — 위임이 꺼져 변환할 수 있게 된 경우만
        ("unsupported", DELEGATED_CODE, "2", "2", True, True),
        ("unsupported", DELEGATED_CODE, "2", "2", False, False),
        # 영구 판정은 변환기 판이 바뀐 것만
        ("download_or_extract_failed", "encrypted", "4", "4", True, False),
        ("download_or_extract_failed", "encrypted", "3", "4", True, True),
        ("download_or_extract_failed", "corrupt", "3", "4", True, True),
        ("download_or_extract_failed", "empty_output", "3", "4", True, True),
        # 판이 바뀌었어도 지금 변환할 수 없으면(위임 중 HWP 등) 열지 않는다
        ("download_or_extract_failed", "corrupt", "3", "4", False, False),
        # 판이 기록되지 않은 옛 metadata 는 「무엇이 바뀌었는지 모른다」 → 열지 않는다
        ("download_or_extract_failed", "corrupt", "", "4", True, False),
        # 일시 실패는 이 함수의 몫이 아니다(`nightly_backfill`)
        ("download_or_extract_failed", "converter_crashed", "3", "4", True, False),
    ],
)
def test_verdict_changed(status, code, recorded, current, convertible, expected):
    assert (
        recheck.verdict_changed(
            status=status,
            error_code=code,
            recorded_version=recorded,
            current_version=current,
            convertible_now=convertible,
        )
        is expected
    )


def test_changed_code_is_retryable_under_queue_policy():
    """사유 코드가 큐 정책에 막히면 `force` 없이는 아무것도 안 올라간다."""
    assert queue.is_retryable(recheck.CHANGED_CODE, True)
    assert recheck.CHANGED_CODE not in queue.FORCE_BLOCKED


def test_recheck_limit_parsing():
    assert recheck.recheck_limit({}) == recheck.DEFAULT_RECHECK_LIMIT
    assert recheck.recheck_limit({recheck.RECHECK_LIMIT_ENV: "7"}) == 7
    assert recheck.recheck_limit({recheck.RECHECK_LIMIT_ENV: "0"}) == 0
    assert recheck.recheck_limit({recheck.RECHECK_LIMIT_ENV: "-3"}) == 0
    assert recheck.recheck_limit({recheck.RECHECK_LIMIT_ENV: "x"}) == recheck.DEFAULT_RECHECK_LIMIT


def test_rhwp_grace_parsing():
    assert recheck.rhwp_grace({}) == recheck.DEFAULT_RHWP_GRACE
    assert recheck.rhwp_grace({recheck.RHWP_GRACE_ENV: "2"}) == timedelta(hours=2)
    assert recheck.rhwp_grace({recheck.RHWP_GRACE_ENV: "0"}) == recheck.DEFAULT_RHWP_GRACE


# --- 판정 변경 소급 ------------------------------------------------------------


def test_changed_backfill_reopens_only_changed_verdicts(tmp_path, no_delegation, enqueued, capsys):
    import drain_conversion_queue as drain

    current = drain.converter_version("pdf")
    _staged(
        tmp_path,
        "FUNSUP",
        status="unsupported",
        error_code="unsupported",
        converter_version=current,
    )
    _staged(
        tmp_path,
        "FENCOLD",
        status="download_or_extract_failed",
        error_code="encrypted",
        converter_version="1",
    )
    _staged(
        tmp_path,
        "FENCSAME",
        status="download_or_extract_failed",
        error_code="encrypted",
        converter_version=current,
    )
    _staged(tmp_path, "FPII", status="pii_refused", error_code="pii_refused", converter_version="1")
    _staged(
        tmp_path,
        "FSWAP",
        status="download_or_extract_failed",
        error_code="original_changed",
        converter_version="1",
    )

    assert drain.changed_backfill(str(tmp_path / "archive"), apply=True) == 0

    assert {item["file_id"] for item in enqueued} == {"FUNSUP", "FENCOLD"}
    for item in enqueued:
        assert item["error_code"] == recheck.CHANGED_CODE
        assert item["force"] is False
        assert item["retryable"] is True
        assert item["converter_version"] == current
        # 옛 판으로 닫힌 행이 새 시도를 가리지 않게 키에 지금 판이 들어간다
        assert item["pipeline_version"] == (f"{queue.PIPELINE_VERSION}:pdf:primary:{current}")
    assert "정책 제외 1건" in capsys.readouterr().out


def test_changed_backfill_never_forces_policy_exclusions(tmp_path, no_delegation, monkeypatch):
    """pii_refused 가 어떤 경로로든 큐에 닿으면 그 자체가 결함이다."""
    import drain_conversion_queue as drain

    _staged(tmp_path, "FPII", status="pii_refused", error_code="pii_refused")
    _staged(tmp_path, "FPII2", status="unsupported", error_code="pii_refused")
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("must not enqueue"))

    assert drain.changed_backfill(str(tmp_path / "archive"), apply=True) == 0


def test_changed_backfill_respects_the_nightly_limit(tmp_path, no_delegation, enqueued):
    import drain_conversion_queue as drain

    for index in range(3):
        _staged(tmp_path, f"F{index}", status="unsupported", error_code="unsupported")
    report = recheck.NightlyReport()

    drain.changed_backfill(str(tmp_path / "archive"), apply=True, limit=1, report=report)

    assert len(enqueued) == 1
    assert report.changed_candidates == 3
    assert report.changed_enqueued == 1
    assert report.deferred_to_next_night == 2


def test_changed_backfill_is_a_dry_run(tmp_path, no_delegation, monkeypatch, capsys):
    import drain_conversion_queue as drain

    _staged(tmp_path, "F1", status="unsupported", error_code="unsupported")
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("dry run must not enqueue"))

    assert drain.changed_backfill(str(tmp_path / "archive"), apply=False) == 0
    assert "--nightly --apply" in capsys.readouterr().out


def test_changed_backfill_skips_missing_originals(tmp_path, no_delegation, monkeypatch):
    import drain_conversion_queue as drain

    _staged(tmp_path, "F1", status="unsupported", error_code="unsupported", original=False)
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("no original"))
    report = recheck.NightlyReport()

    drain.changed_backfill(str(tmp_path / "archive"), apply=True, report=report)
    assert report.original_missing == 1


def test_delegated_hwp_is_not_converted_while_delegation_is_on(
    tmp_path, delegation_on, monkeypatch
):
    """위임 중에 TYBot 이 직접 변환하면 같은 문서가 두 벌의 근거가 된다."""
    import drain_conversion_queue as drain

    _staged(tmp_path, "FHWP", status="unsupported", error_code=DELEGATED_CODE, filetype="hwp")
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("delegated"))

    assert drain.changed_backfill(str(tmp_path / "archive"), apply=True) == 0


def test_delegated_hwp_is_reopened_once_delegation_is_off(tmp_path, no_delegation, enqueued):
    import drain_conversion_queue as drain

    _staged(
        tmp_path,
        "FHWP",
        status="unsupported",
        error_code=DELEGATED_CODE,
        filetype="hwp",
        converter_version=drain.converter_version("hwp"),
    )

    drain.changed_backfill(str(tmp_path / "archive"), apply=True)
    assert [item["file_id"] for item in enqueued] == ["FHWP"]
    assert enqueued[0]["converter"] == "hwp:primary"


# --- rhwp 위임 대조 ------------------------------------------------------------


def _scan(tmp_path):
    from tybot.attachment_review import scan

    return scan(tmp_path / "archive")


def test_pending_delegation_without_a_pdf(tmp_path):
    _staged(
        tmp_path,
        "FHWP",
        status="unsupported",
        error_code=DELEGATED_CODE,
        filetype="hwp",
        name="주간보고.hwp",
    )

    pending = recheck.pending_delegations(_scan(tmp_path), now=NOW, grace=timedelta(hours=6))

    assert [(p.channel_id, p.file_id) for p in pending] == [("C1", "FHWP")]
    # 좌표만 — 파일 이름은 담지 않는다
    assert "주간보고" not in json.dumps([p.__dict__ for p in pending], ensure_ascii=False)


def test_delegation_with_a_matching_pdf_is_complete(tmp_path):
    _staged(
        tmp_path,
        "FHWP",
        status="unsupported",
        error_code=DELEGATED_CODE,
        filetype="hwp",
        name="주간보고.HWP",
    )
    _staged(tmp_path, "FPDF", status="converted", filetype="pdf", name="주간보고.pdf")

    assert recheck.pending_delegations(_scan(tmp_path), now=NOW, grace=timedelta(hours=6)) == []


def test_pdf_in_another_channel_does_not_complete_a_delegation(tmp_path):
    _staged(
        tmp_path,
        "FHWP",
        status="unsupported",
        error_code=DELEGATED_CODE,
        filetype="hwp",
        name="주간보고.hwp",
    )
    _staged(
        tmp_path, "FPDF", status="converted", filetype="pdf", name="주간보고.pdf", channel_id="C2"
    )

    pending = recheck.pending_delegations(_scan(tmp_path), now=NOW, grace=timedelta(hours=6))
    assert [p.file_id for p in pending] == ["FHWP"]


def test_recent_delegation_is_still_in_progress(tmp_path):
    _staged(
        tmp_path,
        "FHWP",
        status="unsupported",
        error_code=DELEGATED_CODE,
        filetype="hwp",
        staged_at=(NOW - timedelta(hours=1)).isoformat(),
    )

    assert recheck.pending_delegations(_scan(tmp_path), now=NOW, grace=timedelta(hours=6)) == []


# --- 야간 한 회차 ---------------------------------------------------------------


def test_nightly_dry_run_writes_nothing(tmp_path, no_delegation, monkeypatch, capsys):
    import drain_conversion_queue as drain

    _staged(tmp_path, "F1", status="unsupported", error_code="unsupported")
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("dry run"))
    monkeypatch.setattr(drain, "drain", lambda *_a, **_k: pytest.fail("dry run"))

    assert drain.run_nightly(str(tmp_path / "archive"), apply=False, limit=5) == 0
    assert not recheck.report_path(tmp_path / "archive").exists()
    assert "convert_nightly_metrics" in capsys.readouterr().out


def test_nightly_apply_records_metrics_without_names(
    tmp_path, no_delegation, enqueued, monkeypatch
):
    import drain_conversion_queue as drain

    _staged(
        tmp_path,
        "FT",
        status="download_or_extract_failed",
        error_code="converter_crashed",
        name="급여명세.pdf",
    )
    meta = tmp_path / "staging/workspaces/pilot/channels/C1/attachments/FT/metadata.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["retryable"] = True
    meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _staged(tmp_path, "FU", status="unsupported", error_code="unsupported", name="계약자명단.pdf")
    _staged(tmp_path, "FP", status="pii_refused", error_code="pii_refused")
    monkeypatch.setenv(recheck.RECHECK_LIMIT_ENV, "10")

    def fake_drain(_archive, *, limit, report=None):
        assert limit == 5
        report.processed, report.succeeded, report.failed = 2, 1, 1
        return drain.EXIT_FAILED

    monkeypatch.setattr(drain, "drain", fake_drain)

    assert drain.run_nightly(str(tmp_path / "archive"), apply=True, limit=5) == drain.EXIT_FAILED

    saved = recheck.read_report(tmp_path / "archive")
    assert saved["retry_enqueued"] == 1
    assert saved["changed_enqueued"] == 1
    assert saved["excluded_policy"] == 1
    assert saved["recheck_limit"] == 10
    assert saved["success_rate"] == 0.5
    assert saved["exit_code"] == drain.EXIT_FAILED
    assert {item["file_id"] for item in enqueued} == {"FT", "FU"}
    text = recheck.report_path(tmp_path / "archive").read_text(encoding="utf-8")
    assert "급여명세" not in text and "계약자명단" not in text


def test_nightly_outdated_shares_the_recheck_budget(tmp_path, no_delegation, enqueued, monkeypatch):
    """판정 변경과 구버전 소급이 한 밤 상한을 나눠 쓴다."""
    import drain_conversion_queue as drain

    _staged(tmp_path, "FU", status="unsupported", error_code="unsupported")
    for index in range(3):
        d = f"FOLD{index}"
        _staged(tmp_path, d, status="converted", converter_version="1")
        meta = tmp_path / f"staging/workspaces/pilot/channels/C1/attachments/{d}/metadata.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["conversion_state"] = "succeeded"
        meta.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(recheck.RECHECK_LIMIT_ENV, "2")
    monkeypatch.setattr(drain, "drain", lambda *_a, **_k: drain.EXIT_OK)

    drain.run_nightly(str(tmp_path / "archive"), apply=True, limit=5)

    saved = recheck.read_report(tmp_path / "archive")
    assert saved["changed_enqueued"] == 1
    assert saved["outdated_candidates"] == 3
    assert saved["outdated_enqueued"] == 1
    assert saved["deferred_to_next_night"] == 2
    assert len(enqueued) == 2


def test_report_is_unknown_not_zero_when_nothing_ran():
    assert recheck.NightlyReport().to_json()["success_rate"] is None


def test_main_nightly_routes_to_the_recheck_round(monkeypatch, tmp_path):
    import drain_conversion_queue as drain

    seen = {}
    monkeypatch.setattr(drain, "load_env_file", lambda: None)
    monkeypatch.setattr(
        drain,
        "run_nightly",
        lambda archive, *, apply, limit: seen.update(apply=apply, limit=limit) or 0,
    )

    assert (
        drain.main(["--nightly", "--apply", "--limit", "9", "--archive", str(tmp_path / "archive")])
        == 0
    )
    assert seen == {"apply": True, "limit": 9}
