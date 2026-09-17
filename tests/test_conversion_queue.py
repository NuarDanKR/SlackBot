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


def test_reconversion_refuses_an_original_changed_after_enqueue(tmp_path, monkeypatch):
    import hashlib

    import drain_conversion_queue as drain

    from tybot.archive import convert as convert_mod

    meta_path = _staged(tmp_path, body=b"new content")
    monkeypatch.setattr(convert_mod, "convert", lambda *_: pytest.fail("must not convert"))
    old_digest = hashlib.sha256(b"old content").hexdigest()

    ok, code, retryable = drain.reconvert(meta_path, expected_sha256=old_digest)

    assert not ok and code == "original_changed" and not retryable


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
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["error_code"] == "original_missing"
    assert saved["conversion_state"] == "failed"
    assert saved["reprocessed_at"]


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


def test_reconversion_is_not_complete_until_archived_and_indexed(tmp_path, monkeypatch):
    import drain_conversion_queue as drain

    from tybot import search_index
    from tybot.archive import writer
    from tybot.archive.store import ArchiveStore

    archive = tmp_path / "archive"
    when = datetime(2026, 9, 14, 9, 0, tzinfo=writer.KST)
    writer.ingest(
        archive,
        workspace="pilot",
        channel="#팀-전산_test",
        channel_id="C1",
        messages=[writer.IncomingMessage(
            ts=when,
            speaker="홍길동",
            text="[첨부:처리실패] 보고서.txt (txt, 1KB)",
        )],
        acl=["#팀-전산_test"],
    )
    meta_path = _staged(tmp_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({
        "slack_file_id": "F1",
        "declared_size": 1024,
        "origin_message_ts": str(when.timestamp()),
    })
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (meta_path.parent / "extracted.md").write_text(
        "<!-- 로컬 재변환본 -->\n# 보고서.txt\n\n표 1행\n",
        encoding="utf-8",
    )
    indexed = []
    monkeypatch.setattr(search_index, "reindex", lambda docs, root: indexed.extend(docs) or {})
    job = queue.Job(
        id=1,
        workspace="pilot",
        channel_id="C1",
        file_id="F1",
        original_sha256="",
        pipeline_version="1",
        state="leased",
        attempt_count=1,
    )

    assert drain.publish_reconversion(str(archive), meta_path, job) == (True, "", False)
    # 재시도되어도 원문 줄은 늘어나지 않는다.
    assert drain.publish_reconversion(str(archive), meta_path, job) == (True, "", False)

    lines = [line.text for doc in ArchiveStore(archive).docs() for line in doc.raw_lines]
    assert lines.count("[첨부추출:보고서.txt] 표 1행") == 1
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["archive_state"] == "archived"
    assert saved["index_state"] == "succeeded"
    assert saved["indexed_at"]
    assert indexed


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


# =============================================================================
# 사람이 요청한 재처리 (backfill)
# =============================================================================


def test_a_forced_enqueue_still_refuses_policy_exclusions(monkeypatch):
    """`force` 는 자동 판정을 넘는 것이지 **정책을 넘는 것이 아니다.**

    버튼 하나로 PII 검사가 우회되면 그 검사는 더 이상 검사가 아니다.
    """
    called = []
    monkeypatch.setattr(queue, "_connect", lambda: called.append(1))

    got = queue.enqueue(
        workspace="pilot", channel_id="C1", file_id="F1",
        error_code="pii_refused", retryable=True, force=True,
    )

    assert got is None
    assert called == [], "DB 를 열지도 않아야 한다"


def test_backfill_lists_before_it_writes(tmp_path, capsys):
    """`--apply` 없이는 아무것도 넣지 않는다. 수백 건이 한 번에 들어갈 수 있다."""
    import drain_conversion_queue as drain

    _staged_review(tmp_path, "F1", status="download_or_extract_failed")

    code = drain.backfill(str(tmp_path / "archive"), apply=False)

    assert code == 0
    out = capsys.readouterr().out
    assert "큐 대상 1건" in out
    assert "--apply" in out


def test_backfill_skips_policy_exclusions(tmp_path, monkeypatch, capsys):
    """정책 제외는 변환기와 무관하다. 변환기를 고쳐도 결과가 같다."""
    import drain_conversion_queue as drain

    _staged_review(tmp_path, "F1", status="pii_refused")
    _staged_review(tmp_path, "F2", status="download_or_extract_failed")
    added = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: added.append(kw["file_id"]) or 1)

    drain.backfill(str(tmp_path / "archive"), apply=True)

    assert added == ["F2"]
    assert "정책 제외 1건" in capsys.readouterr().out


