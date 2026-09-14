"""첨부 변환 재처리 큐 — 정책·배선·재변환 (B-45 §5).

설계: `docs/design/operational-warning-recovery-and-answer-progress.md`

이 파일이 지키는 것 둘.

1. **되풀이하면 안 되는 것을 되풀이하지 않는다.** 정책 제외(`pii_refused`)와
   환경 문제(`converter_missing`)는 자동 재시도 대상이 아니다.
2. **재처리가 자료를 줄이지 않는다.** 재변환이 실패해도 원본과 이전 산출물은
   그대로 남는다.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tybot import conversion_queue as queue

SCHEMA = Path(__file__).resolve().parent.parent / "deploy" / "sql" / "conversion_queue_schema.sql"


# =============================================================================
# 정책 (DB 없이)
# =============================================================================


@pytest.mark.parametrize("code", sorted(queue.TERMINAL_CODES))
def test_a_terminal_code_is_never_retried_even_if_the_caller_says_so(code):
    """호출부가 실수로 `retryable=True` 를 넘겨도 막는다.

    한 곳에서 실수하면 그 파일이 네 번씩 같은 실패를 반복한다.
    """
    assert queue.is_retryable(code, True) is False
    assert queue.next_state(code, True, 0) == queue.FAILED


@pytest.mark.parametrize("code", sorted(queue.HOLD_CODES))
def test_an_environment_failure_is_held_not_failed(code):
    """`held` 와 `failed` 는 **사람이 할 일이 다르다.**

    하나는 서버를 고치는 것이고 하나는 그 파일을 포기한 것이다. 같은 값으로
    두면 "변환 실패 300건" 이 뜨고 정작 고쳐야 할 한 줄이 묻힌다.
    """
    assert queue.next_state(code, True, 0) == queue.HELD
    assert queue.is_retryable(code, True) is False


def test_pii_refusal_is_policy_not_a_technical_failure():
    """사람이 막은 것을 기계가 자동으로 푸는 길을 만들지 않는다."""
    assert queue.is_retryable("pii_refused", True) is False


def test_a_transient_failure_is_retried_until_the_cap():
    assert queue.next_state("converter_timeout", True, 0) == queue.QUEUED
    assert queue.next_state("converter_timeout", True, 3) == queue.QUEUED
    # 최초 시도를 포함해 4회. 넘으면 사람에게 넘긴다.
    assert queue.next_state("converter_timeout", True, queue.MAX_ATTEMPTS) == queue.FAILED


def test_backoff_grows_and_never_lands_on_the_same_second():
    """한 번에 실패한 파일 수백 개가 같은 초에 몰리면 그게 두 번째 장애다."""
    assert queue.backoff_seconds(1, jitter=0) == 60
    assert queue.backoff_seconds(2, jitter=0) == 300
    assert queue.backoff_seconds(3, jitter=0) == 1800
    # 상한을 넘겨도 마지막 값에 머문다.
    assert queue.backoff_seconds(99, jitter=0) == 1800
    # 지터가 실제로 섞인다.
    assert queue.backoff_seconds(1, jitter=0.25) > queue.backoff_seconds(1, jitter=0)


def test_a_zero_attempt_count_does_not_crash_the_backoff():
    """0은 「아직 안 해 봤다」 다. 인덱스가 음수가 되면 마지막 간격이 나온다."""
    assert queue.backoff_seconds(0, jitter=0) == 60


def test_an_open_breaker_lets_one_probe_through_after_a_while():
    """사람이 변환기를 고쳐도 큐가 스스로 회복하지 않으면 모두 멈춘 채 남는다."""
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    just_opened = now - timedelta(seconds=10)
    long_open = now - timedelta(seconds=queue.BREAKER_PROBE_SECONDS + 1)

    assert queue.breaker_allows(queue.BREAKER_CLOSED, None, now=now) is True
    assert queue.breaker_allows(queue.BREAKER_OPEN, just_opened, now=now) is False
    assert queue.breaker_allows(queue.BREAKER_OPEN, long_open, now=now) is True


def test_an_open_breaker_without_a_time_stays_closed_to_traffic():
    """언제 열렸는지 모르면 통과시키지 않는다 — 막는 쪽이 기본값."""
    assert queue.breaker_allows(queue.BREAKER_OPEN, None) is False


# =============================================================================
# 코드와 스키마가 갈리지 않는다
# =============================================================================


def test_the_states_the_code_knows_are_the_states_the_database_allows():
    """코드가 쓰는 상태를 DB 가 거부하면 **운영에서만** 터진다.

    전문 봇 `execution_mode` 에서 실제로 그랬다 — 코드는 `tools` 를 쓰는데
    CHECK 는 `('prompt','http')` 였고, 그 사실은 UPDATE 를 칠 때 드러났다.
    """
    sql = SCHEMA.read_text(encoding="utf-8")
    for state in queue.STATES:
        assert f"'{state}'" in sql, f"스키마가 {state} 를 모른다"
    for breaker_state in (queue.BREAKER_CLOSED, queue.BREAKER_OPEN):
        assert f"'{breaker_state}'" in sql


def test_the_schema_forbids_a_lease_without_an_expiry():
    """임대 만료가 없는 행은 **영원히** 회수되지 않는다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "conversion_job_lease_has_expiry" in sql
    assert "lease_expires_at IS NOT NULL" in sql


