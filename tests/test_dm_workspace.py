"""봇과의 DM — 개인 작업공간 (B-57).

설계: `docs/design/dm-workspace.md`

여기서 고정하는 것은 셋이다 — **본인만 열린다**, **봇 발언은 안 쌓인다**,
**방금 올린 파일이 이번 답변의 근거가 된다.**
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tybot.access import RequestContext, can_access
from tybot.archive import writer
from tybot.archive.store import ArchiveStore

MINE = "U1"
YOURS = "U2"


def _dm(root, *, user: str = MINE, text: str = "개인 메모", source_ts: str = "1758012345.123456"):
    return writer.ingest(
        root,
        workspace="tyit",
        channel=writer.dm_channel(user),
        channel_id=f"D_{user}",
        dm_user=user,
        acl=[],
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
            speaker="사람",
            text=text,
            source_ts=source_ts,
        )],
    )


def _channel_doc(root):
    return writer.ingest(
        root,
        workspace="tyit",
        channel="#팀-전산_ABB110-회의",
        channel_id="C1",
        acl=["#팀-전산_ABB110-회의"],
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
            speaker="사람",
            text="채널 원문",
        )],
    )


def _ctx(**kw) -> RequestContext:
    kw.setdefault("workspace", "tyit")
    return RequestContext(**kw)


# --- 저장 위치와 모양 ----------------------------------------------------------
def test_dm_is_stored_outside_the_channel_tree(tmp_path):
    """경로가 갈려야 채널을 훑는 기존 소비자가 개인 기록을 보지 않는다."""
    result = _dm(tmp_path)

    assert result.path.relative_to(tmp_path).as_posix() == (
        "workspaces/tyit/dm/U1/raw/2026-09-18.md"
    )


def test_channel_scans_do_not_see_dm_by_default(tmp_path):
    _dm(tmp_path)
    _channel_doc(tmp_path)
    store = ArchiveStore(tmp_path)

    assert [d.dm_user for d in store.docs()] == [None]
    assert len(store.docs(dm_scope="*")) == 2


def test_only_that_persons_dm_files_are_even_opened(tmp_path):
    """남의 DM 을 「읽고 나서 거르는」 구조를 만들지 않는다."""
    _dm(tmp_path, user=MINE)
    _dm(tmp_path, user=YOURS)
    store = ArchiveStore(tmp_path)

    assert [d.dm_user for d in store.docs(dm_scope=MINE)] == [MINE]


def test_merging_keeps_the_dm_owner(tmp_path):
    """`dm_user` 를 떨어뜨리면 개인 기록이 조직 자료가 된다 — 실제로 그랬다."""
    _dm(tmp_path, text="첫 줄")
    _dm(tmp_path, text="둘째 줄", source_ts="1758012346.123456")
    store = ArchiveStore(tmp_path)

    assert all(d.dm_user == MINE for d in store.docs(dm_scope=MINE))


# --- 권한 (원칙 3) -------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "ctx", "allowed"),
    [
        ("본인이 DM 에서", _ctx(user_id=MINE), True),
        ("다른 사람", _ctx(user_id=YOURS), False),
        ("사용자 미상(콘솔·배치)", _ctx(), False),
        ("exec 통합조회", _ctx(role="exec", user_id="U9"), False),
        ("root 워크스페이스", _ctx(is_root=True, user_id="U9"), False),
        ("다른 워크스페이스", RequestContext(workspace="mgmt", user_id=MINE), False),
        (
            "본인이지만 채널에서 물음",
            _ctx(user_id=MINE, channel_id="C1", channel="#팀-전산_ABB110-회의"),
            False,
        ),
    ],
)
def test_dm_opens_only_for_its_owner_asking_in_dm(name, ctx, allowed):
    """통합조회 권한은 **조직의 기록**을 보는 권한이지 개인 공간의 열쇠가 아니다."""
    assert can_access(
        ctx,
        visibility="private",
        acl=None,
        owner_workspace="tyit",
        channel_id="D_U1",
        channel=writer.dm_channel(MINE),
        dm_user=MINE,
    ) is allowed, name


def test_the_archive_agrees_with_the_access_rule(tmp_path):
    """규칙만 맞고 실제 조회가 다르면 아무 소용이 없다."""
    _dm(tmp_path, user=MINE)
    _dm(tmp_path, user=YOURS)
    store = ArchiveStore(tmp_path)

    assert [d.dm_user for d in store.visible_docs(_ctx(user_id=MINE))] == [MINE]
    assert store.visible_docs(_ctx(role="exec", user_id="U9")) == []
    assert store.visible_docs(_ctx()) == []


def test_a_channel_question_never_surfaces_dm_material(tmp_path):
    """답을 그 채널 사람들이 함께 본다. 본인이 물었어도 개인 자료는 안 나간다."""
    _dm(tmp_path, user=MINE)
    store = ArchiveStore(tmp_path)

    ctx = _ctx(user_id=MINE, channel_id="C1", channel="#팀-전산_ABB110-회의")

    assert store.visible_docs(ctx) == []


# --- 원문에 무엇이 들어가나 (원칙 1·5) -----------------------------------------
def test_bot_output_is_not_archived_in_dm(tmp_path):
    """DM 이라고 봇 발언을 원문으로 쌓지 않는다. 쌓으면 자기 답을 사실로 인용한다."""
    result = writer.ingest(
        tmp_path,
        workspace="tyit",
        channel=writer.dm_channel(MINE),
        channel_id="D_U1",
        dm_user=MINE,
        acl=[],
        messages=[
            writer.IncomingMessage(
                ts=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
                speaker="사람", text="사람이 쓴 말",
            ),
            writer.IncomingMessage(
                ts=datetime(2026, 9, 18, 1, 1, tzinfo=UTC),
                speaker="TYBot", text="봇이 만든 문장", is_bot=True,
            ),
        ],
    )

    body = result.path.read_text(encoding="utf-8")
    assert "사람이 쓴 말" in body
    assert "봇이 만든 문장" not in body
    assert result.skipped_bot == 1


def test_pii_screening_still_applies_in_dm(tmp_path):
    """개인이 자기 파일을 올리는 자리라 오히려 걸릴 확률이 높다(원칙 5)."""
    result = _dm(tmp_path, text="주민등록번호 880101-1234567 입니다")

    assert result.written == 0
    assert result.refused


def test_dm_frontmatter_has_no_channel_acl(tmp_path):
    """채널 멤버십으로 열리면 안 된다. 여는 열쇠는 `dm_user` 하나뿐이다."""
    body = _dm(tmp_path).path.read_text(encoding="utf-8")

    assert "dm_user: U1" in body
    assert "acl: []" in body
    assert "org_name" not in body


# --- 출처 (원칙 2) -------------------------------------------------------------
def test_citation_does_not_leak_the_internal_dm_key(tmp_path):
    _dm(tmp_path)
    store = ArchiveStore(tmp_path)
    doc = store.docs(dm_scope=MINE)[0]
    from tybot.archive.store import SearchHit

    got = SearchHit(doc=doc, line=doc.raw_lines[0], score=1).citation()

    assert got.startswith("[DM]나와의 대화")
    assert "DM:U1" not in got


# --- 배선: 수집이 먼저, 그다음 답변 -------------------------------------------
def _bare_bot():
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.realtime = True
    return bot


def test_dm_is_collected_before_the_answer_is_made(monkeypatch):
    """순서가 뒤집히면 질문에 붙인 파일은 이번 답변의 근거가 될 수 없다."""
    bot = _bare_bot()
    order: list[str] = []
    monkeypatch.setattr(type(bot), "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(type(bot), "_handle_correction", lambda self, e, s: False)
    monkeypatch.setattr(
        type(bot), "_ingest_dm", lambda self, c, e: order.append("collect") or ["staged"]
    )
    monkeypatch.setattr(
        type(bot), "_handle", lambda self, e, c, s, *, in_channel: order.append("answer")
    )

    event = {"channel_type": "im", "channel": "D1", "user": MINE, "ts": "1.0"}
    assert bot.route_message(event, None, None) == "answered"

    assert order == ["collect", "answer"]
    assert event["_tybot_dm_attachments"] == ["staged"]


def test_a_channel_message_is_not_collected_as_a_dm(monkeypatch):
    """사람과 사람의 DM 도, 채널 메시지도 개인 작업공간에 들어가지 않는다."""
    bot = _bare_bot()
    called: list[str] = []
    monkeypatch.setattr(type(bot), "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(type(bot), "_ingest_dm", lambda self, c, e: called.append("dm"))
    monkeypatch.setattr(type(bot), "_ingest_live", lambda self, c, e: called.append("live"))

    bot.route_message({"channel_type": "channel", "channel": "C1"}, None, None)

    assert called == ["live"]


def test_the_file_just_uploaded_is_carried_into_this_answer(tmp_path):
    """검색어가 파일 내용과 어긋나도 빠지면 안 되는 근거다(문제 패킷 4d98bb66)."""
    _dm(tmp_path, text="[첨부추출:목록.xlsx] 표에 적힌 항목", source_ts="1758012345.123456")
    bot = _bare_bot()
    bot.store = ArchiveStore(tmp_path)

    hits = bot._uploaded_now(
        _ctx(user_id=MINE),
        {"ts": "1758012345.123456", "_tybot_dm_attachments": ["staged"]},
    )

    assert [h.line.text for h in hits] == ["[첨부추출:목록.xlsx] 표에 적힌 항목"]


def test_another_persons_upload_is_not_carried_in(tmp_path):
    """좌표가 같아도 권한은 `visible_docs` 가 쥔다."""
    _dm(tmp_path, user=YOURS, text="남의 첨부", source_ts="1758012345.123456")
    bot = _bare_bot()
    bot.store = ArchiveStore(tmp_path)

    hits = bot._uploaded_now(
        _ctx(user_id=MINE),
        {"ts": "1758012345.123456", "_tybot_dm_attachments": ["staged"]},
    )

    assert hits == []


# --- 피드백 다음에 실제로 뭔가 일어난다 (QA e8463a7b) --------------------------
class _FeedbackLog:
    def __init__(self):
        self.rows: list[dict] = []

    def write(self, **kw):
        self.rows.append(kw)


class _QALog:
    def __init__(self, row):
        self.row = row

    def find_answer(self, workspace, channel_id, *, thread_ts="", response_ts=""):
        return self.row


def _correction_bot(monkeypatch, row):
    bot = _bare_bot()
    bot.feedback_log = _FeedbackLog()
    bot.qa_log = _QALog(row)
    bot.app = type("A", (), {"client": None})()
    handled: list[dict] = []
    monkeypatch.setattr(
        type(bot), "_handle",
        lambda self, e, c, s, *, in_channel: handled.append(e),
    )
    return bot, handled


def test_a_correction_that_tells_us_what_to_do_is_run_again(monkeypatch):
    """접수만 하고 끝내면 알려 준 사람은 그 자리에서 아무것도 얻지 못한다."""
    bot, handled = _correction_bot(monkeypatch, {"record_id": "r1", "reason": "no_hits"})
    said: list[str] = []

    done = bot._handle_correction(
        {"text": "정정: 부대관리비 규정 쪽으로 다시 찾아줘", "user": MINE,
         "channel": "D1", "ts": "2.0", "thread_ts": "1.0"},
        lambda **kw: said.append(kw["text"]),
    )

    assert done
    assert bot.feedback_log.rows[0]["kind"] == "correction"
    assert [e["text"] for e in handled] == ["부대관리비 규정 쪽으로 다시 찾아줘"]
    assert "다시 찾아보겠습니다" in said[0]


def test_a_plain_complaint_is_recorded_with_the_reason_it_failed(monkeypatch):
    """무엇이 달라지는지 말해 주지 않으면 「반영하겠습니다」 는 빈 약속이다."""
    bot, handled = _correction_bot(monkeypatch, {"record_id": "r1", "reason": "no_access"})
    said: list[str] = []

    bot._handle_correction(
        {"text": "정정: 이거 틀렸음", "user": MINE, "channel": "D1",
         "ts": "2.0", "thread_ts": "1.0"},
        lambda **kw: said.append(kw["text"]),
    )

    assert handled == []
    assert "invite" in said[0]


def test_the_correction_text_is_never_stored_as_evidence(monkeypatch):
    """사람이 쓴 정정은 원문이 아니다(원칙 7). 피드백 로그에만 남는다."""
    bot, _ = _correction_bot(monkeypatch, {"record_id": "r1", "reason": ""})

    bot._handle_correction(
        {"text": "정정: 금액은 12억이 맞다", "user": MINE, "channel": "D1",
         "ts": "2.0", "thread_ts": "1.0"},
        lambda **kw: None,
    )

    (row,) = bot.feedback_log.rows
    assert row["text"] == "금액은 12억이 맞다"
    assert row["qa_record_id"] == "r1"
