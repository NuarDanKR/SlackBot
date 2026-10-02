"""보존 기간이 **실제 자료에 적용되는가.**

결정: 2026-10-02 오너(B-70). 콘솔 설정·영구 보관은 B-71 이라 여기 없다.

2026-10-02 조사에서 드러난 모양 — `archive_retention_policy` 에 90일이 들어
있었지만 그 값을 읽어 지우는 코드가 없었다. 체크리스트는 통과하는데 아무것도
만료되지 않았고, 사람은 체크된 것을 보증으로 읽었다.

여기 시험이 지키는 것은 다섯이다.

1. 경로를 **쓰는 코드에서 얻는다** — 손으로 적은 경로는 조용히 어긋난다
2. 만료 기준은 **원래 좌표**다 — 파일 mtime 이 아니다
3. 좌표를 모르면 **안 지운다**
4. 아카이브 원문은 **재작성하지 않는다**
5. 설정값이 아니라 **실행**이 집행의 근거다

## 왜 진짜 `writer` 로 쓰는가

처음에는 DM 원문을 손으로 깔았다(`archive/<날짜>.md`). 실제 경로는
`archive/raw/<날짜>.md` 다 — `writer._ingest_locked` 이 `raw/` 를 하나 더 만든다.
그래서 글롭이 **아무것도 못 찾았고**, 못 찾았으니 「만료 대상 0건」 이 나왔다.
오류는 없다. 집행했는데 아무것도 안 지워진다. 게다가 첨부 좌표는 원문 줄에서
나오므로 모든 첨부가 함께 멈춘다.

그래서 이 파일은 경로를 쓰지 않는다. `shadow_paths` 와 `writer` 에게 **쓰게
하고**, 그 결과를 본다.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tybot import retention
from tybot.archive import shadow_paths, writer
from tybot.archive.archiving_state import production_blockers

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
WS = "tyit"
DM_CHANNEL = "D12345678"
DAYS = {
    retention.POLICY_BOT_AUDIT: 90,
    retention.POLICY_DM_MESSAGE: 90,
    retention.POLICY_DM_ATTACHMENT: 90,
}


def _epoch(days_ago: float) -> str:
    return f"{(NOW - timedelta(days=days_ago)).timestamp():.6f}"


def _kst_iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).astimezone(retention.KST).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _dm_doc(root: Path, user: str, entries: list[tuple[str, str]]) -> Path:
    """**진짜 writer** 로 그 사람의 DM 원문을 쓴다. 경로는 writer 가 정한다."""
    root.mkdir(parents=True, exist_ok=True)
    directory = shadow_paths.dm_archive_dir(root, WS, user, DM_CHANNEL)
    messages = [
        writer.IncomingMessage(
            ts=datetime.fromtimestamp(float(message_ts), tz=UTC).astimezone(writer.KST),
            speaker="김태영",
            text=text,
            source_ts=message_ts,
        )
        for message_ts, text in entries
    ]
    result = writer.ingest(
        root,
        workspace=WS,
        channel=DM_CHANNEL,
        channel_id=DM_CHANNEL,
        messages=messages,
        acl=[user],
        dm_user=user,
        dm_directory=directory,
    )
    assert result.path.is_file(), "writer 가 원문을 쓰지 못했습니다"
    return result.path


def _object(root: Path, user: str, key: str) -> Path:
    """첨부 원본의 자리도 **경로 규칙에서** 얻는다(`dm_consumer` 와 같은 식)."""
    private = shadow_paths.dm_root(root, WS, user, DM_CHANNEL)
    path = private / "objects" / WS / "files" / f"{key}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"sealed")
    return path


def _plan(tmp_path: Path, *, days=None, **over) -> retention.Plan:
    return retention.plan(
        days=days if days is not None else DAYS,
        qa_dir=over.pop("qa_dir", tmp_path / "qa-log"),
        archive_root=over.pop("archive_root", tmp_path / "archive"),
        handoff_dir=over.pop("handoff_dir", None),
        now=NOW,
        **over,
    )



def _whole(tmp_path: Path, **over) -> retention.Plan:
    """**전부 본** 계획. 인계 경로와 reader 가 둘 다 있어야 complete 가 된다."""
    (tmp_path / "handoff").mkdir(exist_ok=True)
    return _plan(
        tmp_path,
        handoff_dir=tmp_path / "handoff",
        spool_reader=lambda *_a: _epoch(1),
        **over,
    )

def _write_unlocked(path: Path, question: str) -> None:
    """락을 못 잡은 writer 가 하는 일. **쓰는 자리는 `appending` 이 정한다.**"""
    from tybot import audit

    with audit.appending(path) as target, target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": _kst_iso(0), "question": question}, ensure_ascii=False)
            + "\n"
        )

# ---------------------------------------------------------------------------
# 0. 경로는 쓰는 코드에서 얻는다
# ---------------------------------------------------------------------------


def test_the_scanner_finds_what_the_writer_actually_wrote(tmp_path):
    """손으로 적은 글롭이 어긋나면 **0건이 나오고 오류는 없다.**

    `archive/*.md` 로 적었을 때 실제로 그랬다. 실제 경로는
    `archive/raw/<날짜>.md` 다.
    """
    archive = tmp_path / "archive"
    written = _dm_doc(archive, "U12345678", [(_epoch(700), "옛 이야기")])

    assert retention.dm_archive_files(archive) == [written]
    assert "raw" in written.parts, "writer 가 raw/ 를 쓴다는 전제가 깨졌습니다"


def test_the_glob_tracks_the_path_helper(tmp_path):
    """글롭과 경로 helper 가 갈리면 조용히 0건이 된다."""
    archive = tmp_path / "archive"
    directory = shadow_paths.dm_archive_dir(archive, WS, "U12345678", DM_CHANNEL)
    relative = directory.relative_to(archive)

    assert f"*/dm/*/{relative.name}/raw/*.md" == retention.DM_RAW_GLOB
    assert relative.parts[:2] == (WS, "dm")


# ---------------------------------------------------------------------------
# 1. 만료 기준은 **원래 좌표**다
# ---------------------------------------------------------------------------


def test_a_backfilled_file_expires_on_its_slack_coordinate(tmp_path):
    """소급 수집은 2년 전 대화를 **오늘** 파일로 쓴다.

    mtime 으로 세면 90일이 그날부터 새로 시작한다 — 2년 하고도 90일을 더
    보관하게 되고, 그건 아무도 정한 적 없는 기간이다.
    """
    archive = tmp_path / "archive"
    old = _dm_doc(archive, "U12345678", [(_epoch(700), "2년 전 이야기")])
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))

    assert [t.path for t in _plan(tmp_path).targets] == [old]


def test_a_fresh_coordinate_in_an_old_file_is_kept(tmp_path):
    """반대쪽도 본다 — 파일이 오래됐다고 내용이 만료된 것이 아니다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U12345678", [(_epoch(3), "어제 이야기")])
    stale = (NOW - timedelta(days=400)).timestamp()
    os.utime(path, (stale, stale))

    assert _plan(tmp_path).targets == []


