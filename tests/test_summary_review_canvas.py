"""요약 검토 Canvas 회차 — 생성·권한·부분 결정(B-50).

설계: `docs/design/summary-review-canvas.md` §10
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import ANY, Mock

import pytest

from tybot import summary_review as sr


def _row(**kw) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "kind": "number_or_schedule",
        "current_text": "",
        "proposed_text": "공정률은 62.5%입니다",
        "evidence_quote": "공정률은 62.5%입니다",
        "evidence_at": "2026-09-16 09:00",
        "evidence_author": "홍길동",
        "source_digest": "d1",
        "state": "pending",
    }
    base.update(kw)
    return base


# --- 예상 요약본 (§2.1) --------------------------------------------------------
def test_projection_replaces_the_exact_approved_sentence():
    approved = ["공정률은 55.0%입니다", "감리 계약은 2월에 끝납니다"]
    rows = [_row(current_text="공정률은 55.0%입니다")]

    got = sr.projected_summary(approved, rows)

    assert got == ["공정률은 62.5%입니다", "감리 계약은 2월에 끝납니다"]


def test_projection_appends_when_there_is_nothing_to_replace():
    assert sr.projected_summary(["기존"], [_row()]) == ["기존", "공정률은 62.5%입니다"]


def test_projection_refuses_when_the_target_sentence_is_ambiguous():
    """바꿀 문장을 못 찾거나 두 번 찾으면 **그리지 않는다.**

    바뀌지 않을 문장이 바뀐다고 읽히면 사람이 그것을 승인한다.
    """
    twice = ["같은 문장", "같은 문장"]
    with pytest.raises(sr.AmbiguousProjection):
        sr.projected_summary(twice, [_row(current_text="같은 문장")])
    with pytest.raises(sr.AmbiguousProjection):
        sr.projected_summary(["다른 문장"], [_row(current_text="없는 문장")])


def test_projection_never_deletes_an_approved_sentence():
    """`closed_issue` 도 교체일 뿐 삭제가 아니다(§2.1)."""
    approved = ["쟁점 A 진행 중", "쟁점 B 진행 중"]
    rows = [_row(kind="closed_issue", current_text="쟁점 A 진행 중",
                 proposed_text="쟁점 A 는 2026-09-15 종결됐습니다")]

    got = sr.projected_summary(approved, rows)

    assert len(got) == len(approved)
    assert "쟁점 B 진행 중" in got


# --- 순서와 지문 ---------------------------------------------------------------
def test_numbers_come_first_everywhere():
    """§10 · 숫자 후보가 Canvas 첫 표와 DM 첫 항목에 온다."""
    rows = [
        _row(id="a", kind="new_issue", proposed_text="발주처 협의 일정이 바뀌었습니다",
             evidence_quote="발주처 협의 일정이 바뀌었습니다"),
        _row(id="b", kind="number_or_schedule", proposed_text="공정률은 62.5%입니다"),
    ]

    assert [r["id"] for r in sr.ordered_rows(rows)] == ["b", "a"]

    body = sr.canvas_markdown(
        channel_label="#전산팀장보고", review_date=date(2026, 9, 16),
        rows=rows, approved=[],
    )
    assert body.index("62.5%") < body.index("발주처 협의")

    blocks = sr.canvas_review_blocks(
        channel_label="#전산팀장보고", review_date=date(2026, 9, 16),
        permalink="https://slack/canvas", artifact_id="A1", rows=rows,
    )
    texts = [b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"]
    assert "62.5" in texts[1]


def test_content_hash_is_stable_across_reruns_but_follows_the_content():
    """재시도에서 같은 지문이 나와야 **같은 회차**로 인식된다.

    조회 순서가 달라도 표시 순서(`ordered_rows`)가 같으면 같은 문서다 — 지문이
    조회 순서에 흔들리면 재시도마다 새 회차가 생기고 Canvas 가 늘어난다.
    """
    first = _row(id="a")
    second = _row(id="b", kind="new_issue", proposed_text="쟁점", evidence_quote="쟁점")
    assert sr.content_hash([], [first, second]) == sr.content_hash([], [second, first])
    # 내용이 달라지면 다른 문서다.
    assert sr.content_hash([], [first]) != sr.content_hash(["기존"], [first])
    assert sr.content_hash([], [first]) != sr.content_hash([], [first, second])
    # 렌더러 버전이 지문에 들어간다 — 규칙이 바뀌면 같은 후보라도 다른 문서다.
    assert str(sr.RENDERER_VERSION) in json.dumps(
        {"renderer": sr.RENDERER_VERSION}, ensure_ascii=False
    )


# --- Canvas 본문 (§2.1, §10) ---------------------------------------------------
def test_canvas_marks_the_projection_as_not_yet_approved():
    body = sr.canvas_markdown(
        channel_label="#전산팀장보고", review_date=date(2026, 9, 16),
        rows=[_row()], approved=["기존 문장"],
    )
    assert "오늘 수집 내용 요약 (검토 전)" in body
    assert "아직 승인된" in body


def test_canvas_has_a_channel_source_link():
    body = sr.canvas_markdown(
        channel_label="#ch", channel_id="C123", review_date=date(2026, 9, 16),
        rows=[_row()], approved=[],
    )
    assert "https://slack.com/archives/C123" in body
    assert "출처 링크" in body


def test_canvas_numbers_come_from_the_candidate_not_from_a_model():
    """§10 · 후보·근거에 없는 값이 표에 생기지 않는다."""
    body = sr.canvas_markdown(
        channel_label="#ch", review_date=date(2026, 9, 16),
        rows=[_row()], approved=[],
    )
    assert "62.5%" in body
    # 환산하거나 반올림한 값이 새로 생기면 안 된다.
    assert "63%" not in body and "62%" not in body


def test_canvas_does_not_carry_file_paths_or_dumps():
    body = sr.canvas_markdown(
        channel_label="#ch", review_date=date(2026, 9, 16),
        rows=[_row(evidence_quote="<root><cell>62.5</cell></root>")], approved=[],
    )
    # 표 구분자를 깨뜨리는 문자는 빠지지만 **내용을 고치지는 않는다.**
    assert "/var/lib/tybot" not in body
    assert "\n" not in sr._cell("<root>\n<cell>")


def test_canvas_title_carries_the_round_id():
    title = sr.canvas_title(
        channel_label="전산팀장보고", review_date=date(2026, 9, 16),
        artifact_id="a1b2c3d4-0000-0000-0000-000000000000",
    )
    assert title == "2026-09-16 전산팀장보고 요약 검토 [a1b2c3d4]"


def test_canvas_body_goes_through_the_generated_document_markers():
    """회차 Canvas 도 **우리가 만든 문서**다 — 다시 수집되면 원칙 1 이 깨진다."""
    from tybot.archive.canvas import is_generated_canvas
    from tybot.canvas_answer import TITLE_SUFFIX, markdown

    title = sr.canvas_title(
        channel_label="전산팀장보고", review_date=date(2026, 9, 16), artifact_id="a1b2c3d4",
    ) + TITLE_SUFFIX
    rendered = markdown(sr.canvas_markdown(
        channel_label="#ch", review_date=date(2026, 9, 16), rows=[_row()], approved=[],
    ))
    assert is_generated_canvas(title) is True
    assert is_generated_canvas("사람이 고친 제목", "", rendered) is True


# --- 버튼과 모달 좌표 ----------------------------------------------------------
def test_buttons_carry_both_the_round_and_the_candidate():
    """후보 ID 만 실으면 **어느 회차의 권한인지** 볼 수 없다."""
    blocks = sr.canvas_review_blocks(
        channel_label="#ch", review_date=date(2026, 9, 16),
        permalink="https://slack/canvas", artifact_id="A1", rows=[_row(id="C1")],
    )
    actions = [b for b in blocks if b["type"] == "actions"][-1]
    value = actions["elements"][0]["value"]
    assert sr.parse_button_value(value) == ("A1", "C1")


def test_old_bare_candidate_values_still_work():
    """이미 보낸 DM 과 폴백 화면은 후보 ID 문자열을 쓴다."""
    assert sr.parse_button_value("C1") == ("", "C1")
    # 깨진 JSON 은 후보 ID 가 아니다. **빈 값으로 두고 거절한다** — 그 문자열을
    # 후보 ID 로 넘기면 무엇을 결정하려던 것인지 모르는 채로 진행된다.
    assert sr.parse_button_value("{broken") == ("", "")


def test_reject_modal_metadata_is_json_not_a_joined_string():
    view = {"private_metadata": sr.reject_modal(
        "C1", artifact_id="A1", position=3, headline="*3. 숫자·일정*"
    )["private_metadata"]}
    assert json.loads(view["private_metadata"])["artifact_id"] == "A1"
    assert sr.reject_metadata(view) == {
        "artifact_id": "A1", "candidate_id": "C1", "position": 3
    }


def test_reject_modal_shows_which_candidate_is_being_rejected():
    blocks = sr.reject_modal("C1", artifact_id="A1", position=3,
                             headline="*3. 숫자·일정* · `62.5%`")["blocks"]
    assert "62.5%" in blocks[0]["text"]["text"]


def test_wrong_part_must_be_one_of_the_known_codes():
    def view(code):
        return {"state": {"values": {"wrong_part": {"wrong_part": {
            "selected_option": {"value": code}}}}}}

    assert sr.wrong_part_from_view(view("number")) == "number"
    assert sr.wrong_part_from_view(view("made-up")) == ""
    assert sr.wrong_part_from_view({}) == ""


# --- DM 상태 갱신 (§8) ---------------------------------------------------------
def test_decided_candidates_lose_their_buttons_and_show_who_decided():
    rows = [_row(id="C1", state="approved", decided_by="U123")]
    blocks = sr.canvas_review_blocks(
        channel_label="#ch", review_date=date(2026, 9, 16),
        permalink="https://slack/canvas", artifact_id="A1", rows=rows,
    )
    updated = sr._with_decisions(blocks, rows)
    assert not [b for b in updated if b.get("block_id") == "decide_1"
                and b["type"] == "actions"]
    rendered = json.dumps(updated, ensure_ascii=False)
    assert "맞음" in rendered and "U123" in rendered


def test_other_reviewers_never_see_the_correction_text():
    """정정은 그 사람의 판단이다. 퍼뜨리면 다음 판단이 그 문장에 끌린다(§8)."""
    rows = [_row(id="C1", state="rejected", decided_by="U456",
                 correction="공정률은 사실 61.0% 입니다")]
    blocks = sr.canvas_review_blocks(
        channel_label="#ch", review_date=date(2026, 9, 16),
        permalink="https://slack/canvas", artifact_id="A1", rows=rows,
    )
    rendered = json.dumps(sr._with_decisions(blocks, rows), ensure_ascii=False)
    assert "61.0" not in rendered
    assert "수정 필요" in rendered


# --- Store: 재실행·부분 결정 ---------------------------------------------------
class FakeCursor:
    """실행된 SQL 과 파라미터를 남기고 대본대로 답하는 커서."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.script.pop(0) if self.script else None

    def fetchall(self):
        return self.script.pop(0) if self.script else []