def test_the_queue_key_includes_the_content_hash_and_pipeline_version():
    """같은 이름의 **다른 내용**은 다른 작업이다. 파이프라인을 고치면 다시 시도한다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "conversion_job_identity" in sql
    assert "original_sha256" in sql
    assert "pipeline_version" in sql


def test_the_queue_never_stores_business_content():
    """큐가 유출되어도 업무 내용이 따라 나가면 안 된다."""
    sql = SCHEMA.read_text(encoding="utf-8").lower()
    for forbidden in ("payload", "content", "extracted", "token", "body"):
        assert f"{forbidden} text" not in sql, f"{forbidden} 열이 생겼다"


# =============================================================================
# 배선 — 수집을 막지 않는다
# =============================================================================


def test_a_queue_outage_does_not_stop_attachment_collection(monkeypatch, caplog):
    """재처리는 나중에 할 수 있지만 놓친 원본은 되돌릴 수 없다.

    Slack 백필은 분당 1요청 제한이라 그 기간 원문은 사실상 복구가 안 된다.
    """
    from tybot.archive import files

    def boom(**kw):
        raise queue.QueueUnavailable("DB 죽음")

    monkeypatch.setattr(queue, "enqueue", boom)
    storage = files.AttachmentStorage(
        staging_dir=Path("s"), objects_dir=Path("o"),
        workspace="pilot", channel_id="C1",
    )

    with caplog.at_level("WARNING"):
        files.queue_retry(
            storage, file_id="F1", original_sha256="abc",
            error_code="converter_timeout", retryable=True,
        )

    assert "재처리 큐에 올리지 못했다" in caplog.text


def test_a_non_retryable_failure_is_not_queued(monkeypatch):
    from tybot.archive import files

    calls = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: calls.append(kw))
    storage = files.AttachmentStorage(
        staging_dir=Path("s"), objects_dir=Path("o"),
        workspace="pilot", channel_id="C1",
    )

    files.queue_retry(
        storage, file_id="F1", original_sha256="abc",
        error_code="pii_refused", retryable=False,
    )

    assert calls == []


def test_a_job_without_coordinates_is_not_queued(monkeypatch):
    """좌표를 모르면 워커가 열 파일을 못 찾는다. 네 번 실패한 뒤 사람에게 간다."""
    from tybot.archive import files

    calls = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: calls.append(kw))
    storage = files.AttachmentStorage(staging_dir=Path("s"), objects_dir=Path("o"))

    files.queue_retry(
        storage, file_id="F1", original_sha256="abc",
        error_code="converter_timeout", retryable=True,
    )

    assert calls == []


def test_attachment_storage_carries_the_original_coordinates(tmp_path):
    """경로에서 되짚으면 `_safe_component()` 를 거친 값이라 원래와 다를 수 있다."""
    from tybot.archive.files import attachment_storage

    storage = attachment_storage(tmp_path / "archive", "pilot", "C0GJ")

    assert storage.workspace == "pilot"
    assert storage.channel_id == "C0GJ"


# =============================================================================
# 재변환 — 자료를 줄이지 않는다
# =============================================================================


def _staged(tmp_path, *, status="download_or_extract_failed", body=b"hello world") -> Path:
    d = tmp_path / "staging" / "workspaces" / "pilot" / "channels" / "C1" / "attachments" / "F1"
    d.mkdir(parents=True, exist_ok=True)
    obj = d / "original.txt"
    obj.write_bytes(body)
    (d / "metadata.json").write_text(
        json.dumps({
            "name": "보고서.txt", "filetype": "txt", "status": status,
            "object_path": str(obj), "error": "이전 실패", "extracted": False,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return d / "metadata.json"


def test_a_failed_reconversion_keeps_the_previous_output(tmp_path, monkeypatch):
    """먼저 지우면 실패했을 때 있던 것까지 사라진다."""
    import drain_conversion_queue as drain

    from tybot.archive import convert as convert_mod

    meta_path = _staged(tmp_path)
    preview = meta_path.parent / "extracted.md"
    preview.write_text("이전에 성공한 변환본", encoding="utf-8")

    def boom(filetype, raw):
        raise convert_mod.ConvertError("변환기 죽음")

    monkeypatch.setattr(convert_mod, "convert", boom)
    ok, code, _retryable = drain.reconvert(meta_path)

    assert ok is False
    assert code
    assert preview.read_text(encoding="utf-8") == "이전에 성공한 변환본"
    assert Path(json.loads(meta_path.read_text(encoding="utf-8"))["object_path"]).is_file()


def test_a_pii_blocked_attachment_is_not_unblocked_by_reprocessing(tmp_path):
    """정책 제외를 기술 실패처럼 자동 해제하지 않는다."""
    import drain_conversion_queue as drain

    meta_path = _staged(tmp_path, status="pii_refused")

    ok, code, retryable = drain.reconvert(meta_path)

    assert (ok, code, retryable) == (False, "pii_refused", False)
    assert json.loads(meta_path.read_text(encoding="utf-8"))["status"] == "pii_refused"


def test_a_missing_original_is_not_retried_forever(tmp_path):
    import drain_conversion_queue as drain

    meta_path = _staged(tmp_path)
    Path(json.loads(meta_path.read_text(encoding="utf-8"))["object_path"]).unlink()

    ok, code, retryable = drain.reconvert(meta_path)

    assert (ok, code, retryable) == (False, "original_missing", False)


def test_a_successful_reconversion_replaces_the_output(tmp_path, monkeypatch):
    import drain_conversion_queue as drain

    from tybot.archive import convert as convert_mod

    meta_path = _staged(tmp_path)
    monkeypatch.setattr(convert_mod, "convert", lambda ft, raw: ["표 1행", "표 2행"])

    ok, code, _retryable = drain.reconvert(meta_path)

    assert (ok, code) == (True, "")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "converted"
    assert meta["conversion_state"] == "succeeded"
    assert meta["extracted"] is True
    assert "표 1행" in (meta_path.parent / "extracted.md").read_text(encoding="utf-8")


def test_reprocessing_a_pii_line_blocks_the_whole_file(tmp_path, monkeypatch):
    """한 줄만 거부하고 나머지를 넣으면 금지 문서가 부분 수집된다."""
    import drain_conversion_queue as drain

    from tybot.archive import convert as convert_mod

    meta_path = _staged(tmp_path)
    monkeypatch.setattr(
        convert_mod, "convert",
        lambda ft, raw: ["정상 줄", "주민등록번호 900101-1234567"],
    )

    ok, code, _retryable = drain.reconvert(meta_path)

    assert (ok, code) == (False, "pii_refused")
    assert not (meta_path.parent / "extracted.md").exists()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["status"] == "pii_refused"


def test_an_empty_conversion_is_not_a_success(tmp_path, monkeypatch):
    """산출물 0줄은 성공이 아니다(설계 §5)."""
    import drain_conversion_queue as drain

    from tybot.archive import convert as convert_mod

    meta_path = _staged(tmp_path)
    monkeypatch.setattr(convert_mod, "convert", lambda ft, raw: ["", "   "])

    ok, code, _retryable = drain.reconvert(meta_path)

    assert (ok, code) == (False, "empty_output")


# =============================================================================
# 경로
# =============================================================================


def test_a_job_coordinate_cannot_escape_the_staging_root(tmp_path):
    """큐는 DB 다. DB 에서 읽은 값을 그대로 경로에 붙이면 그 자리가 경로 탈출이다."""
    import drain_conversion_queue as drain

    job = queue.Job(
        id=1, workspace="../../etc", channel_id="..", file_id="../passwd",
        original_sha256="", pipeline_version="1", state="queued", attempt_count=0,
    )

    path = drain.staging_meta(str(tmp_path / "archive"), job)

    root = (tmp_path / "staging" / "workspaces").resolve()
    assert root in path.resolve().parents
    assert ".." not in path.parts


def test_the_lease_owner_must_be_named():
    """회수할 때 누구 것이었는지 모르면 원인을 되짚을 수 없다."""
    with pytest.raises(ValueError):
        queue.claim("   ")