# ---------------------------------------------------------------------------
# 2. 좌표를 모르면 안 지운다
# ---------------------------------------------------------------------------


def test_a_line_without_a_coordinate_keeps_the_whole_file(tmp_path):
    """좌표 없는 옛 줄은 **모르는 것**이지 오래된 것이 아니다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U12345678", [(_epoch(700), "좌표 있는 옛 줄")])
    # 좌표가 없던 시절의 줄을 그대로 끼워 넣는다(옛 자료가 실제로 이렇다).
    with path.open("a", encoding="utf-8") as handle:
        handle.write("> [2024-05-01 10:00] 김태영: 좌표 없는 옛 줄\n")

    ready = _plan(tmp_path)

    assert ready.targets == []
    assert [u.reason for u in ready.unresolved] == ["좌표 없는 원문 줄이 있습니다"]


def test_an_object_without_a_referring_line_is_not_removed(tmp_path):
    """가리키는 줄이 없는 원본은 **좌표를 모르는 것**이다."""
    archive = tmp_path / "archive"
    orphan = _object(archive, "U12345678", "a" * 64)

    ready = _plan(tmp_path)

    assert orphan not in [t.path for t in ready.targets]
    assert any("가리키는 원문 줄이 없습니다" in u.reason for u in ready.unresolved)


def test_a_spool_entry_whose_seal_cannot_be_read_is_kept(tmp_path):
    """봉인을 못 열면 좌표를 모른다. 큐 파일의 mtime 으로 세지 않는다."""
    spool = tmp_path / "handoff" / WS / "pending"
    spool.mkdir(parents=True)
    (spool / f"{'b' * 64}.bin").write_bytes(b"sealed")

    ready = _plan(
        tmp_path,
        handoff_dir=tmp_path / "handoff",
        spool_reader=lambda *_a: (_ for _ in ()).throw(ValueError),
    )

    assert ready.targets == []
    assert any("봉인 안의 좌표" in u.reason for u in ready.unresolved)


def test_a_spool_entry_expires_on_the_coordinate_inside_the_seal(tmp_path):
    spool = tmp_path / "handoff" / WS / "pending"
    spool.mkdir(parents=True)
    entry = spool / f"{'b' * 64}.bin"
    entry.write_bytes(b"sealed")

    ready = _plan(
        tmp_path, handoff_dir=tmp_path / "handoff", spool_reader=lambda *_a: _epoch(200)
    )

    assert [t.path for t in ready.targets] == [entry]


def test_a_spool_without_a_reader_is_reported_not_counted_as_clean(tmp_path):
    """열 수단이 없으면 **봤다고 하지 않는다.**

    0건으로 보고하면 인계 중인 사본이 영원히 남은 채로 「집행 완료」 가 된다.
    """
    spool = tmp_path / "handoff" / WS / "pending"
    spool.mkdir(parents=True)
    (spool / f"{'c' * 64}.bin").write_bytes(b"sealed")

    ready = _plan(tmp_path, handoff_dir=tmp_path / "handoff")

    assert ready.targets == []
    assert any("봉인을 열 수단이 없어" in note for note in ready.skipped)


def test_a_missing_handoff_directory_is_reported(tmp_path):
    ready = _plan(tmp_path)

    assert any("ARCHIVER_DM_HANDOFF_DIR" in note for note in ready.skipped)


def test_the_spool_reader_uses_the_same_vault_as_the_collector():
    """키가 두 벌이면 하나는 못 열고, 못 연 사본은 영원히 쌓인다."""
    import inspect

    source = inspect.getsource(retention.spool_reader)
    assert "DmInbox" in source
    assert "_fernet" in source
    # 좌표만 꺼낸다 — 지우려고 남의 DM 본문을 읽지 않는다.
    assert "message_ts" in source
    assert retention.spool_reader("") is None


# ---------------------------------------------------------------------------
# 3. 아카이브 원문은 재작성하지 않는다
# ---------------------------------------------------------------------------


def test_one_live_line_keeps_the_whole_dm_file(tmp_path):
    """한 줄이라도 안 지났으면 건너뛴다.

    줄을 골라 지우면 원문을 고치는 것이고(절대 원칙 1), 남은 줄의 `lineno` 가
    어긋나 기존 출처가 다른 문장을 가리킨다.
    """
    archive = tmp_path / "archive"
    _dm_doc(archive, "U12345678", [(_epoch(200), "만료된 줄")])
    _dm_doc(archive, "U12345678", [(_epoch(3), "아직 살아 있는 줄")])

    live = [t for t in _plan(tmp_path).targets if "아직" in t.path.read_text("utf-8")]
    assert live == []


def test_a_dm_file_is_only_ever_removed_whole(tmp_path):
    archive = tmp_path / "archive"
    # **같은 날**의 두 줄이라 writer 가 한 파일에 쌓는다.
    path = _dm_doc(archive, "U12345678", [
        (_epoch(700), "하나"), (_epoch(700.1), "둘"),
    ])

    ready = _plan(tmp_path)

    assert [(t.path, t.kind) for t in ready.targets] == [(path, "file")]
    retention.apply(ready)
    assert not path.exists()


def test_another_users_dm_is_untouched(tmp_path):
    """만료되지 않은 **다른 사람** 자료는 건드리지 않는다."""
    archive = tmp_path / "archive"
    mine = _dm_doc(archive, "U11111111", [(_epoch(700), "옛 이야기")])
    yours = _dm_doc(archive, "U22222222", [(_epoch(3), "어제 이야기")])

    retention.apply(_plan(tmp_path))

    assert not mine.exists()
    assert yours.exists()


# ---------------------------------------------------------------------------
# 감사 기록 — 빈 표가 아니라 **파일**이 보관처다
# ---------------------------------------------------------------------------


def test_expired_qa_lines_are_filtered_and_live_lines_stay(tmp_path):
    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(200), "question": "옛 질문"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"ts": _kst_iso(3), "question": "새 질문"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    ready = _plan(tmp_path)
    assert [(t.path, t.kind, t.count) for t in ready.targets] == [(path, "lines", 1)]

    retention.apply(ready)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["question"] for row in rows] == ["새 질문"]


def test_a_feedback_log_uses_its_own_timestamp_field(tmp_path):
    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "feedback-2026-07.jsonl"
    path.write_text(
        json.dumps({"at": _kst_iso(200), "kind": "correction"}) + "\n", encoding="utf-8"
    )

    assert [t.path for t in _plan(tmp_path).targets] == [path]


def test_an_unreadable_qa_line_is_kept(tmp_path):
    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text("{깨진 줄\n", encoding="utf-8")

    ready = _plan(tmp_path)

    assert ready.targets == []
    assert any("읽을 수 없는 줄" in u.reason for u in ready.unresolved)
    retention.apply(ready)
    assert path.read_text(encoding="utf-8") == "{깨진 줄\n"


def test_a_daily_markdown_is_kept_until_its_day_is_fully_past(tmp_path):
    """자정에 쓴 기록을 **하루 일찍** 지우지 않는다."""
    qa = tmp_path / "qa-log"
    qa.mkdir()
    edge = (NOW - timedelta(days=90)).astimezone(retention.KST).strftime("%Y-%m-%d")
    old = (NOW - timedelta(days=200)).astimezone(retention.KST).strftime("%Y-%m-%d")
    (qa / f"{edge}.md").write_text("경계", encoding="utf-8")
    (qa / f"{old}.md").write_text("옛날", encoding="utf-8")

    assert [t.path.name for t in _plan(tmp_path).targets] == [f"{old}.md"]


# ---------------------------------------------------------------------------
# 감사 로그 동시 쓰기
# ---------------------------------------------------------------------------


def test_the_writer_and_the_purge_share_one_lock(tmp_path):
    """같은 파일이면 **같은 락**이어야 한다. 다르면 둘 다 잡아도 소용없다."""
    from tybot.audit import append_lock

    path = tmp_path / "qa-2026-07.jsonl"
    assert append_lock(path).path == append_lock(Path(str(path))).path
    assert append_lock(path).path != append_lock(tmp_path / "other.jsonl").path


def test_the_purge_defers_while_a_writer_holds_the_lock(tmp_path):
    """덧붙이는 쪽이 쥐고 있으면 **이번엔 두고 간다.**

    기다리다 끼어들면 그 사이의 기록을 잃는다. 사라진 감사 기록은 사라진 줄
    모른다 — 그래서 잃는 쪽보다 미루는 쪽이다.
    """
    from tybot.audit import append_lock

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    body = json.dumps({"ts": _kst_iso(200)}) + "\n"
    path.write_text(body, encoding="utf-8")
    ready = _plan(tmp_path)
    assert ready.targets

    held = append_lock(path)
    held.acquire()
    try:
        import tybot.retention as module

        monkey = module.LOCK_TIMEOUT
        module.LOCK_TIMEOUT = 0.0
        try:
            result = retention.apply(ready)
        finally:
            module.LOCK_TIMEOUT = monkey
    finally:
        held.release()

    assert result["removed"][retention.POLICY_BOT_AUDIT] == 0
    assert path.read_text(encoding="utf-8") == body


def test_the_audit_writer_takes_the_lock_before_appending():
    """쓰는 쪽이 락을 안 잡으면 거르는 쪽이 혼자 잡아도 아무 보호가 없다."""
    import inspect

    from tybot.audit import QALog
    from tybot.feedback import FeedbackLog

    assert "appending(" in inspect.getsource(QALog.write)
    assert "appending(" in inspect.getsource(FeedbackLog.write)


def test_an_append_still_happens_when_the_lock_cannot_be_taken(tmp_path, monkeypatch):
    """락을 못 잡아도 **기록은 남긴다.** 기록을 잃는 쪽이 더 나쁘다."""
    from tybot import audit

    def refuse(_path):
        raise OSError("no lock here")

    monkeypatch.setattr(audit, "append_lock", refuse)
    path = tmp_path / "qa-2026-07.jsonl"
    with audit.appending(path), path.open("a", encoding="utf-8") as handle:
        handle.write("기록\n")

    assert path.read_text(encoding="utf-8") == "기록\n"


def test_a_line_appended_after_planning_is_not_lost(tmp_path):
    """계획과 적용 사이에 덧붙은 줄은 **끝에** 붙는다.

    그래서 계획이 센 줄 번호는 그대로 맞고, 새 줄은 남는다.
    """
    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(200), "question": "옛 질문"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    ready = _plan(tmp_path)
    assert ready.targets

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": _kst_iso(1), "question": "방금 질문"}, ensure_ascii=False)
            + "\n"
        )

    retention.apply(ready)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["question"] for row in rows] == ["방금 질문"]


def test_a_jsonl_that_grows_mid_swap_is_left_for_next_time(monkeypatch, tmp_path):
    """락을 쥔 채로도 크기를 견준다 — 락을 못 잡고 쓴 프로세스가 있을 수 있다."""
    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    body = json.dumps({"ts": _kst_iso(200)}) + "\n"
    path.write_text(body, encoding="utf-8")
    ready = _plan(tmp_path)
    assert ready.targets

    sizes = iter([len(body), len(body) + 1])
    monkeypatch.setattr(retention, "_size_of", lambda _path: next(sizes))

    result = retention.apply(ready)

    assert result["removed"][retention.POLICY_BOT_AUDIT] == 0
    # **0건은 성공이 아니다.** 미뤘다고 적지 않으면 그 실행이 집행으로 인정되고
    # 이 파일은 아무도 다시 보지 않는다.
    assert result["deferred"], "커진 파일을 미뤘다는 사실이 결과에 없습니다"
    assert path.read_text(encoding="utf-8") == body
    assert not list(qa.glob(".*tmp*"))


# ---------------------------------------------------------------------------
# 첨부 원본
# ---------------------------------------------------------------------------


def _with_attachment(archive: Path, *, days_ago: float, key: str):
    stamp = _epoch(days_ago)
    doc = _dm_doc(archive, "U12345678", [
        (stamp, f"[DM attachment original retained: F1; object={key}; extraction pending]"),
    ])
    return doc, _object(archive, "U12345678", key)


def test_an_attachment_expires_on_the_coordinate_of_its_line(tmp_path):
    archive = tmp_path / "archive"
    doc, blob = _with_attachment(archive, days_ago=700, key="c" * 64)

    assert {t.path for t in _plan(tmp_path).targets} == {doc, blob}


def test_attachments_are_removed_before_the_lines_that_locate_them(tmp_path):
    """원본의 좌표는 원문 줄에서 나온다.

    원문을 먼저 지우면 중간에 멈췄을 때 남은 원본의 만료 여부를 **영영 모른다.**
    """
    archive = tmp_path / "archive"
    doc, blob = _with_attachment(archive, days_ago=700, key="d" * 64)
    order: list[Path] = []
    original = Path.unlink

    def watched(self, missing_ok=False):
        order.append(Path(self))
        original(self, missing_ok=missing_ok)

    ready = _plan(tmp_path)
    Path.unlink = watched
    try:
        retention.apply(ready)
    finally:
        Path.unlink = original

    assert order.index(blob) < order.index(doc)


def test_an_attachment_whose_line_is_still_live_is_kept(tmp_path):
    archive = tmp_path / "archive"
    _, blob = _with_attachment(archive, days_ago=3, key="e" * 64)

    assert blob not in [t.path for t in _plan(tmp_path).targets]


# ---------------------------------------------------------------------------
# 4. 설정값이 아니라 실행이 근거다
# ---------------------------------------------------------------------------


def test_an_undecided_policy_removes_nothing(tmp_path):
    """`NULL` 은 「영구」 가 아니라 **「건드리지 않음」** 이다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U12345678", [(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path, days=dict(DAYS, bot_dm_message=None))

    assert path not in [t.path for t in ready.targets]
    assert any("미결정" in note for note in ready.skipped)
    assert not ready.complete