class FakeConn:
    def __init__(self, script: list):
        self.cur = FakeCursor(script)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_a_rerun_reuses_the_existing_round_instead_of_making_another():
    """§10 · 같은 워크스페이스·채널·날짜·digest 재실행에 Canvas 는 하나뿐이다."""
    existing = {"id": "A1", "state": "ready"}
    conn = FakeConn([None, existing])   # INSERT ... DO NOTHING → 없음, 그 뒤 SELECT
    store = sr.Store(conn)

    artifact_id, state = store.begin_artifact(
        workspace="ws", channel_id="C1", channel_name="#ch",
        review_date=date(2026, 9, 16), source_digest="d1", digest="h1", rows=[_row()],
    )

    assert (artifact_id, state) == ("A1", "ready")
    inserts = [sql for sql, _ in conn.cur.calls if sql.startswith("INSERT")]
    # 후보 매핑을 **다시 쓰지 않는다** — 번호가 바뀌면 "3번" 이 다른 후보가 된다.
    assert not [sql for sql in inserts if "artifact_candidate" in sql]


def test_a_new_round_pins_the_candidate_numbers():
    conn = FakeConn([{"id": "A1"}])
    store = sr.Store(conn)
    rows = [
        _row(id="a", kind="new_issue", proposed_text="쟁점", evidence_quote="쟁점"),
        _row(id="b"),
    ]

    artifact_id, state = store.begin_artifact(
        workspace="ws", channel_id="C1", channel_name="#ch",
        review_date=date(2026, 9, 16), source_digest="d1", digest="h1", rows=rows,
    )

    assert artifact_id and state == "new"
    mapped = [params for sql, params in conn.cur.calls if "artifact_candidate" in sql]
    # 숫자 후보가 1번이다. 조회 순서가 아니라 **표시 순서**를 고정한다.
    assert [(p[1], p[2]) for p in mapped] == [("b", 1), ("a", 2)]


