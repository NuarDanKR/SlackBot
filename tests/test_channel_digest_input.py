"""채널×일 다이제스트 입력 수집과 지문 (B-93 2단계 §7-1)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from tybot import channel_digest as cd
from tybot.archive import attachment_doc, writer

WS, CH, CHANNEL = "mgmt", "C0PLAN0001", "#팀-기획_abb110-경영계획수립"


def _ingest(root: Path, when: datetime, text: str) -> None:
    writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CH, acl=[CHANNEL],
        messages=[writer.IncomingMessage(when, "담당자", text)],
    )


def _attach(root: Path, *, file_id: str, text: str, message_ts: str, revision="rev1") -> Path:
    doc = attachment_doc.AttachmentDoc(
        workspace=WS, channel_id=CH, channel=CHANNEL, file_id=file_id,
        name=f"{file_id}.pdf", revision=revision, visibility="private",
        acl=frozenset({CHANNEL}), text=text, conversion_state=attachment_doc.CONVERTED,
        message_ts=message_ts, filetype="pdf", sha256="a" * 64,
        staged_at="2026-11-01T00:00:00+00:00",
    )
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")
    return path


def _days(root: Path):
    return cd.collect_channel_days(root, WS, CH)


# 2026-11-10 23:30 KST = 2026-11-10 14:30 UTC
TS_1110_KST = str(datetime(2026, 11, 10, 14, 30, tzinfo=UTC).timestamp())


def test_message_ts_is_bucketed_by_korean_day():
    # UTC 로는 11-10 15:30 이지만 KST 로는 11-11 00:30 — 하루 경계는 KST 다.
    late = str(datetime(2026, 11, 10, 15, 30, tzinfo=UTC).timestamp())
    assert cd.day_of_message_ts(late) == date(2026, 11, 11)
    assert cd.day_of_message_ts(TS_1110_KST) == date(2026, 11, 10)
    assert cd.day_of_message_ts("") is None and cd.day_of_message_ts("x") is None


def test_collects_lines_and_same_day_attachments(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "사업계획 자료 공유드립니다 부채비율")
    _attach(tmp_path, file_id="F0PLAN0001", text="2026년 경영목표 영업이익 325억", message_ts=TS_1110_KST)
    days = _days(tmp_path)
    assert [d.day for d in days] == [date(2026, 11, 10)]
    (day,) = days
    assert day.workspace == WS and day.channel_id == CH
    assert any("부채비율" in line for line in day.lines)
    assert day.attachments[0][1].startswith("2026년 경영목표")
    assert day.source_files[0].path.endswith("/raw/2026-11-10.md")
    assert day.source_attachments[0].file_id == "F0PLAN0001"


def test_attachment_without_message_ts_is_not_guessed_into_a_day(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "공유")
    _attach(tmp_path, file_id="F0NOTS0001", text="본문", message_ts="")
    (day,) = _days(tmp_path)
    assert day.source_attachments == ()


def test_only_the_newest_revision_of_an_attachment_is_used(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "공유")
    _attach(tmp_path, file_id="F0PLAN0001", text="옛 본문", message_ts=TS_1110_KST, revision="rev1")
    new = attachment_doc.AttachmentDoc(
        workspace=WS, channel_id=CH, channel=CHANNEL, file_id="F0PLAN0001",
        name="F0PLAN0001.pdf", revision="rev2", visibility="private",
        acl=frozenset({CHANNEL}), text="새 본문", conversion_state=attachment_doc.CONVERTED,
        message_ts=TS_1110_KST, filetype="pdf", sha256="b" * 64,
        staged_at="2026-11-02T00:00:00+00:00", converted_at="2026-11-02T00:00:00+00:00",
    )
    path = tmp_path / new.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(new), encoding="utf-8")
    (day,) = _days(tmp_path)
    assert [a.revision for a in day.source_attachments] == ["rev2"]
    assert day.attachments[0][1] == "새 본문"


def test_revision_is_stable_and_changes_with_the_original(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "첫 줄")
    (day,) = _days(tmp_path)
    first = cd.revision(day, model="m")
    (again,) = _days(tmp_path)
    assert cd.revision(again, model="m") == first
    assert cd.revision(day, model="other") != first
    assert cd.revision(day, model="m", prompt_revision="x") != first
    _ingest(tmp_path, datetime(2026, 11, 10, 2, 0, tzinfo=UTC), "둘째 줄")
    (changed,) = _days(tmp_path)
    assert cd.revision(changed, model="m") != first
    assert cd.digest_id(changed, "r") == cd.digest_id(changed, "r")


def test_dm_files_are_never_digested(tmp_path):
    dm = tmp_path / "workspaces" / WS / "dm" / CH / "raw"
    dm.mkdir(parents=True)
    (dm / "2026-11-10.md").write_text("x", encoding="utf-8")
    assert _days(tmp_path) == []


def test_a_misplaced_file_of_another_channel_is_not_mixed_in(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "공유")
    assert cd.collect_channel_days(tmp_path, WS, "C0OTHER001") == []
    assert cd.collect_channel_days(tmp_path, "tyit", CH) == []


def test_the_archiver_layout_is_read_too(tmp_path):
    _ingest(tmp_path, datetime(2026, 11, 10, 1, 0, tzinfo=UTC), "v2 줄")
    (v2,) = list(tmp_path.glob(f"workspaces/{WS}/channels/{CH}__*/raw/2026-11-10.md"))
    legacy = tmp_path / WS / f"{CH}__name" / "archive" / "raw" / "2026-11-11.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(v2.read_bytes().replace("v2 줄".encode(), "archiver 줄".encode()))
    days = _days(tmp_path)
    assert [d.day for d in days] == [date(2026, 11, 10), date(2026, 11, 11)]
    assert any("archiver 줄" in line for line in days[1].lines)


def test_render_input_marks_truncation():
    day = cd.DayInput(
        workspace=WS, channel_id=CH, channel=CHANNEL, day=date(2026, 11, 10),
        lines=tuple(f"10:00 담당: 줄 {i}" for i in range(100)),
    )
    full = cd.render_input(day)
    assert not full.truncated and "줄 99" in full.text
    cut = cd.render_input(day, max_chars=200)
    assert cut.truncated and "앞 200자만" in cut.text
