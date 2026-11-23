"""재처리 큐는 옛 배치(`<root>/workspaces/...`)에 쓰지 않는다 (B-99 1단계 C).

2026-11-20 장애: 재처리가 원문 한 줄을 `<root>/workspaces/<ws>/channels/...` 에
덧붙였고, 그 디렉터리가 생기자 Archiver 가 옛 배치가 남았다고 보고 기동을
거부했다. 이 파일이 지키는 것.

1. 채널별 아카이브가 있으면 원문 줄도 정본도 **그 자리에만** 쓴다.
2. 찾지 못하면 **아무것도 쓰지 않고** 고정 코드로 닫는다 — 옛 배치로 물러서지 않는다.
3. 어느 경우에도 `<root>/workspaces/` 를 만들지 않는다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tybot import conversion_queue as queue

CH = "C0TESTCH02"


def _job(channel_id: str = CH) -> queue.Job:
    return queue.Job(
        id=7,
        workspace="pilot",
        channel_id=channel_id,
        file_id="F1",
        original_sha256="",
        pipeline_version="1",
        state="leased",
        attempt_count=1,
    )


def _staged(tmp_path: Path, when: datetime) -> Path:
    d = tmp_path / "stage" / "F1"
    d.mkdir(parents=True)
    obj = d / "original.txt"
    obj.write_bytes(b"hello")
    meta = d / "metadata.json"
    meta.write_text(
        json.dumps(
            {
                "name": "보고서.txt",
                "filetype": "txt",
                "status": "converted",
                "object_path": str(obj),
                "slack_file_id": "F1",
                "declared_size": 1024,
                "origin_message_ts": str(when.timestamp()),
                "conversion_state": "succeeded",
                "sha256": "abcdef1234567890",
                "converter_name": "txt:primary",
                "converter_version": "1",
                "converter_config": {},
                "converted_at": "2026-09-14T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "extracted.md").write_text(
        "<!-- 재변환본 -->\n# 보고서.txt\n\n본문 한 줄\n", encoding="utf-8"
    )
    return meta


def test_an_unresolved_channel_writes_nothing_and_records_a_fixed_code(
    tmp_path, monkeypatch, capsys
):
    """채널 디렉터리가 없으면 옛 배치로 물러서지 않는다."""
    import drain_conversion_queue as drain

    from tybot import search_index
    from tybot.archive import writer

    archive = tmp_path / "archive"
    archive.mkdir()
    when = datetime(2026, 9, 14, 9, 0, tzinfo=writer.KST)
    meta = _staged(tmp_path, when)
    monkeypatch.setattr(search_index, "reindex", lambda docs, root: {})

    result = drain.publish_reconversion(str(archive), meta, _job())

    assert result == (False, drain.CHANNEL_ARCHIVE_UNRESOLVED, False)
    assert drain.CHANNEL_ARCHIVE_UNRESOLVED.isascii()
    assert not (archive / "workspaces").exists()
    assert list(archive.rglob("*.md")) == []
    # 로그에는 좌표만. 파일 이름·본문은 싣지 않는다.
    err = capsys.readouterr().err
    assert f"ch={CH}" in err and "file=F1" in err
    assert "보고서" not in err and "본문 한 줄" not in err
    # 되풀이해도 같은 결과다 — 큐가 다시 돌리지 않는다.
    assert queue.next_state(drain.CHANNEL_ARCHIVE_UNRESOLVED, False, 1) == queue.FAILED


def test_an_invalid_channel_id_is_unresolved_not_legacy(tmp_path):
    """`shadow_paths` 가 거부하는 채널 ID 도 옛 배치로 새지 않는다."""
    import drain_conversion_queue as drain

    archive = tmp_path / "archive"
    archive.mkdir()
    assert drain.channel_archive_for(str(archive), _job("C1")) is None
    assert not (archive / "workspaces").exists()


def test_two_directories_for_one_channel_are_unresolved(tmp_path):
    """한 채널 ID 에 디렉터리가 둘이면 고르지 않는다."""
    import drain_conversion_queue as drain

    archive = tmp_path / "archive"
    for name in ("a", "b"):
        (archive / "pilot" / f"{CH}__{name}" / "archive").mkdir(parents=True)
    assert drain.channel_archive_for(str(archive), _job()) is None


def test_the_drain_loop_records_the_code_without_retrying(tmp_path, monkeypatch):
    """큐 기록은 기존 의미를 따른다: 실패·재시도 불가."""
    import drain_conversion_queue as drain

    from tybot.archive import writer

    archive = tmp_path / "archive"
    archive.mkdir()
    meta = _staged(tmp_path, datetime(2026, 9, 14, 9, 0, tzinfo=writer.KST))
    failed = []
    monkeypatch.setattr(queue, "reclaim_expired", lambda: 0)
    monkeypatch.setattr(queue, "claim", lambda owner, limit: [_job()])
    monkeypatch.setattr(queue, "succeed", lambda job_id: None)
    monkeypatch.setattr(
        queue,
        "fail",
        lambda job_id, *, error_code, retryable: (
            failed.append((job_id, error_code, retryable)) or "failed"
        ),
    )
    monkeypatch.setattr(drain, "staging_meta", lambda root, job: meta)
    monkeypatch.setattr(drain, "reconvert", lambda path, expected_sha256="": (True, "", False))

    assert drain.drain(str(archive), limit=1) == drain.EXIT_FAILED
    assert failed == [(7, drain.CHANNEL_ARCHIVE_UNRESOLVED, False)]
    assert not (archive / "workspaces").exists()


def test_a_symlinked_channel_archive_is_unresolved(tmp_path):
    """링크로 다른 자리를 가리키는 `archive` 칸에는 쓰지 않는다."""
    import drain_conversion_queue as drain
    import pytest

    archive = tmp_path / "archive"
    channel = archive / "pilot" / f"{CH}__name"
    channel.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (channel / "archive").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    assert drain.channel_archive_for(str(archive), _job()) is None


def test_an_existing_channel_archive_resolves(tmp_path):
    import drain_conversion_queue as drain

    archive = tmp_path / "archive"
    target = archive / "pilot" / f"{CH}__팀-전산" / "archive"
    target.mkdir(parents=True)
    assert drain.channel_archive_for(str(archive), _job()) == target