def test_a_non_recipient_cannot_decide_anything():
    """§10 · 검토자가 아닌 사용자가 payload 를 재현해도 상태가 바뀌지 않는다."""
    conn = FakeConn([None])   # delivery 조회 결과 없음
    assert sr.Store(conn).may_decide("11111111-1111-1111-1111-111111111111",
                                     "U-stranger", "ws") is False
    assert not [sql for sql, _ in conn.cur.calls if sql.startswith("UPDATE")]


def test_a_bad_round_id_is_refused_without_touching_the_database():
    conn = FakeConn([])
    assert sr.Store(conn).may_decide("not-a-uuid", "U1", "ws") is False
    assert conn.cur.calls == []


def test_decide_checks_the_round_when_one_is_given():
    """회차가 있으면 **그 회차를 받았는지**로 본다(설계 §4.3)."""
    conn = FakeConn([None])
    changed = sr.Store(conn).decide(
        "11111111-1111-1111-1111-111111111111", workspace="ws", actor="U1",
        decision="approved", artifact_id="22222222-2222-2222-2222-222222222222",
    )
    assert changed is False
    sql, params = conn.cur.calls[0]
    assert "summary_review_delivery" in sql
    assert "review_digest_sent" in sql   # 폴백 경로도 같은 문장에 남아 있다
    assert params[-1] == "U1"


