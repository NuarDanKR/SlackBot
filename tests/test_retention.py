"""보존 기간이 **실제 자료에 적용되는가.**

결정: 2026-10-02 오너(B-70). 콘솔 설정·영구 보관은 B-71 이라 여기 없다.

2026-10-02 조사에서 드러난 모양 — `archive_retention_policy` 에 90일이 들어
있었지만 그 값을 읽어 지우는 코드가 없었다. 체크리스트는 통과하는데 아무것도
만료되지 않았고, 사람은 체크된 것을 보증으로 읽었다.

여기 시험이 지키는 것은 넷이다.

1. 만료 기준은 **원래 좌표**다 — 파일 mtime 이 아니다
2. 좌표를 모르면 **안 지운다**
3. 아카이브 원문은 **재작성하지 않는다**
4. 설정값이 아니라 **실행**이 집행의 근거다
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tybot import retention
from tybot.archive.archiving_state import production_blockers

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
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


def _dm_doc(root: Path, user: str, name: str, lines: list[str]) -> Path:
    path = root / "tyit" / "dm" / user / "archive" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["---", "schema_version: 2", "---", "", "## 원문", ""]
    body.extend(lines)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _raw(message_ts: str, text: str, *, speaker: str = "김태영") -> str:
    return f"> [2026-09-16 14:23|{message_ts}] {speaker}: {text}"


def _object(root: Path, user: str, key: str) -> Path:
    path = root / "tyit" / "dm" / user / "objects" / "tyit" / "files" / f"{key}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"sealed")
    return path


def _plan(tmp_path: Path, *, days=None, **over) -> retention.Plan:
    return retention.plan(
        days=days if days is not None else DAYS,
        qa_dir=over.pop("qa_dir", tmp_path / "qa-log"),
        archive_root=over.pop("archive_root", tmp_path / "archive"),
        state_dir=over.pop("state_dir", tmp_path / "state"),
        now=NOW,
        **over,
    )


# ---------------------------------------------------------------------------
# 1. 만료 기준은 **원래 좌표**다
# ---------------------------------------------------------------------------


def test_a_backfilled_file_expires_on_its_slack_coordinate(tmp_path):
    """소급 수집은 2년 전 대화를 **오늘** 파일로 쓴다.

    mtime 으로 세면 90일이 그날부터 새로 시작한다 — 2년 하고도 90일을 더
    보관하게 되고, 그건 아무도 정한 적 없는 기간이다.
    """
    archive = tmp_path / "archive"
    old = _dm_doc(archive, "U1", "2024-05-01", [_raw(_epoch(700), "2년 전 이야기")])
    # 파일 자체는 방금 쓰였다(소급 수집).
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))

    ready = _plan(tmp_path)

    assert [t.path for t in ready.targets] == [old]


def test_a_fresh_coordinate_in_an_old_file_is_kept(tmp_path):
    """반대쪽도 본다 — 파일이 오래됐다고 내용이 만료된 것이 아니다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U1", "2026-09-30", [_raw(_epoch(3), "어제 이야기")])
    stale = (NOW - timedelta(days=400)).timestamp()
    os.utime(path, (stale, stale))

    assert _plan(tmp_path).targets == []


# ---------------------------------------------------------------------------
# 2. 좌표를 모르면 안 지운다
# ---------------------------------------------------------------------------


def test_a_line_without_a_coordinate_keeps_the_whole_file(tmp_path):
    """좌표 없는 옛 줄은 **모르는 것**이지 오래된 것이 아니다."""
    archive = tmp_path / "archive"
    _dm_doc(archive, "U1", "2024-05-01", [
        _raw(_epoch(700), "좌표 있는 옛 줄"),
        "> [2024-05-01 10:00] 김태영: 좌표 없는 옛 줄",
    ])

    ready = _plan(tmp_path)

    assert ready.targets == []
    assert [u.reason for u in ready.unresolved] == ["좌표 없는 원문 줄이 있습니다"]