def test_a_policy_outside_the_scope_is_skipped(tmp_path):
    archive = tmp_path / "archive"
    _dm_doc(archive, "U12345678", [(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path, policies=(retention.POLICY_BOT_AUDIT,))

    assert ready.targets == []
    assert any(retention.POLICY_DM_MESSAGE in note for note in ready.skipped)
    assert ready.covered == (retention.POLICY_BOT_AUDIT,)
    assert not ready.complete


def test_a_full_run_is_marked_complete(tmp_path):
    assert _whole(tmp_path).complete


def test_planning_alone_never_deletes(tmp_path):
    """기본은 **읽기 전용 미리보기**다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U12345678", [(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path)

    assert ready.targets
    assert path.exists()
    assert ready.counts()[retention.POLICY_DM_MESSAGE] == 1


def test_a_run_is_recorded_even_when_it_only_previewed(tmp_path):
    ready = _whole(tmp_path)
    path = retention.record_run(
        ready, {"removed": {}, "failed": []}, actor="dan", applied=False,
        state_dir=tmp_path, now=NOW,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["applied"] is False
    assert saved["by"] == "dan"
    assert saved["days"] == DAYS
    assert sorted(saved["covered"]) == sorted(retention.POLICIES)


def _fresh(**over) -> dict:
    record = {
        "at": (NOW - timedelta(days=1)).isoformat(),
        "applied": True,
        "covered": list(retention.POLICIES),
        "failed": [],
    }
    record.update(over)
    return record


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "한 번도 실행되지 않았습니다"),
        (_fresh(applied=False), "미리보기로만"),
        (_fresh(at=(NOW - timedelta(days=30)).isoformat()), "30일 전"),
        (_fresh(at="언제인지모름"), "시각을 읽을 수 없습니다"),
        (_fresh(covered=["bot_conversation_audit"]), "보지 않은 정책이 있습니다"),
        (_fresh(covered=None), "보지 않은 정책이 있습니다"),
        (_fresh(failed=["/var/lib/tybot/archive/x.md: 권한 없음"]), "지우지 못했습니다"),
    ],
)
def test_enforcement_is_not_claimed_without_a_complete_recent_run(
    tmp_path, record, expected
):
    """**값이 있다는 사실은 집행이 아니다.** 2026-10-02 까지 정확히 그랬다.

    「돌았다」 와 「전부 돌았다」 도 다르다. 한 정책만 돌린 실행이나 실패가 있는
    실행을 집행으로 인정하면, 나머지 자료는 영원히 안 지워진 채로 통과한다.
    """
    if record is not None:
        path = tmp_path / "state" / retention.STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    running, why = retention.enforcement_status(tmp_path, now=NOW)

    assert running is False
    assert expected in why


def test_a_recent_complete_run_counts_as_enforcement(tmp_path):
    path = tmp_path / "state" / retention.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_fresh()), encoding="utf-8")

    assert retention.enforcement_status(tmp_path, now=NOW) == (True, "")


def test_the_production_gate_asks_whether_enforcement_runs():
    """정책값만 보면 아무것도 안 지워진 채로 운영에 간다."""
    settled = {name: 90 for name in retention.POLICIES}
    flags = dict.fromkeys(
        ("archiver_writes_live", "require_attachment_ack"), True
    )

    assert production_blockers(settled, flags=flags) == []
    blockers = production_blockers(
        settled, flags=flags, enforcement=(False, "보존 집행이 한 번도 실행되지 않았습니다")
    )
    assert blockers == [
        "보존 기간이 집행되지 않고 있습니다: 보존 집행이 한 번도 실행되지 않았습니다"
    ]


def test_every_policy_this_module_enforces_is_required_for_production():
    """집행하는 것과 요구하는 것이 갈리면, 안 정한 자료가 조용히 영구 보관된다."""
    from tybot.archive.archiving_state import REQUIRED_RETENTION

    assert set(retention.POLICIES) == set(REQUIRED_RETENTION)


def test_the_schema_declares_every_policy_the_code_enforces():
    """코드가 지우는 이름이 표에 없으면 운영자가 기간을 정할 자리가 없다."""
    schema = (
        Path(__file__).resolve().parents[1] / "deploy" / "sql" / "archiving_schema.sql"
    ).read_text(encoding="utf-8")
    for name in retention.POLICIES:
        assert f"('{name}'," in schema


# ---------------------------------------------------------------------------
# 지우지 않는 사본을 **숨기지 않는다**
# ---------------------------------------------------------------------------


def test_copies_this_policy_cannot_reach_are_named(capsys, tmp_path):
    """안 보이면 「90일 집행 완료」 로 읽히고, 사본은 남는다."""
    retention._report(_plan(tmp_path))
    printed = capsys.readouterr().out

    assert "journald" in printed
    for name, _why in retention.UNMANAGED_COPIES:
        assert name in printed


def test_the_report_separates_undecided_from_zero(capsys, tmp_path):
    retention._report(_plan(tmp_path, days=dict(DAYS, bot_dm_attachment=None)))
    printed = capsys.readouterr().out

    assert "미결정" in printed


# ---------------------------------------------------------------------------
# 락을 못 잡은 writer 와 교체 사이의 경쟁
# ---------------------------------------------------------------------------


def test_an_append_during_the_swap_is_not_lost(tmp_path):
    """**이 순서**를 재현한다.

    1. 집행자가 락을 잡는다
    2. writer 가 타임아웃된다 (집행자가 쥐고 있으므로)
    3. 집행자가 크기를 확인한다
    4. writer 가 덧붙인다  ← 여기
    5. 집행자가 `os.replace` 한다

    4번 줄은 교체되는 사본에 없다. 그래서 **사라진다.** 오류는 안 난다 —
    사라진 감사 기록은 사라진 줄 모른다.

    고치는 방향은 「writer 를 막는다」 가 아니다. 기록을 잃는 것보다 나쁜 선택이
    없으므로 writer 는 언제나 써야 하고, **집행이 미뤄져야** 한다.
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(200), "question": "옛 질문"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    ready = _plan(tmp_path)
    assert ready.targets

    # 집행자가 락을 잡은 **그 상태로** writer 가 들어온다. 크기 확인과 교체
    # 사이(3~5번)에 끼어들게 둔다.
    audit_calls: list[str] = []
    real_size = retention._size_of
    seen = 0

    def size_then_append(target_path):
        nonlocal seen
        seen += 1
        value = real_size(target_path)
        if seen == 2:
            # 크기를 이미 확인한 뒤다. writer 가 타임아웃되고 그냥 쓴다.
            with audit.appending(path) as writable, writable.open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps({"ts": _kst_iso(0), "question": "방금 질문"},
                               ensure_ascii=False) + "\n"
                )
                audit_calls.append("appended")
        return value

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(retention, "_size_of", size_then_append)
    monkeypatched.setattr(retention, "LOCK_TIMEOUT", 0.0)
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        retention.apply(ready)
    finally:
        monkeypatched.undo()

    assert audit_calls == ["appended"], "경쟁을 재현하지 못했습니다"

    # **잃지 않았다.** 락을 못 잡은 writer 는 살아 있는 파일을 건드리지 않고
    # 옆자리에 **새 항목**으로 쌓았으므로, 교체가 그 줄을 지나쳐도 줄은 그대로다.
    entries = audit.spill_entries(path)
    assert entries, "방금 쓴 감사 줄이 사라졌습니다"
    assert "방금 질문" in entries[0].read_text(encoding="utf-8")

    # **그리고 다시 보인다.** 다음에 락을 잡는 사람이 합친다 — 보통 다음 기록이
    # 밀리초 뒤에 온다. 합쳐지기 전까지 잠깐 안 보이는 쪽을 고른 이유는, 글롭에
    # 걸리게 두면 합친 뒤 같은 기록이 두 번 읽히기 때문이다.
    with audit.appending(path) as target, target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": _kst_iso(0), "question": "다음 질문"}, ensure_ascii=False)
            + "\n"
        )

    assert audit.spill_entries(path) == []
    assert _questions(qa) == ["방금 질문", "다음 질문"]