def test_approve_all_skips_what_someone_else_already_decided():
    """§10 · 이미 반려된 후보를 승인으로 덮어쓰지 않는다."""
    rows = [
        _row(id="11111111-1111-1111-1111-111111111111", state="pending"),
        _row(id="22222222-2222-2222-2222-222222222222", state="rejected"),
    ]
    calls: list[str] = []

    class Store(sr.Store):
        def artifact_rows(self, artifact_id):
            return rows

        def decide(self, candidate_id, **kw):
            calls.append(candidate_id)
            return True

    approved_ids, skipped = Store(FakeConn([])).approve_all(
        "A1", workspace="ws", actor="U1"
    )

    assert (approved_ids, skipped) == (["11111111-1111-1111-1111-111111111111"], 1)
    assert calls == ["11111111-1111-1111-1111-111111111111"]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["pending", "pending"], "ready"),
        (["approved", "pending"], "partial"),
        (["approved", "rejected"], "completed"),
        (["expired", "expired"], "expired"),
        (["approved", "expired"], "completed"),
        # 보류는 결정이 아니다. 하나라도 있으면 완료가 아니다(§7).
        (["approved", "deferred"], "partial"),
    ],
)
def test_round_state_is_computed_from_its_candidates(states, expected):
    total = len(states)
    decided = sum(s in ("approved", "rejected") for s in states)
    deferred = sum(s == "deferred" for s in states)
    expired = sum(s == "expired" for s in states)
    conn = FakeConn([{
        "total": total, "decided": decided, "deferred": deferred,
        "expired": expired,
    }])

    assert sr.Store(conn).refresh_artifact_state(
        "11111111-1111-1111-1111-111111111111"
    ) == expected


def test_unanswered_candidates_expire_without_becoming_approved():
    conn = FakeConn([
        [{"id": "C1"}],  # UPDATE ... RETURNING
        [],                # 연결된 열린 Artifact 없음
    ])

    count = sr.Store(conn).expire_unconfirmed("ws", "C1", date(2026, 9, 17))

    assert count == 1
    sql = conn.cur.calls[0][0]
    assert "state='expired'" in sql
    assert "run_date<%s" in sql
    assert "approved_summary_item" not in sql


def test_expired_candidate_is_shown_as_discarded_not_approved():
    rows = [_row(state="expired", decided_by="system:expired")]
    blocks = sr.canvas_review_blocks(
        channel_label="#ch", review_date=date(2026, 9, 16),
        permalink="https://slack/canvas", artifact_id="A1", rows=rows,
    )
    rendered = json.dumps(sr._with_decisions(blocks, rows), ensure_ascii=False)
    assert "미응답 폐기" in rendered
    assert "맞음" not in rendered


