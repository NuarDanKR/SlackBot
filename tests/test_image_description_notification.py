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