def _questions(qa: Path) -> list[str]:
    """그 달의 감사 기록 **전부**. 읽는 쪽이 보는 것과 같은 글롭을 쓴다."""
    found: list[str] = []
    for path in sorted(qa.glob("qa-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                found.append(json.loads(line).get("question", ""))
    return found


def test_the_spill_is_invisible_to_readers_until_it_is_merged(tmp_path):
    """옆자리는 `qa-*.jsonl` 글롭에 **걸리면 안 된다.**

    걸리면 합친 뒤 같은 기록이 두 번 읽힌다. 감사 기록이 두 번 세어지는 것은
    사라지는 것만큼 나쁘지는 않지만, 둘 다 거짓이다.

    디렉터리라 글롭이 한 겹만 보는 것만으로도 안 걸리지만, 이름까지 못 박는다 —
    나중에 누가 `.jsonl` 로 끝내고 싶어질 수 있다.
    """
    import fnmatch

    from tybot import audit

    path = tmp_path / "qa-2026-07.jsonl"
    path.write_text("", encoding="utf-8")
    directory = audit.spill_dir(path)
    assert not fnmatch.fnmatch(directory.name, "qa-*.jsonl")

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리")
    finally:
        monkeypatched.undo()
        held.release()

    assert audit.spill_entries(path)
    assert list(tmp_path.glob("qa-*.jsonl")) == [path]


def test_the_purge_merges_the_spill_before_filtering(tmp_path):
    """집행도 락을 쥐면 **먼저 합친다.** 안 합치면 옆자리가 영원히 남는다."""
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(200), "question": "옛 질문"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리 질문")
    finally:
        monkeypatched.undo()
        held.release()
    ready = _whole(tmp_path)
    assert ready.targets

    retention.apply(ready)

    assert audit.spill_entries(path) == []
    assert _questions(qa) == ["옆자리 질문"]
def test_a_deferred_purge_is_not_recorded_as_a_success(tmp_path):
    """락을 못 잡아 미룬 것은 **성공이 아니다.**

    0건 지웠다고 적으면 그 실행이 집행으로 인정되고, 미뤄진 파일은 아무도
    다시 보지 않는다.
    """
    from tybot.audit import append_lock

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(json.dumps({"ts": _kst_iso(200)}) + "\n", encoding="utf-8")
    ready = _whole(tmp_path)
    assert ready.targets

    held = append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(retention, "LOCK_TIMEOUT", 0.0)
    try:
        result = retention.apply(ready)
    finally:
        monkeypatched.undo()
        held.release()

    assert result["deferred"], "미뤘다는 사실이 결과에 없습니다"
    record = json.loads(
        retention.record_run(
            ready, result, actor="dan", applied=True, state_dir=tmp_path, now=NOW
        ).read_text(encoding="utf-8")
    )
    assert record["deferred"], "미뤘다는 사실이 집행 기록에 없습니다"
    running, why = retention.enforcement_status(tmp_path, now=NOW)
    assert running is False
    assert "미뤘" in why or "미룬" in why


# ---------------------------------------------------------------------------
# 못 본 정책을 「봤다」 로 세지 않는다
# ---------------------------------------------------------------------------


def test_a_missing_handoff_directory_leaves_the_dm_policy_uncovered(tmp_path):
    """건너뛴 것을 `covered` 에 넣으면 게이트가 **전부 집행** 으로 읽는다."""
    ready = _plan(tmp_path)

    assert any("ARCHIVER_DM_HANDOFF_DIR" in note for note in ready.skipped)
    assert retention.POLICY_DM_MESSAGE not in ready.covered
    assert not ready.complete


def test_a_spool_reader_failure_leaves_the_dm_policy_uncovered(tmp_path):
    spool = tmp_path / "handoff" / WS / "pending"
    spool.mkdir(parents=True)
    (spool / f"{'f' * 64}.bin").write_bytes(b"sealed")

    ready = _plan(tmp_path, handoff_dir=tmp_path / "handoff")

    assert any("봉인을 열 수단이 없어" in note for note in ready.skipped)
    assert retention.POLICY_DM_MESSAGE not in ready.covered
    assert not ready.complete


@pytest.mark.parametrize(
    "broken",
    ["handoff-missing", "reader-missing", "delete-failed", "partial-policies"],
)
def test_an_incomplete_run_never_opens_the_production_gate(tmp_path, broken):
    """네 가지 모두 **게이트를 통과하면 안 된다.**

    통과하면 그 자료는 아무도 안 지운 채로 운영에 간다. 그리고 화면은
    「집행 중」 이라고 말한다.
    """
    if broken == "handoff-missing":
        ready = _plan(tmp_path)
        result = {"removed": {}, "failed": [], "deferred": []}
    elif broken == "reader-missing":
        spool = tmp_path / "handoff" / WS / "pending"
        spool.mkdir(parents=True)
        (spool / f"{'a' * 64}.bin").write_bytes(b"sealed")
        ready = _plan(tmp_path, handoff_dir=tmp_path / "handoff")
        result = {"removed": {}, "failed": [], "deferred": []}
    elif broken == "delete-failed":
        ready = _plan(
            tmp_path, handoff_dir=tmp_path / "handoff",
            spool_reader=lambda *_a: _epoch(1),
        )
        result = {"removed": {}, "failed": ["/x.md: 권한 없음"], "deferred": []}
    else:
        ready = _plan(tmp_path, policies=(retention.POLICY_BOT_AUDIT,))
        result = {"removed": {}, "failed": [], "deferred": []}

    retention.record_run(
        ready, result, actor="dan", applied=True, state_dir=tmp_path, now=NOW
    )
    running, why = retention.enforcement_status(tmp_path, now=NOW)

    assert running is False, f"{broken} 인데 집행 중으로 판정했습니다"
    assert why


# ---------------------------------------------------------------------------
# 미리보기가 마지막 집행 기록을 덮지 않는다
# ---------------------------------------------------------------------------


def test_a_preview_does_not_overwrite_the_last_applied_run(tmp_path):
    """미리보기는 집행이 아니다. 덮으면 **어제 실제로 돌린 사실**이 사라진다.

    그 순간 게이트가 「미리보기로만 실행됐습니다」 로 닫히고, 운영자는 멀쩡히
    돌고 있는 집행을 다시 돌리러 간다.
    """
    ready = _plan(tmp_path, handoff_dir=tmp_path / "handoff",
                  spool_reader=lambda *_a: _epoch(1))
    retention.record_run(
        ready, {"removed": {}, "failed": [], "deferred": []},
        actor="dan", applied=True, state_dir=tmp_path, now=NOW,
    )
    assert retention.enforcement_status(tmp_path, now=NOW)[0] is True

    retention.record_run(
        ready, {"removed": {}, "failed": [], "deferred": []},
        actor="preview", applied=False, state_dir=tmp_path, now=NOW,
    )

    running, why = retention.enforcement_status(tmp_path, now=NOW)
    assert running is True, f"미리보기가 집행 기록을 덮었습니다: {why}"


# ---------------------------------------------------------------------------
# 옆자리(spill) 자체의 경합
# ---------------------------------------------------------------------------


def test_an_append_to_the_spill_during_the_drain_is_not_lost(tmp_path):
    """합치는 **중에** 들어온 줄이 사라지면 안 된다.

    1. 집행자(또는 다음 writer)가 락을 잡고 옆자리를 읽는다
    2. 락을 못 잡은 writer 가 **같은 옆자리에** 덧붙인다
    3. 합친 쪽이 옆자리를 지운다  ← 2번 줄이 여기서 사라진다

    옆자리를 **한 파일**로 두면 이 순서를 막을 수 없다. 읽기와 지우기 사이는
    언제나 벌어져 있고, 그 사이에 쓰는 쪽은 락이 없다.
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(1), "question": "본 파일"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리 먼저")
    finally:
        monkeypatched.undo()
        held.release()

    # 합치는 중에 끼어든다. 지우기 **직전**이 가장 나쁜 자리다.
    intruded: list[str] = []
    original = Path.unlink

    def unlink_but_first_append(self, missing_ok=False):
        if not intruded and "spill" in str(self):
            intruded.append(str(self))
            patch = pytest.MonkeyPatch()
            patch.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
            try:
                _write_unlocked(path, "합치는 중에 들어옴")
            finally:
                patch.undo()
        original(self, missing_ok=missing_ok)

    second = audit.append_lock(path)
    second.acquire()
    Path.unlink = unlink_but_first_append
    try:
        audit.drain_spill(path)
    finally:
        Path.unlink = original
        second.release()

    assert intruded, "합치는 중 끼어들기를 재현하지 못했습니다"

    # 아직 본 파일에 없어도 된다 — 다음 합치기에서 들어오면 된다.
    # **사라지지만 않으면 된다.**
    with audit.appending(path) as target, target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": _kst_iso(0), "question": "마지막"}, ensure_ascii=False)
            + "\n"
        )
    assert _questions(qa) == ["본 파일", "옆자리 먼저", "합치는 중에 들어옴", "마지막"]


def test_a_crash_between_merging_and_cleanup_does_not_duplicate(tmp_path):
    """합친 뒤 지우기 전에 죽으면, 다음 합치기가 **같은 줄을 또** 넣으면 안 된다.

    감사 기록이 두 번 세어지는 것은 사라지는 것만큼 나쁘지는 않지만 둘 다
    거짓이다. 특히 같은 질문이 두 번 있었던 것처럼 보인다.
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(1), "question": "본 파일"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리")
    finally:
        monkeypatched.undo()
        held.release()

    # 합치는 도중 죽는다 — 본 파일에는 들어갔는데 옆자리는 안 지워졌다.
    original = Path.unlink
    Path.unlink = lambda self, missing_ok=False: None
    try:
        audit.drain_spill(path)
    finally:
        Path.unlink = original

    assert _questions(qa) == ["본 파일", "옆자리"]

    # 다시 살아나서 한 번 더 합친다.
    audit.drain_spill(path)

    assert _questions(qa) == ["본 파일", "옆자리"], "같은 줄이 두 번 합쳐졌습니다"


def test_a_spill_without_a_live_file_is_merged(tmp_path):
    """본 파일이 아직 없을 수 있다. 그때도 **잃지 않는다.**"""
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "본 파일 없이 들어온 줄")
    finally:
        monkeypatched.undo()
        held.release()

    assert not path.exists()

    audit.drain_spill(path)

    assert _questions(qa) == ["본 파일 없이 들어온 줄"]


def test_an_unreadable_spill_defers_the_purge(tmp_path):
    """옆자리를 못 읽었으면 **집행 성공이 아니다.**

    0건 성공으로 적으면 그 실행이 집행으로 인정되고, 못 읽은 줄은 아무도 다시
    보지 않는다.
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text(
        json.dumps({"ts": _kst_iso(200), "question": "옛 질문"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리")
    finally:
        monkeypatched.undo()
        held.release()

    ready = _whole(tmp_path)
    assert ready.targets

    broken = pytest.MonkeyPatch()
    broken.setattr(
        audit, "drain_spill",
        lambda _path: (_ for _ in ()).throw(OSError("옆자리를 못 읽었다")),
    )
    try:
        result = retention.apply(ready)
    finally:
        broken.undo()

    assert result["deferred"], "못 읽은 옆자리를 미뤘다고 적지 않았습니다"
    assert result["removed"][retention.POLICY_BOT_AUDIT] == 0
    assert "옛 질문" in _questions(qa), "미뤘는데 원본을 건드렸습니다"


def test_a_drain_reports_what_it_could_not_read(tmp_path):
    """합치지 못한 것이 있으면 **숫자가 아니라 사실로** 돌려준다."""
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text("", encoding="utf-8")

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        _write_unlocked(path, "옆자리")
    finally:
        monkeypatched.undo()
        held.release()

    original = Path.read_bytes

    def refuse(self):
        if "spill" in str(self):
            raise OSError("못 읽는다")
        return original(self)

    Path.read_bytes = refuse
    try:
        with pytest.raises(OSError):
            audit.drain_spill(path)
    finally:
        Path.read_bytes = original


def test_a_half_written_entry_is_never_merged(tmp_path):
    """쓰는 중인 파일을 합치면 **반쯤 쓰인 줄**이 감사 기록에 들어간다.

    그건 사라진 줄보다 나쁘다 — 사라진 줄은 없는 것으로 보이지만, 잘린 줄은
    있는 것처럼 보이고 JSON 으로도 안 읽힌다.
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text("", encoding="utf-8")

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        with audit.appending(path) as target:
            # 아직 다 안 썼다. **이 순간** 합치는 쪽이 들어온다.
            with target.open("a", encoding="utf-8") as handle:
                handle.write('{"ts": "2026-10-02T09:00:00", "ques')
                handle.flush()
            assert audit.spill_entries(path) == [], "쓰는 중인 파일이 합칠 대상입니다"
            assert audit.drain_spill(path) == 0
            assert path.read_text(encoding="utf-8") == ""
            # 마저 쓴다.
            with target.open("a", encoding="utf-8") as handle:
                handle.write('tion": "끝까지 쓴 줄"}\n')
    finally:
        monkeypatched.undo()
        held.release()

    # 다 쓰고 나서야 합칠 대상이 된다.
    assert len(audit.spill_entries(path)) == 1
    audit.drain_spill(path)
    assert _questions(qa) == ["끝까지 쓴 줄"]


def test_entries_are_merged_in_the_order_they_were_written(tmp_path):
    """합치는 순서가 섞이면 감사 기록의 **시간순**이 깨진다.

    스레드 문맥은 이 순서를 그대로 읽는다(`audit.context_for_thread`).
    """
    from tybot import audit

    qa = tmp_path / "qa-log"
    qa.mkdir()
    path = qa / "qa-2026-07.jsonl"
    path.write_text("", encoding="utf-8")

    held = audit.append_lock(path)
    held.acquire()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        for order in ("첫째", "둘째", "셋째"):
            _write_unlocked(path, order)
    finally:
        monkeypatched.undo()
        held.release()

    assert len(audit.spill_entries(path)) == 3
    audit.drain_spill(path)

    assert _questions(qa) == ["첫째", "둘째", "셋째"]


def test_failed_spill_write_is_not_published_as_a_complete_entry(tmp_path, monkeypatch):
    from tybot import audit

    path = tmp_path / "qa-2026-07.jsonl"
    held = audit.append_lock(path)
    held.acquire()
    monkeypatch.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        with pytest.raises(OSError, match="interrupted"):
            with audit.appending(path) as target:
                target.write_text('{"question": "unfinished', encoding="utf-8")
                raise OSError("interrupted")
    finally:
        held.release()

    assert audit.spill_entries(path) == []


def test_identical_spilled_feedback_events_are_not_collapsed(tmp_path, monkeypatch):
    from tybot import audit

    path = tmp_path / "feedback-2026-07.jsonl"
    record = '{"at":"2026-07-01T00:00:00+09:00","kind":"positive"}\n'
    held = audit.append_lock(path)
    held.acquire()
    monkeypatch.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        for _ in range(2):
            with audit.appending(path) as target:
                target.write_text(record, encoding="utf-8")
    finally:
        held.release()

    assert audit.drain_spill(path) == 2
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_drain_finishes_a_partially_appended_entry_after_a_crash(tmp_path, monkeypatch):
    from tybot import audit

    path = tmp_path / "qa-2026-07.jsonl"
    held = audit.append_lock(path)
    held.acquire()
    monkeypatch.setattr(audit, "APPEND_LOCK_TIMEOUT", 0.0)
    try:
        with audit.appending(path) as target:
            target.write_text('{"question":"one"}\n', encoding="utf-8")
    finally:
        held.release()

    entry = audit.spill_entries(path)[0]
    data = audit._spill_data(entry)
    path.write_bytes(data[:12])
    assert audit.drain_spill(path) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["question"] == "one"
    assert audit.spill_entries(path) == []