def test_recipients_are_reviewers_only():
    """§6 · 채널 담당자·개설자를 자동 수신자로 넣지 않는다."""
    conn = FakeConn([[{"reviewer_user": "U1"}, {"reviewer_user": "U1"}, {"reviewer_user": ""}]])

    assert sr.Store(conn).reviewer_recipients("ws", "C1") == ["U1"]
    sql, _ = conn.cur.calls[0]
    assert "channel_reviewer" in sql and "enabled" in sql
    assert "channel_owner" not in sql


# --- Canvas 생성 순서와 실패 (§5) ----------------------------------------------
def test_an_unclear_failure_becomes_ambiguous_not_failed():
    """만들어졌는지 모르면 **다시 만들지 않는다.** 중복보다 지연을 택한다."""
    assert sr._canvas_failure(TimeoutError("read timeout"))[1] == "ambiguous"
    assert sr._canvas_failure(RuntimeError("missing_scope")) == ("missing_scope", "failed")


class FakeArtifactStore:
    def __init__(self, state, artifact):
        self.state = state
        self.saved = artifact
        self.marked = []

    def approved(self, *_args):
        return []

    def begin_artifact(self, **_kwargs):
        return "A1", self.state

    def artifact(self, _artifact_id):
        return self.saved

    def mark_artifact(self, artifact_id, state, **kwargs):
        self.marked.append((artifact_id, state, kwargs))


def test_an_interrupted_creating_round_is_not_created_again(monkeypatch):
    """기존 `creating`은 API 전후 어디서 끊겼는지 몰라 중복 생성하면 안 된다."""
    created = Mock()
    monkeypatch.setattr("tybot.canvas_answer.create", created)
    store = FakeArtifactStore("creating", {})
    result = sr.RunResult()

    got = sr._canvas_round(
        store, Mock(), workspace="ws", channel_id="C1", channel_label="#ch",
        rows=[_row(source_digest="d1")], review_date=date(2026, 9, 16),
        recipients=["U1"], result=result,
    )

    assert got is None and not created.called
    assert store.marked[-1][1:] == (
        "ambiguous", {"error_code": "canvas-create-interrupted"}
    )


def test_access_failure_reuses_the_existing_canvas(monkeypatch):
    """권한 실패는 새 Canvas 생성이 아니라 기존 Canvas 권한 재부여로 복구한다."""
    created = Mock()
    granted = Mock()
    monkeypatch.setattr("tybot.canvas_answer.create", created)
    monkeypatch.setattr("tybot.canvas_answer.grant_users", granted)
    store = FakeArtifactStore("failed", {
        "canvas_id": "FC1", "canvas_permalink": "https://slack/canvas/FC1",
    })

    got = sr._canvas_round(
        store, Mock(), workspace="ws", channel_id="C1", channel_label="#ch",
        rows=[_row(source_digest="d1")], review_date=date(2026, 9, 16),
        recipients=["U1", "U2"], result=sr.RunResult(),
    )

    assert got["canvas_id"] == "FC1" and not created.called
    granted.assert_called_once_with(ANY, "FC1", ["U1", "U2"])
    assert store.marked[-1][1] == "ready"


# --- 배포 (§10) ----------------------------------------------------------------
def test_the_new_schema_is_discoverable_by_the_drift_checker():
    """`CREATE TABLE IF NOT EXISTS` 여야 드리프트 검사가 표를 **선언으로** 읽는다.

    검사기는 SQL 파일을 파싱해서 기대 목록을 만든다. `IF NOT EXISTS` 가 없으면
    그 표는 목록에 안 들어가고, 배포에서 빠져도 아무도 모른다.
    """
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path("scripts").resolve()))
    from check_schema_drift import declared

    _, tables = declared()
    for name in ("summary_review_artifact", "summary_review_artifact_candidate",
                 "summary_review_delivery"):
        assert tables.get(name) == "summary_review_canvas_schema.sql"


def test_the_new_schema_grants_access_in_the_file_itself():
    """수동 GRANT 절차를 만들지 않는다 — 사람이 옮겨 적는 단계는 한 번은 빠진다."""
    import pathlib

    sql = pathlib.Path("deploy/sql/summary_review_canvas_schema.sql").read_text(
        encoding="utf-8"
    )
    assert "GRANT SELECT, INSERT, UPDATE" in sql
    for name in ("summary_review_artifact", "summary_review_artifact_candidate",
                 "summary_review_delivery"):
        assert name in sql
    # Canvas 본문을 DB 에 복제하지 않는다(§4.1).
    assert "body" not in sql and "markdown" not in sql
