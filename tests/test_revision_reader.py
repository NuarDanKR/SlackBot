"""수정·삭제된 메시지가 **일반 근거로 나가지 않는다.**

결정: 2026-09-25 오너 §3, 2026-09-26 후속 지시(revision reader).

수집기는 수정·삭제를 원문에 쌓는다(원칙 1 — 원문은 안 고친다). 그래서 한 메시지가
세 줄로 남는다 — 고치기 전 본문, `[수정 전]`, `[수정 후]`. reader 가 없으면 검색이
**셋을 다 집는다.** 「10시」 로 물으면 고치기 전 문장이 근거로 나오고, 지운
메시지도 `[삭제 전]` 줄로 나온다. 사람이 고치거나 지운 뜻이 뒤집힌다.

DB 를 요구하지 않는다. 조회 함수를 갈아 끼우고 **무엇이 남는지** 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive import revision_reader as reader
from tybot.archive.store import ArchiveDoc, RawLine

WS, CH = "tyit", "C0FUND"
TS = "1790070000.000001"


def _line(text: str, lineno: int, *, message_ts: str = TS) -> RawLine:
    return RawLine(
        ts="2026-09-22 18:40", speaker="U1", text=text, lineno=lineno,
        source_path=Path("raw/2026-09-22.md"), message_ts=message_ts,
    )


def _revision(kind: str, body: str = "") -> reader.LatestRevision:
    return reader.LatestRevision(kind, reader.body_digest(body))


def _visible(lines, latest) -> list[str]:
    got = reader.visible_lines(
        lines, workspace=WS, channel_id=CH, lookup=lambda *_: latest
    )
    return [line.text for line in got]


# --- 1. 수정 전/후 중 최신 본문만 ---------------------------------------------

EDITED = [
    _line("10시입니다", 1),
    _line("[수정 전] 10시입니다", 2),
    _line("[수정 후] 11시입니다", 3),
]


def test_only_the_latest_body_survives_an_edit():
    """고치기 전 본문은 **두 줄** 있다 — 원래 줄과 `[수정 전]` 줄. 둘 다 빠진다."""
    assert _visible(EDITED, _revision("change", "11시입니다")) == ["[수정 후] 11시입니다"]


def test_the_pre_edit_body_is_never_general_evidence():
    got = _visible(EDITED, _revision("change", "11시입니다"))

    assert not any("10시" in text for text in got)


# --- 2. 여러 번 고치면 마지막만 -----------------------------------------------

TWICE = [
    _line("10시입니다", 1),
    _line("[수정 전] 10시입니다", 2),
    _line("[수정 후] 11시입니다", 3),
    _line("[수정 전] 11시입니다", 4),
    _line("[수정 후] 12시입니다", 5),
]


def test_only_the_last_body_survives_two_edits():
    """`[수정 후]` 가 둘이다. **해시가 맞는 하나만** 남는다."""
    assert _visible(TWICE, _revision("change", "12시입니다")) == ["[수정 후] 12시입니다"]


def test_the_middle_body_is_excluded_too():
    got = _visible(TWICE, _revision("change", "12시입니다"))

    assert not any("11시" in text for text in got)
    assert len(got) == 1, "정확히 한 번만 나온다"


# --- 3. 삭제·redact 는 통째로 --------------------------------------------------

DELETED = [
    _line("10시입니다", 1),
    _line("[삭제 전] 10시입니다", 2),
    _line("[삭제됨] Slack에서 삭제된 메시지", 3),
]


@pytest.mark.parametrize("kind", ["delete", "redact"])
def test_a_removed_message_is_excluded_entirely(kind):
    """지운 것을 보여 주면 **지운 사람의 뜻을 뒤집는다.**"""
    assert _visible(DELETED, _revision(kind)) == []


def test_a_message_edited_then_deleted_leaves_nothing():
    """**변이 시험이 잡아낸 구멍.**

    고친 뒤 지우는 것은 흔한 순서다. 그때 원문에는 `[수정 후]` 줄이 남아 있고
    그 본문 해시가 최신 revision 과 맞는다. `removed` 판정이 없으면 해시 매칭이
    먼저 걸려서 **지운 문장이 그대로 근거로 나간다.**

    처음 시험은 삭제 케이스에 `[수정 후]` 줄이 없어서 이 경로를 안 밟았다.
    """
    lines = [
        _line("10시입니다", 1),
        _line("[수정 전] 10시입니다", 2),
        _line("[수정 후] 11시입니다", 3),
        _line("[삭제 전] 11시입니다", 4),
        _line("[삭제됨] Slack에서 삭제된 메시지", 5),
    ]

    # 최신은 delete 인데 본문 해시는 고친 뒤 본문 그대로다
    latest = reader.LatestRevision("delete", reader.body_digest("11시입니다"))

    assert _visible(lines, latest) == []


def test_a_redacted_message_leaves_nothing_even_with_a_body_line():
    """PII·법적 삭제는 본문 해시가 비어 있다. 그래도 전부 빠져야 한다."""
    lines = [*DELETED, _line("[수정 후] 11시입니다", 4)]

    assert _visible(lines, reader.LatestRevision("redact", "")) == []


# --- 4. 감사 조회는 전부 본다 ---------------------------------------------------

def test_audit_sees_every_revision():
    """일반 조회에서 감춘 것을 감사에서는 봐야 한다. 안 보이면 무엇이 바뀌었는지 모른다."""
    got = reader.audit_lines(TWICE)

    assert len(got) == len(TWICE)
    assert any("10시" in line.text for line in got)
    assert any("11시" in line.text for line in got)


def test_audit_and_general_are_separate_functions():
    """같은 함수에 플래그를 두면 호출부 하나가 기본값을 잘못 줘서 지워진 문장이 나간다."""
    import inspect

    assert "flag" not in inspect.signature(reader.audit_lines).parameters
    assert "include" not in str(inspect.signature(reader.audit_lines))


# --- 5. 다른 워크스페이스·채널이 섞이지 않는다 ---------------------------------

def test_the_lookup_receives_the_whole_coordinate():
    """Slack `ts` 는 채널을 가로질러 같은 값이 나온다.

    둘만 보면 **남의 채널 삭제 기록이 이 채널 줄을 감춘다.**
    """
    seen: list[tuple] = []

    reader.visible_lines(
        EDITED, workspace=WS, channel_id=CH,
        lookup=lambda *args: seen.append(args) or _revision("change", "11시입니다"),
    )

    assert seen == [(WS, CH, TS)]


def test_another_channels_deletion_does_not_hide_this_one():
    def lookup(workspace, channel_id, message_ts):
        # 다른 채널에서만 지워졌다
        if channel_id == "C_OTHER":
            return _revision("delete")
        return _revision("change", "11시입니다")

    got = reader.visible_lines(EDITED, workspace=WS, channel_id=CH, lookup=lookup)

    assert [line.text for line in got] == ["[수정 후] 11시입니다"]


def test_the_batch_query_is_scoped_to_the_workspace():
    """질의에 워크스페이스가 빠지면 다른 회사 자료의 삭제가 우리 줄을 감춘다."""
    source = Path(reader.__file__).read_text(encoding="utf-8")
    query = source[source.index("def latest_revisions"):source.index("def index_excluded")]

    assert "WHERE workspace = %s" in query
    assert "DISTINCT ON (channel_id, message_ts)" in query


# --- 6. DB 장애는 fail-closed ---------------------------------------------------

def test_an_unknown_revision_state_hides_the_whole_coordinate():
    """「지워졌는지 모른다」 와 「안 지워졌다」 는 다르다.

    모르는 채로 보여 주면 지운 메시지가 근거로 나가고, 그건 되돌릴 수 없다 —
    사람은 이미 그 내용을 봤다.
    """
    assert _visible(EDITED, reader.UNKNOWN) == []


def test_a_lookup_that_raises_is_treated_as_unknown():
    def boom(*_):
        raise RuntimeError("DB 다운")

    got = reader.visible_lines(EDITED, workspace=WS, channel_id=CH, lookup=boom)

    assert got == []


def test_a_missing_revision_row_with_markers_is_also_hidden():
    """표시줄이 있는데 기록이 없다는 것은 **둘이 어긋났다**는 뜻이다.

    어긋난 상태에서 보여 줄 쪽을 고를 근거가 없다.
    """
    assert _visible(EDITED, None) == []


def test_a_body_that_does_not_match_the_record_is_hidden():
    """파일과 DB 가 안 맞으면 무엇이 지금인지 모른다."""
    assert _visible(EDITED, _revision("change", "전혀 다른 본문")) == []


# --- 7. 옛 자료는 그대로 검색된다 -----------------------------------------------

LEGACY = [
    RawLine("2026-08-01 09:00", "홍길동", "기성 청구분 정리했습니다", 1),
    RawLine("2026-08-01 09:05", "박과장", "확인했습니다", 2),
]


def test_lines_without_a_coordinate_are_untouched():
    """옛 원문에는 `message_ts` 가 없다. 수집 이전 자료가 전부 여기 해당한다."""
    got = reader.visible_lines(LEGACY, workspace=WS, channel_id=CH, lookup=_boom)

    assert len(got) == 2


def test_a_coordinate_without_markers_is_untouched():
    """수정·삭제된 적이 없으면 DB 를 볼 이유도 없다."""
    lines = [_line("10시입니다", 1)]

    got = reader.visible_lines(lines, workspace=WS, channel_id=CH, lookup=_boom)

    assert [line.text for line in got] == ["10시입니다"]


def test_legacy_survives_even_when_the_database_is_down():
    """장애가 옛 자료 검색까지 끊으면, 고치려던 것보다 큰 사고가 된다."""
    mixed = [*LEGACY, *EDITED]

    got = reader.visible_lines(mixed, workspace=WS, channel_id=CH, lookup=lambda *_: reader.UNKNOWN)

    assert [line.text for line in got] == [line.text for line in LEGACY]


def _boom(*_):
    raise AssertionError("DB 를 보면 안 되는 경우다")


# --- 8. 직접 조회와 색인 조회가 같다 --------------------------------------------

def _doc(lines) -> ArchiveDoc:
    return ArchiveDoc(
        path=Path("raw/2026-09-22.md"), workspace=WS, channel="#팀_자금",
        visibility="private", acl=frozenset({"#팀_자금"}), share_with=frozenset(),
        last_ingested=None, channel_id=CH, schema_version=2, raw_lines=list(lines),
    )


def test_index_exclusions_are_exactly_what_the_reader_hides(monkeypatch):
    """색인에서 빼는 목록과 reader 가 감추는 줄이 **같은 집합**이어야 한다.

    다르면 색인 후보와 파일 판정이 어긋나고, 그때 히트 수가 실제와 달라진다.
    """
    latest = _revision("change", "12시입니다")
    kept = {
        line.lineno
        for line in reader.visible_lines(
            TWICE, workspace=WS, channel_id=CH, lookup=lambda *_: latest
        )
    }
    excluded = reader.index_excluded_keys(
        TWICE, workspace=WS, channel_id=CH, lookup=lambda *_: latest
    )

    assert {line_no for _, line_no in excluded} == {
        line.lineno for line in TWICE
    } - kept


def test_apply_filters_a_document_in_place(monkeypatch):
    """`ArchiveStore.docs()` 가 부르는 자리. 직접 조회가 이 결과를 쓴다."""
    monkeypatch.setattr(
        reader, "latest_revisions", lambda *_: {(CH, TS): _revision("change", "11시입니다")}
    )

    got = reader.apply(_doc(EDITED))

    assert [line.text for line in got.raw_lines] == ["[수정 후] 11시입니다"]


def test_apply_skips_the_database_when_there_are_no_markers(monkeypatch):
    """옛 자료가 대부분이다. 매 문서마다 질의하면 DB 가 답변 경로의 병목이 된다."""
    monkeypatch.setattr(reader, "latest_revisions", _boom)

    got = reader.apply(_doc(LEGACY))

    assert len(got.raw_lines) == 2


def test_apply_hides_everything_when_the_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(reader, "latest_revisions", lambda *_: reader.UNKNOWN)

    got = reader.apply(_doc(EDITED))

    assert got.raw_lines == []


def test_the_store_applies_the_reader_on_the_answer_path():
    """직접 조회와 색인 조회가 **같은 문서 목록**을 쓴다. 거르는 자리도 하나여야 한다."""
    source = (
        Path(__file__).resolve().parent.parent
        / "src" / "tybot" / "archive" / "store.py"
    ).read_text(encoding="utf-8")

    assert "revision_reader.apply(doc)" in source
    # 감사 경로는 거르지 않는다
    audit = source[source.index("def audit_docs"):source.index("def source_docs")]
    assert "revision_reader" not in audit


# --- 표시 문구가 한 곳에만 있다 -------------------------------------------------

def test_the_markers_match_what_the_collector_writes():
    """한쪽만 고치면 그 표시가 **일반 근거로 새어 나온다.**"""
    collector = (
        Path(__file__).resolve().parent.parent
        / "src" / "tybot" / "archiving_bot.py"
    ).read_text(encoding="utf-8")

    for marker in reader.ALL_MARKERS:
        assert marker in collector, marker


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[수정 후] 11시", ("[수정 후]", "11시")),
        ("  [삭제 전]  10시 ", ("[삭제 전]", "10시")),
        ("[삭제됨] Slack에서 삭제된 메시지", ("[삭제됨]", "Slack에서 삭제된 메시지")),
        ("그냥 본문", ("", "그냥 본문")),
        ("[참고] 대괄호로 시작하지만 표시가 아니다", ("", "[참고] 대괄호로 시작하지만 표시가 아니다")),
    ],
)
def test_marker_parsing(text, expected):
    assert reader.strip_marker(text) == expected


def test_the_body_digest_matches_the_writer():
    """두 곳이 갈리면 매칭이 **전부** 실패하고, 그러면 모든 수정 메시지가 사라진다."""
    from tybot.archive import revision_store

    assert reader.body_digest("11시입니다") == revision_store.body_digest("11시입니다")
    assert reader.body_digest("") == revision_store.body_digest("")


# --- ArchiveStore 를 통과하는 통합 ---------------------------------------------

def _written_archive(tmp_path):
    """수집기가 실제로 쓴 원문. 손으로 만든 문자열이 아니라 **진짜 경로**다."""
    from tybot import archiving_bot

    env = {
        "ARCHIVER_CONFIG_SOURCE": "env", "ARCHIVER_WORKSPACES": "tyit",
        "ARCHIVER_BOT_TOKEN_TYIT": "a", "ARCHIVER_APP_TOKEN_TYIT": "b",
        "ARCHIVER_TEAM_ID_TYIT": "T12345678",
        "SLACK_BOT_TOKEN_TYIT": "m", "SLACK_APP_TOKEN_TYIT": "n",
        "ARCHIVER_MASTER_BOT_USER_TYIT": "U_MASTER",
        "ARCHIVER_CHANNEL_IDS_TYIT": "C12345678",
        "ARCHIVE_DIR": str(tmp_path / "live"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "shadow"),
    }

    class Client:
        def conversations_info(self, *, channel):
            return {"channel": {"id": channel, "name": "팀-전산_abb155-공지",
                                "is_member": True}}

        def users_info(self, *, user):
            return {"user": {"name": user}}

    cfg = archiving_bot.load_archiver_workspaces(env)[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    collector.ingest_event(Client(), {
        "channel_type": "channel", "channel": "C12345678", "user": "U1",
        "ts": "1790070000.000001", "text": "회의는 10시입니다",
    })
    collector.ingest_event(Client(), {
        "channel_type": "channel", "channel": "C12345678",
        "subtype": "message_changed", "ts": "1790070100.000001",
        "event_ts": "1790070100.000001",
        "message": {"ts": "1790070000.000001", "user": "U1", "text": "회의는 11시입니다"},
        "previous_message": {"ts": "1790070000.000001", "user": "U1",
                             "text": "회의는 10시입니다"},
    })
    return tmp_path / "shadow"


def _store(root, monkeypatch, latest):
    from tybot.archive.store import ArchiveStore

    monkeypatch.setattr(
        reader, "latest_revisions",
        lambda *_: latest if latest is reader.UNKNOWN
        else {("C12345678", "1790070000.000001"): latest},
    )
    return ArchiveStore(root)


def test_the_store_shows_only_the_current_body(tmp_path, monkeypatch):
    """직접 조회(`docs`)가 곧 검색의 후보 집합이다."""
    root = _written_archive(tmp_path)
    store = _store(root, monkeypatch, _revision("change", "회의는 11시입니다"))

    texts = [line.text for doc in store.docs() for line in doc.raw_lines]

    assert texts == ["[수정 후] 회의는 11시입니다"]
    assert not any("10시" in text for text in texts)


def test_a_deleted_message_disappears_from_the_store(tmp_path, monkeypatch):
    root = _written_archive(tmp_path)
    store = _store(root, monkeypatch, _revision("delete"))

    assert [line for doc in store.docs() for line in doc.raw_lines] == []


def test_the_search_path_and_the_store_agree(tmp_path, monkeypatch):
    """색인 경로도 `visible_docs` 가 준 문서에서 줄을 찾는다(`_scan`).

    거르는 자리가 하나라서 두 경로가 갈릴 수 없다 — 그게 이 설계의 요점이다.
    """
    from tybot.access import RequestContext

    root = _written_archive(tmp_path)
    store = _store(root, monkeypatch, _revision("change", "회의는 11시입니다"))
    ctx = RequestContext(workspace="tyit", role="exec")

    direct = {line.text for doc in store.visible_docs(ctx) for line in doc.raw_lines}
    hits = {hit.line.text for hit in store.search("회의", ctx, limit=20)}

    assert direct == {"[수정 후] 회의는 11시입니다"}
    assert hits == direct


def test_searching_the_old_body_finds_nothing(tmp_path, monkeypatch):
    """「10시」 로 물으면 고치기 전 문장이 근거로 나오던 것이 이 시험의 이유다."""
    from tybot.access import RequestContext

    root = _written_archive(tmp_path)
    store = _store(root, monkeypatch, _revision("change", "회의는 11시입니다"))

    assert store.search("10시", RequestContext(workspace="tyit", role="exec")) == []


def test_the_audit_view_still_holds_every_revision(tmp_path, monkeypatch):
    """과거 revision 은 **감사 조회에서만** 보인다."""
    root = _written_archive(tmp_path)
    store = _store(root, monkeypatch, _revision("change", "회의는 11시입니다"))

    audited = [line.text for doc in store.audit_docs() for line in doc.raw_lines]

    assert any("10시" in text for text in audited)
    assert any("[수정 전]" in text for text in audited)
    assert len(audited) == 3


def test_the_raw_file_is_never_modified(tmp_path, monkeypatch):
    """읽을 때만 거른다. 파일에서 줄을 지우면 감사에서 무엇이 바뀌었는지 못 본다."""
    root = _written_archive(tmp_path)
    path = next(root.glob("workspaces/*/channels/*/raw/*.md"))
    before = path.read_bytes()

    store = _store(root, monkeypatch, _revision("delete"))
    store.docs()

    assert path.read_bytes() == before


def test_a_database_outage_hides_the_revision_but_not_the_rest(tmp_path, monkeypatch):
    """장애가 옛 자료 검색까지 끊으면, 고치려던 것보다 큰 사고가 된다."""
    from tybot.archive import writer

    root = _written_archive(tmp_path)
    writer.ingest(
        root, workspace="tyit", channel="#팀-전산_abb155-공지",
        channel_id="C12345678",
        messages=[writer.IncomingMessage(
            __import__("datetime").datetime.now(__import__("datetime").UTC),
            "U2", "옛 자료 한 줄",
        )],
        acl=["#팀-전산_abb155-공지"],
    )
    store = _store(root, monkeypatch, reader.UNKNOWN)

    texts = [line.text for doc in store.docs() for line in doc.raw_lines]

    assert texts == ["옛 자료 한 줄"]
