"""첨부 정본 reader — **무엇이 근거가 되고 무엇이 안 되나.**

결정: 2026-09-26 오너 지시(첨부 정본 reader) · 분리 설계 §2·§3.

정본 경로는 원문 글롭 밖이라, reader 가 없으면 파일이 있어도 검색이 못 본다 —
오류 없이, 그냥 없는 자료가 된다. 그리고 붙이고 나면 반대 위험이 생긴다:
절반만 읽힌 변환본을 전체로 읽거나, 같은 첨부를 두 번 세거나.

DB 를 요구하지 않는다. 파일을 실제로 써서 **무엇이 나오는지** 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tybot.access import RequestContext
from tybot.archive import attachment_doc
from tybot.archive import attachment_reader as reader
from tybot.archive.store import ArchiveStore

WS, CH, CHANNEL = "tyit", "C0FUND", "#팀_자금(ABB540)_주간보고"


def _doc(**over) -> attachment_doc.AttachmentDoc:
    base = {
        "workspace": WS, "channel_id": CH, "channel": CHANNEL,
        "file_id": "F1", "name": "기성내역.xlsx", "revision": "aaaa11112222",
        "visibility": "private", "acl": frozenset({CHANNEL}),
        "text": "9월 기성 청구액은 15억입니다", "conversion_state": attachment_doc.CONVERTED,
        "message_ts": "1790070000.000001",
        "permalink": "https://slack.example/archives/C0FUND/p1790070000000001",
        "filetype": "xlsx", "sha256": "a" * 64, "staged_at": "2026-09-22T10:00:00+00:00",
    }
    return attachment_doc.AttachmentDoc(**(base | over))


def _write(root: Path, doc: attachment_doc.AttachmentDoc) -> Path:
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")
    return path


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "archive"


# --- 1. 경로 계약 --------------------------------------------------------------

def test_the_canonical_path_is_attachments_fileid_revision(archive):
    path = _write(archive, _doc())

    parts = path.relative_to(archive).parts

    assert parts[-3] == "attachments"
    assert parts[-2] == "F1"
    assert parts[-1] == "aaaa11112222.md"


def test_the_reader_finds_what_the_writer_wrote(archive):
    """글롭이 `AttachmentDoc.relative_path()` 와 어긋나면 **한 장도 안 읽힌다.**"""
    _write(archive, _doc())

    assert len(reader.source_files(archive)) == 1


def test_an_empty_archive_is_not_an_error(archive):
    assert reader.source_files(archive) == []


# --- 2. 최신 성공 변환본만 -----------------------------------------------------

def test_only_the_latest_revision_is_evidence(archive):
    """옛 판을 지우지 않는 이유는 되짚기 위해서지 근거로 쓰기 위해서가 아니다."""
    _write(archive, _doc(revision="old000000000", text="8월 기성 12억",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="9월 기성 15억",
                         staged_at="2026-09-22T10:00:00+00:00"))

    got = reader.evidence(archive, [])

    assert [doc.revision for doc in got] == ["new000000000"]
    assert "8월" not in got[0].text


def test_a_revision_without_a_timestamp_does_not_displace_a_known_one(archive):
    """모르는 것으로 아는 것을 덮으면 최신이 옛것으로 밀린다."""
    _write(archive, _doc(revision="known0000000", staged_at="2026-09-22T10:00:00+00:00"))
    _write(archive, _doc(revision="unknown00000", staged_at="", text="정체불명"))

    got = reader.evidence(archive, [])

    assert [doc.revision for doc in got] == ["known0000000"]


def test_two_different_files_both_survive(archive):
    _write(archive, _doc(file_id="F1"))
    _write(archive, _doc(file_id="F2", name="예산안.docx", text="예산 20억"))

    assert {doc.file_id for doc in reader.evidence(archive, [])} == {"F1", "F2"}


# --- 3. 실패·부분·대기는 근거가 아니다 -----------------------------------------

@pytest.mark.parametrize(
    "state",
    [
        attachment_doc.PARTIAL,
        attachment_doc.FAILED,
        attachment_doc.BLOCKED,
        attachment_doc.UNSUPPORTED,
        attachment_doc.PENDING,
    ],
)
def test_non_succeeded_states_are_not_general_evidence(archive, state):
    """`partial` 이 여기 있는 것이 요점이다.

    「본문이 나왔다」 와 「다 읽었다」 는 다르다. 절반만 읽힌 변환본을 근거로 쓰면
    뒤쪽에 있던 내용을 **「없다」 고 답하게 된다.**
    """
    _write(archive, _doc(conversion_state=state))

    assert reader.evidence(archive, []) == []


def test_partial_differs_from_the_document_level_usable_flag():
    """`attachment_doc.USABLE` 과 일부러 다르다.

    그쪽은 「본문이 있나」(화면이 본다), 여기는 「근거로 써도 되나」 다. 한 이름으로
    합치면 화면이 부분 변환본을 못 보거나, 답변이 그걸 전체로 읽는다.
    """
    assert attachment_doc.PARTIAL in attachment_doc.USABLE
    assert attachment_doc.PARTIAL not in reader.GENERAL_EVIDENCE


def test_a_succeeded_document_with_no_body_is_excluded(archive):
    """상태만 맞고 본문이 비면 근거가 아니다."""
    _write(archive, _doc(text="   "))

    assert reader.evidence(archive, []) == []


# --- 4. 과거 revision 은 감사에서만 ---------------------------------------------

def test_audit_sees_every_revision_and_state(archive):
    _write(archive, _doc(revision="old000000000", staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", staged_at="2026-09-22T10:00:00+00:00"))
    _write(archive, _doc(file_id="F9", revision="bad000000000",
                         conversion_state=attachment_doc.FAILED, text=""))

    audited = reader.all_revisions(archive)

    assert len(audited) == 3
    assert {doc.revision for doc in audited} == {
        "old000000000", "new000000000", "bad000000000"
    }


def test_audit_and_evidence_are_separate_functions():
    """같은 함수에 플래그를 두면 호출부 하나가 부분 변환본을 근거로 만든다."""
    import inspect

    assert "include" not in str(inspect.signature(reader.all_revisions))
    assert "state" not in inspect.signature(reader.all_revisions).parameters


# --- 5. 같은 첨부를 두 번 세지 않는다 -------------------------------------------

class _Line:
    def __init__(self, text):
        self.text = text


class _ChannelDoc:
    def __init__(self, channel_id, lines):
        self.channel_id = channel_id
        self.raw_lines = [_Line(text) for text in lines]


def test_a_body_already_in_raw_is_not_counted_twice(archive):
    """답이 「두 군데서 확인됨」 처럼 보이는데 사실은 한 군데다."""
    _write(archive, _doc())
    channels = [_ChannelDoc(CH, ["[첨부추출:기성내역.xlsx] 9월 기성 청구액은 15억입니다"])]

    assert reader.evidence(archive, channels) == []


def test_the_bridge_only_covers_the_same_channel(archive):
    """다른 채널에 같은 이름이 있다고 이 채널 정본이 빠지면 안 된다."""
    _write(archive, _doc())
    channels = [_ChannelDoc("C_OTHER", ["[첨부추출:기성내역.xlsx] 다른 채널 본문"])]

    assert len(reader.evidence(archive, channels)) == 1


def test_the_bridge_lifts_itself_once_raw_stops_carrying_bodies(archive):
    """분리 스위치를 켜면 raw 에 본문이 없어지므로 아무것도 안 걸러진다."""
    _write(archive, _doc())
    channels = [_ChannelDoc(CH, ["📎 첨부: `기성내역.xlsx` · id:F1"])]

    assert len(reader.evidence(archive, channels)) == 1


# --- 6. 좌표와 상태를 보존한다 ---------------------------------------------------

def test_the_archive_doc_keeps_the_coordinates(archive):
    """요구 6 — 파일 ID·채널 ID·원문 링크·변환 상태."""
    doc = _doc()
    _write(archive, doc)

    (got,) = reader.archive_docs(archive, [])

    assert got.channel_id == CH
    assert got.workspace == WS
    assert "F1" in str(got.path)
    assert all(line.message_ts == "1790070000.000001" for line in got.raw_lines)


def test_the_file_name_becomes_the_speaker(archive):
    """출처에 무엇에서 나온 내용인지 남아야 한다."""
    _write(archive, _doc())

    (got,) = reader.archive_docs(archive, [])

    assert {line.speaker for line in got.raw_lines} == {"기성내역.xlsx"}


def test_the_frontmatter_round_trips(archive):
    """쓰는 쪽과 읽는 쪽이 갈리면 상태가 조용히 `unknown` 이 된다."""
    written = _doc(conversion_state=attachment_doc.PARTIAL)
    path = _write(archive, written)

    got = reader.load(path)

    assert got.file_id == written.file_id
    assert got.channel_id == written.channel_id
    assert got.permalink == written.permalink
    assert got.conversion_state == attachment_doc.PARTIAL
    assert got.acl == written.acl


def test_a_non_attachment_document_is_ignored(archive):
    """원문 문서가 이 경로에 섞여 들어와도 첨부로 읽지 않는다."""
    path = archive / "workspaces" / WS / "channels" / CH / "attachments" / "F1" / "x.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '---\nworkspace: tyit\nchannel: "#x"\nvisibility: private\nacl: \n---\n\n## 원문\n',
        encoding="utf-8",
    )

    assert reader.load(path) is None


def test_a_corrupt_document_does_not_break_the_rest(archive):
    _write(archive, _doc(file_id="F2", text="정상 본문"))
    bad = archive / "workspaces" / WS / "channels" / CH / "attachments" / "F3" / "b.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("프론트매터가 없다", encoding="utf-8")

    got = reader.evidence(archive, [])

    assert [doc.file_id for doc in got] == ["F2"]


# --- 7. reader 와 색인이 같은 판정을 쓴다 ---------------------------------------

def _channel_raw(root: Path) -> None:
    """같은 채널의 사람 발언. 첨부와 **섞이지 않아야** 한다."""
    from datetime import UTC, datetime

    from tybot.archive import writer

    writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CH,
        messages=[writer.IncomingMessage(
            datetime.now(UTC), "김자금", "사람이 말한 기성 이야기",
        )],
        acl=[CHANNEL],
    )


def test_the_store_returns_attachments_as_separate_documents(archive):
    """채널 문서에 합치면 사람 발언과 문서 본문이 한 줄기가 된다(원칙 7)."""
    _channel_raw(archive)
    _write(archive, _doc())

    docs = ArchiveStore(archive).docs()

    assert len(docs) == 2
    kinds = {("attachments" in str(doc.path)) for doc in docs}
    assert kinds == {True, False}


def test_search_finds_the_attachment_body(archive):
    """reader 가 없으면 이 자료는 오류 없이 **없는 것**이었다."""
    _channel_raw(archive)
    _write(archive, _doc())
    store = ArchiveStore(archive)
    ctx = RequestContext(workspace=WS, role="exec")

    hits = store.search("기성", ctx, limit=20)

    assert any("15억" in hit.line.text for hit in hits)


def test_the_store_and_the_search_path_agree(archive):
    """거르는 자리가 하나라 두 경로가 갈릴 수 없다."""
    _channel_raw(archive)
    _write(archive, _doc(revision="old000000000", text="옛 판 기성 12억",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="새 판 기성 15억",
                         staged_at="2026-09-22T10:00:00+00:00"))
    store = ArchiveStore(archive)
    ctx = RequestContext(workspace=WS, role="exec")

    direct = {
        line.text
        for doc in store.visible_docs(ctx)
        if "attachments" in str(doc.path)
        for line in doc.raw_lines
    }
    hits = {
        hit.line.text for hit in store.search("기성", ctx, limit=20)
        if "attachments" in str(hit.line.source_path or hit.doc.path)
    }

    assert direct == {"새 판 기성 15억"}
    assert hits == direct


def test_the_old_revision_is_not_searchable(archive):
    _channel_raw(archive)
    _write(archive, _doc(revision="old000000000", text="옛 판 12억",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="새 판 15억",
                         staged_at="2026-09-22T10:00:00+00:00"))

    hits = ArchiveStore(archive).search("12억", RequestContext(workspace=WS, role="exec"))

    assert hits == []


def test_the_audit_view_keeps_every_revision(archive):
    _channel_raw(archive)
    _write(archive, _doc(revision="old000000000", text="옛 판 12억",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="새 판 15억",
                         staged_at="2026-09-22T10:00:00+00:00"))

    texts = [
        line.text
        for doc in ArchiveStore(archive).audit_docs()
        if "attachments" in str(doc.path)
        for line in doc.raw_lines
    ]

    assert set(texts) == {"옛 판 12억", "새 판 15억"}


# --- 8. 권한 --------------------------------------------------------------------

def test_an_attachment_inherits_the_channel_acl(archive):
    """첨부는 그 채널에 올라온 것이므로 **채널보다 넓게 열릴 수 없다.**"""
    _write(archive, _doc())

    (got,) = reader.archive_docs(archive, [])

    assert got.acl == frozenset({CHANNEL})
    assert got.visibility == "private"


def test_a_private_attachment_is_hidden_from_an_outsider(archive):
    _write(archive, _doc())
    store = ArchiveStore(archive)

    outsider = RequestContext(workspace=WS, channels=frozenset({"C_OTHER"}))

    assert [d for d in store.visible_docs(outsider) if "attachments" in str(d.path)] == []


def test_a_member_sees_it(archive):
    _write(archive, _doc())
    store = ArchiveStore(archive)

    member = RequestContext(workspace=WS, channels=frozenset({CHANNEL}))

    assert [d for d in store.visible_docs(member) if "attachments" in str(d.path)]


# --- 재색인 dry-run -------------------------------------------------------------

def test_the_reindex_plan_keeps_only_the_current_revision(archive, monkeypatch):
    """스크립트가 판정을 다시 쓰지 않는다 — reader 가 고른 것만 넣는다."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import reindex_attachments

    _channel_raw(archive)
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="새 판",
                         staged_at="2026-09-22T10:00:00+00:00"))

    keep, stale = reindex_attachments.plan(ArchiveStore(archive))

    assert [str(doc.path).endswith("new000000000.md") for doc in keep] == [True]
    assert stale, "옛 판 좌표가 정리 목록에 있어야 한다"
    assert all("old000000000" in path for path, _, _ in stale)


def test_the_reindex_plan_is_empty_when_there_is_nothing_to_do(archive):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import reindex_attachments

    _channel_raw(archive)
    _write(archive, _doc())

    keep, stale = reindex_attachments.plan(ArchiveStore(archive))

    assert len(keep) == 1
    assert stale == []


def test_the_reindex_script_refuses_without_a_database(monkeypatch, capsys):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import reindex_attachments

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["reindex_attachments.py", "--dry-run"])
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda: "")

    assert reindex_attachments.main() == 2
    assert "DATABASE_URL" in capsys.readouterr().out