def test_backfill_forces_because_the_old_verdict_used_the_old_converter(tmp_path, monkeypatch):
    """그때 「되풀이해도 소용없다」 고 본 근거는 **그때의 변환기**였다."""
    import drain_conversion_queue as drain

    _staged_review(tmp_path, "F1", status="download_or_extract_failed", error_code="unsupported")
    seen = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: seen.append(kw) or 1)

    drain.backfill(str(tmp_path / "archive"), apply=True)

    assert seen and seen[0]["force"] is True


def test_backfill_does_not_queue_an_attachment_without_its_original(
    tmp_path, monkeypatch, capsys
):
    import drain_conversion_queue as drain

    _staged_review(
        tmp_path, "F1", status="download_or_extract_failed", original=False
    )
    added = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: added.append(kw) or 1)

    drain.backfill(str(tmp_path / "archive"), apply=True)

    assert added == []
    assert "원본 없음 1건" in capsys.readouterr().out


def test_backfill_does_not_queue_an_attachment_with_an_unknown_channel(
    tmp_path, monkeypatch, capsys
):
    import drain_conversion_queue as drain

    _staged_review(
        tmp_path,
        "F1",
        status="download_or_extract_failed",
        channel_id="unknown",
    )
    added = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: added.append(kw) or 1)

    drain.backfill(str(tmp_path / "archive"), apply=True)

    assert added == []
    assert "채널 미확인 1건" in capsys.readouterr().out


def test_nightly_backfill_queues_only_retryable_failures(
    tmp_path, monkeypatch, capsys
):
    """무인 배치는 변환기 crash 같은 일시 실패만 다시 연다."""
    import drain_conversion_queue as drain

    _staged_review(
        tmp_path,
        "F1",
        status="download_or_extract_failed",
        error_code="converter_crashed",
        retryable=True,
    )
    _staged_review(
        tmp_path,
        "F2",
        status="unsupported",
        error_code="unsupported",
        retryable=False,
    )
    added = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: added.append(kw) or 1)

    code = drain.nightly_backfill(str(tmp_path / "archive"), apply=True)

    assert code == 0
    assert [item["file_id"] for item in added] == ["F1"]
    assert added[0]["force"] is False
    assert "영구 제외 1건" in capsys.readouterr().out


def test_nightly_backfill_is_a_dry_run_without_apply(tmp_path, monkeypatch, capsys):
    import drain_conversion_queue as drain

    _staged_review(
        tmp_path,
        "F1",
        status="download_or_extract_failed",
        error_code="converter_timeout",
        retryable=True,
    )
    monkeypatch.setattr(
        queue,
        "enqueue",
        lambda **_kw: pytest.fail("dry run must not enqueue"),
    )

    assert drain.nightly_backfill(str(tmp_path / "archive"), apply=False) == 0
    assert "--nightly --apply" in capsys.readouterr().out


def test_nightly_unit_requeues_and_can_publish_to_the_archive():
    root = Path(__file__).resolve().parent.parent
    service = (root / "deploy" / "tybot-convert-nightly.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "deploy" / "tybot-convert-nightly.timer").read_text(
        encoding="utf-8"
    )

    assert "--nightly --apply" in service
    assert "ReadWritePaths=/var/lib/tybot/staging /var/lib/tybot/archive" in service
    assert "OnCalendar=*-*-* 03:20:00" in timer
    assert "Persistent=true" in timer


