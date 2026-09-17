"""아카이브 v2 경로·호환·첨부 격리·마이그레이션 검증."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tybot.archive import writer
from tybot.archive.files import attachment_storage, stage_files
from tybot.archive.migrate import migrate_archive
from tybot.archive.store import ArchiveStore, SchemaError, validate


def _message(day: int, text: str) -> writer.IncomingMessage:
    return writer.IncomingMessage(datetime(2026, 8, day, 1, 0, tzinfo=UTC), "홍길동", text)


def _legacy_doc() -> str:
    return """---
workspace: pilot
channel: "#팀-전산_ABB110-회의"
visibility: private
acl: [#팀-전산_ABB110-회의]
share_with: []
doc_count: 2
last_ingested: 2026-08-13T10:00+09:00
---

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 10:00] 홍길동: 첫날 회의
> [2026-08-13 10:00] 홍길동: 둘째날 회의
"""


def test_v2_schema_requires_channel_identity_and_date():
    broken = _legacy_doc().replace("workspace: pilot", "schema_version: 2\nworkspace: pilot")
    with pytest.raises(SchemaError, match="v2 필수 필드"):
        validate(broken)


def test_writer_uses_channel_id_and_splits_days(tmp_path):
    result = writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-회의",
        channel_id="C123",
        messages=[_message(12, "첫날"), _message(13, "둘째날")],
        acl=["#팀-전산_ABB110-회의"],
    )
    assert [path.name for path in result.paths] == ["2026-08-12.md", "2026-08-13.md"]
    assert all("workspaces/pilot/channels/C123__" in path.as_posix() for path in result.paths)
    assert all("## 요약" not in path.read_text(encoding="utf-8") for path in result.paths)


def test_channel_rename_reuses_id_directory(tmp_path):
    first = writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-회의",
        channel_id="C123",
        messages=[_message(12, "첫날")],
        acl=["#팀-전산_ABB110-회의"],
    )
    second = writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-주간회의",
        channel_id="C123",
        messages=[_message(13, "둘째날")],
        acl=["#팀-전산_ABB110-주간회의"],
    )
    assert first.path.parents[1] == second.path.parents[1]
    doc = ArchiveStore(tmp_path).docs()[0]
    assert doc.channel == "#팀-전산_ABB110-주간회의"
    assert doc.acl == frozenset(
        {"#팀-전산_ABB110-회의", "#팀-전산_ABB110-주간회의"}
    )


def test_store_merges_v1_and_v2_without_duplicate_evidence(tmp_path):
    legacy = tmp_path / "channels" / "pilot" / "회의.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_legacy_doc(), encoding="utf-8")
    writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-회의",
        channel_id="C123",
        messages=[_message(12, "첫날 회의")],
        acl=["#팀-전산_ABB110-회의"],
    )

    docs = ArchiveStore(tmp_path).docs()
    assert len(docs) == 1
    assert len(docs[0].raw_lines) == 2
    assert docs[0].channel_id == "C123"


def test_migration_is_dry_run_by_default_and_idempotent(tmp_path):
    legacy = tmp_path / "channels" / "pilot" / "회의.md"
    legacy.parent.mkdir(parents=True)
    original = _legacy_doc()
    legacy.write_text(original, encoding="utf-8")
    mapping = {"pilot": {"#팀-전산_ABB110-회의": "C123"}}

    dry = migrate_archive(tmp_path, mapping)
    assert dry.dry_run and dry.migrated_messages == 2
    assert not (tmp_path / "workspaces").exists()

    first = migrate_archive(tmp_path, mapping, apply=True)
    second = migrate_archive(tmp_path, mapping, apply=True)
    assert first.migrated_messages == 2
    assert second.migrated_messages == 0
    assert legacy.read_text(encoding="utf-8") == original


def test_staged_attachment_is_outside_search_archive(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    storage = attachment_storage(archive, "pilot", "C123")
    monkeypatch.setattr("tybot.archive.files.download_bytes", lambda *_: b"approved later")
    raw_file = {
        "id": "F123",
        "name": "회의.txt",
        "filetype": "txt",
        "size": 14,
        "url_private_download": "https://example.invalid/file",
        "permalink": "https://example.slack.com/files/F123",
    }

    lines, warnings = stage_files([raw_file], "xoxb-test", storage)
    assert warnings == []
    assert lines[0].startswith("[첨부:자동변환]")
    assert "<https://example.slack.com/files/F123|원본 파일>" in lines[0]
    assert lines[1] == "[첨부본문:회의.txt] approved later"
    metadata = json.loads((storage.staging_dir / "F123" / "metadata.json").read_text("utf-8"))
    assert metadata["status"] == "converted"
    assert metadata["permalink"] == "https://example.slack.com/files/F123"
    assert (storage.objects_dir / "F123" / "회의.txt").read_bytes() == b"approved later"
    assert ArchiveStore(archive).docs() == []


def test_staged_attachment_rejects_all_extracted_lines_when_any_line_contains_pii(
    tmp_path, monkeypatch
):
    """직접 식별자 한 줄이면 **첨부 전체**를 막는다. 부분 수집은 하지 않는다."""
    archive = tmp_path / "archive"
    storage = attachment_storage(archive, "pilot", "C123")
    monkeypatch.setattr(
        "tybot.archive.files.download_bytes",
        lambda *_: "일반 내용\n계약자 900101-1234567".encode(),
    )
    raw_file = {
        "id": "F-PII",
        "name": "자료.txt",
        "filetype": "txt",
        "size": 30,
        "url_private_download": "https://example.invalid/file",
    }

    lines, warnings = stage_files([raw_file], "xoxb-test", storage)

    assert lines == ["[첨부:수집제외] 자료.txt (txt, 1KB)"]
    assert warnings and "주민등록번호" in warnings[0]
    metadata = json.loads(
        (storage.staging_dir / "F-PII" / "metadata.json").read_text("utf-8")
    )
    assert metadata["status"] == "pii_refused"
    assert metadata["screen_result"] == "blocked"
    assert metadata["screen_codes"] == ["resident-registration-number"]
    # 감사 metadata 에 번호가 복제되면 막은 의미가 없다(설계 §2.4).
    assert "900101" not in json.dumps(metadata, ensure_ascii=False)


def test_sensitive_term_alone_is_collected_with_a_notice(tmp_path, monkeypatch):
    """"계약자 명단 취합 예정" 은 **일정 문장**이다. 단어 하나로 막지 않는다.

    예전에는 이 파일이 통째로 `pii_refused` 였고, 봇은 그 일정에 대해
    "자료가 없다" 고 답했다(설계 §2.1).
    """
    archive = tmp_path / "archive"
    storage = attachment_storage(archive, "pilot", "C123")
    monkeypatch.setattr(
        "tybot.archive.files.download_bytes",
        lambda *_: "9월 공정 일정\n계약자 명단 취합 예정".encode(),
    )
    raw_file = {
        "id": "F-TERM",
        "name": "9월일정.txt",
        "filetype": "txt",
        "size": 30,
        "url_private_download": "https://example.invalid/file",
    }

    lines, _ = stage_files([raw_file], "xoxb-test", storage)

    assert any("계약자 명단 취합 예정" in line for line in lines)
    metadata = json.loads(
        (storage.staging_dir / "F-TERM" / "metadata.json").read_text("utf-8")
    )
    assert metadata["status"] == "converted"
    assert metadata["screen_result"] == "passed_with_notice"
    assert "sensitive-term-mentioned" in metadata["screen_codes"]


def test_image_is_ocr_converted_without_waiting_for_a_command(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    storage = attachment_storage(archive, "pilot", "C123")
    monkeypatch.setattr("tybot.archive.files.download_bytes", lambda *_: b"image")
    monkeypatch.setattr("tybot.archive.files.convert", lambda *_: ["OCR 본문"])
    raw_file = {
        "id": "F-IMAGE",
        "name": "현장사진.png",
        "filetype": "png",
        "size": 5,
        "url_private_download": "https://example.invalid/file",
    }

    lines, warnings = stage_files([raw_file], "xoxb-test", storage)

    assert warnings == []
    assert lines == [
        "[첨부:자동변환] 현장사진.png (png, 1KB)",
        "[첨부추출:현장사진.png] OCR 본문",
    ]
    metadata = json.loads(
        (storage.staging_dir / "F-IMAGE" / "metadata.json").read_text("utf-8")
    )
    assert metadata["status"] == "converted"
    assert metadata["extracted"] is True


# --- Slack 메시지 좌표 보존 (B-56) ---------------------------------------------
def test_the_slack_message_ts_is_kept_out_of_the_original_text(tmp_path):
    """좌표는 **시각 칸 안에** 적는다. 본문에 붙이면 그 글자가 원문이 된다(원칙 1)."""
    writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-회의",
        channel_id="C1",
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
            speaker="홍길동",
            text="공정률은 62.5%입니다",
            source_ts="1758012345.123456",
        )],
    )

    line = ArchiveStore(tmp_path).source_docs()[0].raw_lines[0]

    assert line.message_ts == "1758012345.123456"
    assert line.text == "공정률은 62.5%입니다"
    assert line.ts == "2026-09-16 10:00"


def test_lines_collected_before_the_coordinate_existed_still_parse(tmp_path):
    """옛 줄에는 좌표가 없다. 그 줄이 안 읽히면 과거 근거가 통째로 사라진다."""
    path = tmp_path / "channels" / "pilot" / "회의.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_legacy_doc(), encoding="utf-8")

    line = ArchiveStore(tmp_path).source_docs()[0].raw_lines[0]

    assert line.message_ts == ""
    assert line.text == "첫날 회의"


def test_a_message_archived_before_the_coordinate_is_not_written_twice(tmp_path):
    """좌표만 다른 같은 줄이 다시 쌓이면 사람이 같은 말을 두 번 한 것처럼 보인다."""
    kw = {
        "workspace": "pilot",
        "channel": "#팀-전산_ABB110-회의",
        "channel_id": "C1",
    }
    when = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)
    writer.ingest(tmp_path, messages=[
        writer.IncomingMessage(ts=when, speaker="홍길동", text="공정률은 62.5%입니다"),
    ], **kw)

    again = writer.ingest(tmp_path, messages=[
        writer.IncomingMessage(
            ts=when, speaker="홍길동", text="공정률은 62.5%입니다",
            source_ts="1758012345.123456",
        ),
    ], **kw)

    assert again.written == 0
    assert len(ArchiveStore(tmp_path).source_docs()[0].raw_lines) == 1


def test_a_made_up_coordinate_is_not_written(tmp_path):
    """permalink 로 나갈 값이다. 모양이 아니면 채널 링크로 내려가는 편이 낫다."""
    writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀-전산_ABB110-회의",
        channel_id="C1",
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
            speaker="홍길동",
            text="공정률은 62.5%입니다",
            source_ts="어제 그 메시지",
        )],
    )

    line = ArchiveStore(tmp_path).source_docs()[0].raw_lines[0]

    assert line.message_ts == ""
    assert line.ts == "2026-09-16 10:00"
