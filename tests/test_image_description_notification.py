from __future__ import annotations

import json

from tybot.archive.image_description_notification import (
    ImageDescriptionNotice,
    drain,
    enqueue,
    outbox_path,
)


def _notice(job: str = "a" * 32) -> ImageDescriptionNotice:
    return ImageDescriptionNotice(
        job_id=job,
        workspace="tyit",
        channel_id="C12345678",
        message_ts="1700000000.123456",
    )


def test_outbox_keeps_only_safe_fixed_completion_metadata(tmp_path):
    assert enqueue(_notice(), root=tmp_path)
    payload = json.loads(outbox_path("tyit", root=tmp_path).read_text(encoding="utf-8"))

    assert payload == {
        "channel_id": "C12345678",
        "job_id": "a" * 32,
        "message_ts": "1700000000.123456",
        "state": "completed",
        "workspace": "tyit",
    }
    assert "OCR" not in json.dumps(payload)
    assert "description" not in json.dumps(payload)


def test_delivery_is_threaded_and_idempotent_by_the_job_id(tmp_path):
    notice = _notice()
    assert enqueue(notice, root=tmp_path)
    assert enqueue(notice, root=tmp_path)
    calls = []

    result = drain("tyit", lambda item: calls.append(item) or True, root=tmp_path)

    assert result == {"sent": 1, "left": 0, "invalid": 0}
    assert calls == [notice]
    assert calls[0].text == "✅ 이미지 설명·텍스트 인식 완료 — 검색 보조 정보로 반영되었습니다."
    assert calls[0].client_msg_id
    assert not outbox_path("tyit", root=tmp_path).exists()


def test_failed_delivery_stays_queued_without_losing_the_notice(tmp_path):
    assert enqueue(_notice(), root=tmp_path)

    result = drain("tyit", lambda _item: False, root=tmp_path)

    assert result == {"sent": 0, "left": 1, "invalid": 0}
    assert outbox_path("tyit", root=tmp_path).is_file()


def test_malformed_records_are_never_delivered(tmp_path):
    path = outbox_path("tyit", root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")

    result = drain("tyit", lambda _item: True, root=tmp_path)

    assert result == {"sent": 0, "left": 0, "invalid": 1}


# --- 2026-11-20 오너 결정: 인식 결과를 원 스레드에 보인다 ------------------

from tybot.archive.image_description_notification import (  # noqa: E402
    MAX_TRANSCRIPT_LINES,
    load_recognition,
    recognition_fields,
)


def _with(**fields) -> ImageDescriptionNotice:
    return ImageDescriptionNotice(
        job_id="b" * 32,
        workspace="tyit",
        channel_id="C12345678",
        message_ts="1700000000.123456",
        **fields,
    )


def test_recognition_is_shown_in_the_thread_reply():
    notice = _with(**recognition_fields(
        description="철근 배근 상태를 찍은 현장 사진",
        tags=("철근", "배근", "철근"),
        transcript="안전제일\n출입금지",
    ))
    text = notice.text
    assert "이미지를 이렇게 인식했습니다" in text
    assert "철근 배근 상태를 찍은 현장 사진" in text
    assert "*태그* 철근, 배근" in text
    assert "안전제일" in text and "출입금지" in text
    assert "바로잡아" in text


def test_recognition_survives_the_outbox_round_trip(tmp_path):
    notice = _with(**recognition_fields(description="거푸집 설치", transcript="A동"))
    assert enqueue(notice, root=tmp_path)
    calls = []
    drain("tyit", lambda item: calls.append(item) or True, root=tmp_path)
    assert calls == [notice]


def test_sensitive_values_are_masked_before_slack():
    fields = recognition_fields(
        description="연락처 010-1234-5678 이 적힌 안내판",
        transcript="담당 010-9876-5432\n주민 900101-1234567\n출입금지",
    )
    text = _with(**fields).text
    for raw in ("010-1234-5678", "010-9876-5432", "900101-1234567"):
        assert raw not in text
    assert "출입금지" in text


def test_slack_control_sequences_from_the_model_are_escaped():
    text = _with(**recognition_fields(
        description="<!channel> 공지 <https://evil.test|여기>", transcript="<@U123>"
    )).text
    assert "<!channel>" not in text and "<@U123>" not in text and "<https://" not in text
    assert "&lt;!channel&gt;" in text


def test_long_transcript_is_capped_with_a_count():
    body = "\n".join(f"{i}번째 줄" for i in range(MAX_TRANSCRIPT_LINES + 5))
    fields = recognition_fields(transcript=body)
    assert len(fields["transcript"]) == MAX_TRANSCRIPT_LINES
    assert fields["transcript_more"] == 5
    assert "외 5줄" in _with(**fields).text


def test_uncertain_recognition_says_so():
    text = _with(**recognition_fields(description="흐린 사진", uncertain=True)).text
    assert text.startswith("🟡") and "일부 불확실" in text


def test_tampered_outbox_with_unknown_field_is_not_delivered(tmp_path):
    path = outbox_path("tyit", root=tmp_path)
    path.parent.mkdir(parents=True)
    row = _notice().to_json() | {"raw_model_output": "x"}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert drain("tyit", lambda _item: True, root=tmp_path)["invalid"] == 1


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self):
        return self.cur


def test_load_recognition_reads_only_published_revisions():
    conn = _Conn([
        {"status": "described", "description": "콘크리트 타설", "tags": ["타설"]},
        {"status": "uncertain", "body": "B동 3층"},
    ])
    fields = load_recognition(conn, "c" * 32)
    assert fields["description"] == "콘크리트 타설"
    assert fields["tags"] == ("타설",)
    assert fields["transcript"] == ("B동 3층",)
    assert fields["uncertain"] is True
    tables = " ".join(sql for sql, _ in conn.cur.sql)
    assert "archiver_image_description_revision" in tables
    assert "archiver_image_transcript_revision" in tables


def test_load_recognition_without_a_revision_keeps_the_fixed_text():
    assert load_recognition(_Conn([None, None]), "d" * 32) == {}
    assert load_recognition(_Conn([]), "not-a-revision") == {}
    assert _notice().text.startswith("✅ 이미지 설명·텍스트 인식 완료")


def test_an_unreadable_outbox_is_logged_not_silently_skipped(tmp_path, monkeypatch, caplog):
    """B-99: 2026-11-20 운영 유닛이 이 디렉터리를 읽기 전용으로 막았는데 로그가 없었다."""
    import logging

    from tybot.archive import image_description_notification as mod

    assert enqueue(_notice(), root=tmp_path)

    def refuse(self, *, timeout=0.0, poll=0.05):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod.FileLock, "acquire", refuse)
    with caplog.at_level(logging.WARNING):
        result = drain("tyit", lambda _item: True, root=tmp_path)

    assert result["sent"] == 0 and result["error"] == 1
    assert "image notice outbox unreadable" in caplog.text
    assert outbox_path("tyit", root=tmp_path).is_file()