def test_an_object_without_a_referring_line_is_not_removed(tmp_path):
    """가리키는 줄이 없는 원본은 **좌표를 모르는 것**이다.

    지우면 되돌릴 수 없고, 안 지우면 다음 실행에서 다시 본다. 두 선택의 값이
    다르다.
    """
    archive = tmp_path / "archive"
    orphan = _object(archive, "U1", "a" * 64)

    ready = _plan(tmp_path)

    assert orphan not in [t.path for t in ready.targets]
    assert any("가리키는 원문 줄이 없습니다" in u.reason for u in ready.unresolved)


def test_a_spool_entry_whose_seal_cannot_be_read_is_kept(tmp_path):
    """봉인을 못 열면 좌표를 모른다. 큐 파일의 mtime 으로 세지 않는다."""
    spool = tmp_path / "state" / "dm-handoff" / "tyit" / "pending"
    spool.mkdir(parents=True)
    (spool / f"{'b' * 64}.bin").write_bytes(b"sealed")

    ready = _plan(tmp_path, spool_reader=lambda *_a: (_ for _ in ()).throw(ValueError))

    assert ready.targets == []
    assert any("봉인 안의 좌표" in u.reason for u in ready.unresolved)


def test_a_spool_entry_expires_on_the_coordinate_inside_the_seal(tmp_path):
    spool = tmp_path / "state" / "dm-handoff" / "tyit" / "pending"
    spool.mkdir(parents=True)
    entry = spool / f"{'b' * 64}.bin"
    entry.write_bytes(b"sealed")

    ready = _plan(tmp_path, spool_reader=lambda *_a: _epoch(200))

    assert [t.path for t in ready.targets] == [entry]


# ---------------------------------------------------------------------------
# 3. 아카이브 원문은 재작성하지 않는다
# ---------------------------------------------------------------------------


def test_one_live_line_keeps_the_whole_dm_file(tmp_path):
    """한 줄이라도 안 지났으면 건너뛴다.

    줄을 골라 지우면 원문을 고치는 것이고(절대 원칙 1), 남은 줄의 `lineno` 가
    어긋나 기존 출처가 다른 문장을 가리킨다.
    """
    archive = tmp_path / "archive"
    _dm_doc(archive, "U1", "2026-06-01", [
        _raw(_epoch(200), "만료된 줄"),
        _raw(_epoch(3), "아직 살아 있는 줄"),
    ])

    assert _plan(tmp_path).targets == []


def test_a_dm_file_is_only_ever_removed_whole(tmp_path):
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U1", "2024-05-01", [
        _raw(_epoch(700), "하나"), _raw(_epoch(650), "둘"),
    ])

    ready = _plan(tmp_path)

    assert [(t.path, t.kind) for t in ready.targets] == [(path, "file")]
    retention.apply(ready)
    assert not path.exists()


def test_another_users_dm_is_untouched(tmp_path):
    """만료되지 않은 **다른 사람** 자료는 건드리지 않는다."""
    archive = tmp_path / "archive"
    mine = _dm_doc(archive, "U1", "2024-05-01", [_raw(_epoch(700), "옛 이야기")])
    yours = _dm_doc(archive, "U2", "2026-09-30", [_raw(_epoch(3), "어제 이야기")])

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

    ready = _plan(tmp_path)

    assert [t.path for t in ready.targets] == [path]


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

    ready = _plan(tmp_path)

    assert [t.path.name for t in ready.targets] == [f"{old}.md"]