def _staged_review(
    tmp_path,
    file_id,
    *,
    status,
    error_code="",
    channel_id="C1",
    original=True,
    retryable=False,
) -> None:
    """`attachment_review.scan()` 이 읽는 모양으로 하나 만든다."""
    d = (
        tmp_path / "staging" / "workspaces" / "pilot" / "channels" / channel_id
        / "attachments" / file_id
    )
    d.mkdir(parents=True, exist_ok=True)
    object_path = d / "original.pdf"
    if original:
        object_path.write_bytes(b"pdf")
    (d / "metadata.json").write_text(
        json.dumps({
            "name": f"{file_id}.pdf", "filetype": "pdf", "status": status,
            "error_code": error_code, "retryable": retryable, "sha256": "abc123",
            "object_path": str(object_path),
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# =============================================================================
# 회로 차단기가 실제로 막는다
# =============================================================================


class _FakeCursor:
    """실행한 SQL 을 기록하고 준비된 행을 돌려주는 최소 커서."""

    def __init__(self, script: list):
        self._script = script
        self.executed: list[tuple[str, tuple]] = []
        self._rows: list[dict] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        self._rows = self._script.pop(0) if self._script else []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, script):
        self.cursors: list[_FakeCursor] = []
        self._script = script
        self.committed = False

    def cursor(self):
        cur = _FakeCursor(self._script)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_db(monkeypatch, script):
    """`_connect()` 가 돌려줄 연결을 갈아 끼운다. 호출마다 같은 대본을 이어 쓴다."""
    conns: list[_FakeConn] = []
    shared = list(script)

    def connect():
        conn = _FakeConn(shared)
        conns.append(conn)
        return conn

    monkeypatch.setattr(queue, "_connect", connect)
    return conns


def test_an_open_breaker_stops_the_worker_from_claiming_its_jobs(monkeypatch):
    """**기록만 하고 안 읽으면 아무 일도 하지 않는다.**

    환경이 고장났는데 계속 집으면 시도 횟수만 갉아먹고, 정작 고친 뒤에는 남은
    횟수가 없어 그 파일이 영영 `failed` 로 닫힌다.
    """
    opened = datetime.now(UTC) - timedelta(seconds=30)
    job_row = {
        "id": 7, "workspace": "pilot", "channel_id": "C1", "file_id": "F1",
        "original_sha256": "abc", "pipeline_version": "1", "state": "leased",
        "attempt_count": 1, "error_code": "converter_crashed",
        "converter": "hwp5txt", "converter_version": "1.2", "next_attempt_at": None,
    }
    conns = _fake_db(monkeypatch, [
        # 1) open_breakers() 의 SELECT
        [{"converter": "hwp5txt", "converter_version": "1.2",
          "state": "open", "opened_at": opened}],
        # 2) claim 의 UPDATE ... RETURNING
        [job_row],
        # 3) 되돌리는 UPDATE
        [],
    ])

    jobs = queue.claim("host:1")

    assert jobs == [], "회로가 열린 변환기의 작업을 돌려주면 안 된다"
    statements = [sql for cur in conns[-1].cursors for sql, _ in cur.executed]
    # 되돌리는 UPDATE 는 임대를 풀고 시도 횟수를 되돌린다. 잡는 질의(SELECT ...
    # state = 'queued')와 구별해야 하므로 **되돌림의 표식**으로 고른다.
    returned = [s for s in statements if "attempt_count - 1" in s]
    assert returned, (
        "잡은 것을 되돌리지 않았다 — 임대만 잡고 있으면 그 시간만큼 다른 "
        "프로세스도 못 집고, 돌리지도 않은 시도가 상한을 갉아먹는다"
    )
    assert "lease_owner = ''" in returned[0]


def test_a_breaker_past_its_probe_window_lets_the_job_through(monkeypatch):
    """사람이 고쳤을 수 있다. 한 건은 흘려 보내 확인한다."""
    opened = datetime.now(UTC) - timedelta(seconds=queue.BREAKER_PROBE_SECONDS + 60)
    job_row = {
        "id": 7, "workspace": "pilot", "channel_id": "C1", "file_id": "F1",
        "original_sha256": "abc", "pipeline_version": "1", "state": "leased",
        "attempt_count": 1, "error_code": "", "converter": "hwp5txt",
        "converter_version": "1.2", "next_attempt_at": None,
    }
    _fake_db(monkeypatch, [
        [{"converter": "hwp5txt", "converter_version": "1.2",
          "state": "open", "opened_at": opened}],
        [job_row],
    ])

    jobs = queue.claim("host:1")

    assert [j.id for j in jobs] == [7]


def test_claiming_still_works_when_the_breaker_table_cannot_be_read(monkeypatch):
    """회로를 못 읽는 것이 재처리를 통째로 멈추는 이유가 되면 안 된다."""
    job_row = {
        "id": 9, "workspace": "pilot", "channel_id": "C1", "file_id": "F2",
        "original_sha256": "", "pipeline_version": "1", "state": "leased",
        "attempt_count": 1, "error_code": "", "converter": "", "converter_version": "",
        "next_attempt_at": None,
    }
    calls = {"n": 0}
    shared = [[job_row]]

    def connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise queue.QueueUnavailable("회로 표를 못 읽음")
        return _FakeConn(shared)

    monkeypatch.setattr(queue, "_connect", connect)

    assert [j.id for j in queue.claim("host:1")] == [9]
