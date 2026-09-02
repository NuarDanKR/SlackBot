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
    assert lines[0].startswith("[첨부:변환·원본검수대기]")
    assert "<https://example.slack.com/files/F123|원본 파일>" in lines[0]
    assert lines[1] == "[첨부본문:회의.txt] approved later"
    metadata = json.loads((storage.staging_dir / "F123" / "metadata.json").read_text("utf-8"))
    assert metadata["status"] == "pending_review"
    assert metadata["permalink"] == "https://example.slack.com/files/F123"
    assert (storage.objects_dir / "F123" / "회의.txt").read_bytes() == b"approved later"
    assert ArchiveStore(archive).docs() == []


def test_staged_attachment_rejects_all_extracted_lines_when_any_line_contains_pii(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    storage = attachment_storage(archive, "pilot", "C123")
    monkeypatch.setattr(
        "tybot.archive.files.download_bytes", lambda *_: "일반 내용\n계약자 명단".encode()
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
    assert warnings and "계약자 명단" in warnings[0]
    metadata = json.loads(
        (storage.staging_dir / "F-PII" / "metadata.json").read_text("utf-8")
    )
    assert metadata["status"] == "pii_refused"