def test_a_line_appended_after_planning_is_not_lost(tmp_path):
    """계획과 적용 사이에 덧붙은 줄은 **끝에** 붙는다.

    그래서 계획이 센 줄 번호는 그대로 맞고, 새 줄은 남는다. 이게 깨지면 방금
    기록한 감사 줄이 조용히 사라진다.
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
    """읽은 뒤 바꿔 끼우는 **사이**에 덧붙으면 그 줄은 사라진다.

    쓰는 쪽은 잠그지 않으므로(`audit.QALog.write`) 여기서 잠가도 소용없다.
    대신 크기를 견준다 — append 만 하는 파일은 크기가 그대로면 아무것도 덧붙지
    않았다는 뜻이다. 달라졌으면 이번엔 두고 다음 실행에서 한다.

    사라진 감사 기록은 **사라진 줄 모른다.** 그래서 잃는 쪽보다 미루는 쪽이다.
    """
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
    assert path.read_text(encoding="utf-8") == body
    assert not list(qa.glob(".*tmp*"))


# ---------------------------------------------------------------------------
# 첨부 원본
# ---------------------------------------------------------------------------


def _with_attachment(archive: Path, *, days_ago: float, key: str):
    path = _dm_doc(archive, "U1", "2024-05-01", [
        _raw(days_ago and _epoch(days_ago),
             f"[DM attachment original retained: F1; object={key}; extraction pending]"),
    ])
    return path, _object(archive, "U1", key)


def test_an_attachment_expires_on_the_coordinate_of_its_line(tmp_path):
    archive = tmp_path / "archive"
    doc, blob = _with_attachment(archive, days_ago=700, key="c" * 64)

    ready = _plan(tmp_path)

    assert {t.path for t in ready.targets} == {doc, blob}


def test_attachments_are_removed_before_the_lines_that_locate_them(tmp_path):
    """원본의 좌표는 원문 줄에서 나온다.

    원문을 먼저 지우면 중간에 멈췄을 때 남은 원본의 만료 여부를 **영영 모른다.**
    이 순서면 멈춰도 다음 실행이 이어서 한다.
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
    path = _dm_doc(archive, "U1", "2024-05-01", [_raw(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path, days=dict(DAYS, bot_dm_message=None))

    assert path not in [t.path for t in ready.targets]
    assert any("미결정" in note for note in ready.skipped)


def test_a_policy_outside_the_scope_is_skipped(tmp_path):
    archive = tmp_path / "archive"
    _dm_doc(archive, "U1", "2024-05-01", [_raw(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path, policies=(retention.POLICY_BOT_AUDIT,))

    assert ready.targets == []
    assert any(retention.POLICY_DM_MESSAGE in note for note in ready.skipped)


def test_planning_alone_never_deletes(tmp_path):
    """기본은 **읽기 전용 미리보기**다."""
    archive = tmp_path / "archive"
    path = _dm_doc(archive, "U1", "2024-05-01", [_raw(_epoch(700), "옛 이야기")])

    ready = _plan(tmp_path)

    assert ready.targets
    assert path.exists()
    assert ready.counts()[retention.POLICY_DM_MESSAGE] == 1


def test_a_run_is_recorded_even_when_it_only_previewed(tmp_path):
    ready = _plan(tmp_path)
    path = retention.record_run(
        ready, {"removed": {}, "failed": []}, actor="dan", applied=False,
        state_dir=tmp_path, now=NOW,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["applied"] is False
    assert saved["by"] == "dan"
    assert saved["days"] == DAYS


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "한 번도 실행되지 않았습니다"),
        ({"at": NOW.isoformat(), "applied": False}, "미리보기로만"),
        ({"at": (NOW - timedelta(days=30)).isoformat(), "applied": True}, "30일 전"),
        ({"at": "언제인지모름", "applied": True}, "시각을 읽을 수 없습니다"),
    ],
)
def test_enforcement_is_not_claimed_without_a_recent_run(tmp_path, record, expected):
    """**값이 있다는 사실은 집행이 아니다.** 2026-10-02 까지 정확히 그랬다."""
    if record is not None:
        path = tmp_path / "state" / retention.STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")

    running, why = retention.enforcement_status(tmp_path, now=NOW)

    assert running is False
    assert expected in why


def test_a_recent_applied_run_counts_as_enforcement(tmp_path):
    path = tmp_path / "state" / retention.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"at": (NOW - timedelta(days=1)).isoformat(), "applied": True}),
        encoding="utf-8",
    )

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
