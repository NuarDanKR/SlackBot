"""운영 루트 **하나**에서 새 구조를 읽는다.

결정: 2026-10-01 오너 지시.

최종 운영 루트는 `/var/lib/tybot/archive` 하나이고, 그 아래 Archiver 가 쓰는
`<workspace>/<channel-id>__<name>/{archive,objects,staging}` 를 TYBot 이 읽는다.
**옛 루트와 합쳐 읽는 기능은 만들지 않는다** — 루트가 둘이면 검색 색인의
상대경로가 한 이름공간에서 부딪히고, 안 맞는 쪽은 조용히 버려진다.

## 이 파일이 보는 셋

1. 새 구조의 원문이 **권한 필터를 거쳐 답변 근거가 되는가**
2. **옛 자료가 운영 검색에 남지 않는가** — 운영 루트 밖으로 뺀 뒤
3. **DM 이 채널 검색에 섞이지 않는가**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tybot.access import RequestContext
from tybot.archive import shadow_paths, writer
from tybot.archive.store import ArchiveStore, workspace_from_path

WS = "tyit"
CHANNEL = "#팀-전산_ABB110-회의"
CID = "C0FUND123"
OTHER_CID = "C0OTHER99"
OTHER_CHANNEL = "#팀-자금_ABB540-주간보고"
ME = "U0BR12345"
DM_CHANNEL_ID = "D0AB12345"
WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _msg(text: str, *, source_ts: str = "1759100000.000100", speaker: str = "김현장"):
    return writer.IncomingMessage(ts=WHEN, speaker=speaker, text=text,
                                  source_ts=source_ts)


def _new_channel(root, *, text="착공계 제출했습니다", channel=CHANNEL, cid=CID):
    """Archiver 가 쓰는 자리. 운영 루트 **안**의 새 구조다."""
    return writer.ingest(
        root, workspace=WS, channel=channel, channel_id=cid,
        messages=[_msg(text)], acl=[channel],
        channel_directory=shadow_paths.archive_dir(root, WS, cid, channel),
    )


def _old_channel(root, *, text="옛 구조의 줄"):
    """옛 v2 구조. 운영 루트에서 **빠질** 자료다."""
    return writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CID,
        messages=[_msg(text, source_ts="1759200000.000100")], acl=[CHANNEL],
    )


def _new_dm(root, *, text="개인 메모입니다"):
    return writer.ingest(
        root, workspace=WS, channel=writer.dm_channel(ME), channel_id=DM_CHANNEL_ID,
        messages=[_msg(text, source_ts="1759300000.000100")], acl=[], dm_user=ME,
        dm_directory=shadow_paths.dm_archive_dir(root, WS, ME, DM_CHANNEL_ID),
    )


def _channel_ctx(channel: str = CHANNEL, cid: str = CID) -> RequestContext:
    return RequestContext(workspace=WS, channels=frozenset({channel}),
                          channel_id=cid, channel=channel)


def _dm_ctx() -> RequestContext:
    return RequestContext(workspace=WS, user_id=ME)


def _texts(docs) -> list[str]:
    return [line.text for doc in docs for line in doc.raw_lines]


# --- 1. 새 구조가 답변 근거가 된다 -------------------------------------------------

def test_the_new_layout_is_read_from_the_single_root(tmp_path):
    _new_channel(tmp_path)

    assert _texts(ArchiveStore(tmp_path).docs()) == ["착공계 제출했습니다"]


def test_the_new_layout_passes_the_permission_filter(tmp_path):
    """읽히는 것과 **그 사람에게 보이는 것**은 다르다. 답변은 뒤엣것만 쓴다."""
    _new_channel(tmp_path)

    docs = ArchiveStore(tmp_path).visible_docs(_channel_ctx())

    assert _texts(docs) == ["착공계 제출했습니다"]


def test_another_channel_does_not_see_it(tmp_path):
    """채널에서 온 질문은 그 채널 자료만 연다(절대 원칙 3)."""
    _new_channel(tmp_path)

    docs = ArchiveStore(tmp_path).visible_docs(
        _channel_ctx(OTHER_CHANNEL, OTHER_CID)
    )

    assert docs == []


def test_another_workspace_does_not_see_it(tmp_path):
    _new_channel(tmp_path)

    ctx = RequestContext(workspace="mgmt", channels=frozenset({CHANNEL}),
                         channel_id=CID, channel=CHANNEL)

    assert ArchiveStore(tmp_path).visible_docs(ctx) == []


def test_the_new_layout_is_searchable(tmp_path):
    """색인이 없으면 파일 스캔으로 떨어진다. 둘 다 같은 답을 내야 한다."""
    _new_channel(tmp_path)

    hits = ArchiveStore(tmp_path).search("착공계", _channel_ctx())

    assert [hit.line.text for hit in hits] == ["착공계 제출했습니다"]


def test_the_new_layout_carries_its_source(tmp_path):
    """출처 없는 답은 근거를 못 찾은 답이다(절대 원칙 2).

    출처는 `SearchHit` 이 만든다 — 답변이 인용하는 단위가 문서가 아니라 줄이기
    때문이다.
    """
    _new_channel(tmp_path)

    (hit,) = ArchiveStore(tmp_path).search("착공계", _channel_ctx())

    citation = hit.citation()
    assert "📄" in citation
    assert hit.doc.channel_id == CID


def test_the_new_layout_keeps_its_coordinate(tmp_path):
    """후속 질문이 그 줄을 다시 여는 좌표다."""
    _new_channel(tmp_path)

    (line,) = [ln for doc in ArchiveStore(tmp_path).docs() for ln in doc.raw_lines]

    assert line.message_ts == "1759100000.000100"


def test_two_channels_in_the_new_layout_stay_apart(tmp_path):
    """한 루트 안에서 채널이 섞이면 근거 오염이다(B-61)."""
    _new_channel(tmp_path)
    _new_channel(tmp_path, text="자금 쪽 줄", channel=OTHER_CHANNEL, cid=OTHER_CID)

    mine = ArchiveStore(tmp_path).visible_docs(_channel_ctx())

    assert _texts(mine) == ["착공계 제출했습니다"]


# --- 2. 옛 자료가 운영 검색에 남지 않는다 ------------------------------------------

def test_the_operational_root_reports_the_legacy_files_it_still_holds(tmp_path):
    """**옮기기 전에 셀 수 있어야 한다.** 0 이 된 것을 보고 글롭을 뗀다."""
    _old_channel(tmp_path)
    _new_channel(tmp_path)

    found = ArchiveStore(tmp_path).legacy_files()

    assert len(found) == 1
    assert found[0].is_relative_to(tmp_path / "workspaces")


def test_a_root_with_only_the_new_layout_reports_none(tmp_path):
    _new_channel(tmp_path)
    _new_dm(tmp_path)

    assert ArchiveStore(tmp_path).legacy_files() == []


def test_moving_the_legacy_data_out_removes_it_from_search(tmp_path):
    """백업은 운영 루트 **밖**이다. 밖으로 나가면 검색에서 사라진다."""
    _old_channel(tmp_path)
    _new_channel(tmp_path)
    assert len(_texts(ArchiveStore(tmp_path).docs())) == 2

    # Codex 가 할 이동을 흉내 낸다. 지우지 않고 루트 밖으로 옮긴다.
    (tmp_path / "workspaces").rename(tmp_path.parent / "backup-workspaces")

    assert _texts(ArchiveStore(tmp_path).docs()) == ["착공계 제출했습니다"]
    assert ArchiveStore(tmp_path).legacy_files() == []


def test_the_legacy_report_counts_every_old_layout(tmp_path):
    """한 갈래라도 빠지면 「0 건」 이 거짓이 되고, 그 상태로 글롭을 뗀다."""
    _old_channel(tmp_path)
    v1 = tmp_path / "channels" / WS / "옛평면.md"
    v1.parent.mkdir(parents=True, exist_ok=True)
    v1.write_text("v1 문서", encoding="utf-8")
    flat = tmp_path / "workspaces" / WS / "channels" / "구조1.md"
    flat.write_text("구조1 문서", encoding="utf-8")

    found = ArchiveStore(tmp_path).legacy_files()

    assert len(found) == 3


def test_the_new_layout_is_not_counted_as_legacy(tmp_path):
    """새 구조를 옛 것으로 세면 영원히 0 이 안 되고 전환이 막힌다."""
    _new_channel(tmp_path)
    _new_dm(tmp_path)

    assert ArchiveStore(tmp_path).legacy_files() == []


# --- 3. DM 이 채널 검색에 섞이지 않는다 --------------------------------------------

def test_a_dm_is_not_in_the_channel_read(tmp_path):
    _new_channel(tmp_path)
    _new_dm(tmp_path)

    assert _texts(ArchiveStore(tmp_path).docs()) == ["착공계 제출했습니다"]


def test_a_dm_is_not_visible_from_a_channel_question(tmp_path):
    """본인이 물었어도 채널에서 물으면 개인 자료가 공개 화면으로 나간다."""
    _new_dm(tmp_path)

    ctx = RequestContext(workspace=WS, channels=frozenset({CHANNEL}),
                         channel_id=CID, channel=CHANNEL, user_id=ME)

    assert ArchiveStore(tmp_path).visible_docs(ctx) == []


def test_a_dm_is_not_searchable_from_a_channel(tmp_path):
    _new_dm(tmp_path, text="개인 메모 착공계")
    _new_channel(tmp_path)

    hits = ArchiveStore(tmp_path).search("착공계", _channel_ctx())

    assert [hit.line.text for hit in hits] == ["착공계 제출했습니다"]


def test_the_owner_reads_their_dm_in_a_dm_question(tmp_path):
    """제외가 기본값이라는 것이 「아예 못 읽는다」 는 뜻은 아니다."""
    _new_dm(tmp_path)

    docs = ArchiveStore(tmp_path).visible_docs(_dm_ctx())

    assert _texts(docs) == ["개인 메모입니다"]


# --- 콘솔이 새 구조의 깨진 문서를 제 워크스페이스로 센다 ----------------------------

def test_a_broken_file_in_the_new_layout_is_attributed_to_its_workspace(tmp_path):
    """깨진 문서는 프론트매터를 못 읽으므로 **경로**가 유일한 단서다.

    `unknown` 으로 떨어지면 콘솔에서 그 파일을 어느 워크스페이스에서 찾아야 할지
    알 수 없다.
    """
    path = tmp_path / WS / f"{CID}__팀-전산" / "archive" / "raw" / "2026-09-01.md"

    assert workspace_from_path(path, tmp_path) == WS


def test_a_broken_dm_file_is_attributed_to_its_workspace(tmp_path):
    path = tmp_path / WS / "dm" / ME / "archive" / "raw" / "2026-09-01.md"

    assert workspace_from_path(path, tmp_path) == WS


@pytest.mark.parametrize("path_parts,expected", [
    (("workspaces", WS, "channels", "C1__x", "raw", "a.md"), WS),
    (("channels", WS, "a.md"), WS),
    (("a.md",), "unknown"),
])
def test_the_old_layouts_still_resolve(tmp_path, path_parts, expected):
    """옛 경로 판정이 바뀌면 백업 전까지 콘솔이 그 문서를 못 찾는다."""
    assert workspace_from_path(tmp_path.joinpath(*path_parts), tmp_path) == expected


# --- 색인이 새 구조를 가리키면 그 줄이 열린다 --------------------------------------
#
# `store.search()` 는 색인 후보를 **루트 기준 상대경로**로 다시 찾는다. 안 맞으면
# 그 후보를 조용히 버린다("색인에 있으나 지금 권한으로는 안 보이는 문서" 분기).
# 그래서 새 구조에서 경로가 어긋나면 **검색만 비고 오류는 안 난다.**

def test_an_indexed_candidate_in_the_new_layout_is_found(tmp_path, monkeypatch):
    from tybot import search_index

    _new_channel(tmp_path)
    store = ArchiveStore(tmp_path)
    (doc,) = store.docs()
    (line,) = doc.raw_lines

    class _Candidate:
        def __init__(self, doc_path: str, line_no: int):
            self.doc_path = doc_path
            self.line_no = line_no

    wanted = search_index.rel_path(line.source_path or doc.path, store.root)
    monkeypatch.setattr(
        search_index, "candidates",
        lambda query, channels: [_Candidate(wanted, line.lineno)],
    )

    hits = store.search("착공계", _channel_ctx())

    assert [hit.line.text for hit in hits] == ["착공계 제출했습니다"]


def test_an_indexed_candidate_from_another_root_never_becomes_a_hit(tmp_path, monkeypatch):
    """루트를 합쳐 읽지 않는다는 사실의 다른 얼굴이다.

    다른 루트에서 색인된 후보는 이 루트의 어떤 파일과도 안 맞는다. 그 후보가 **근거가
    되지 않는** 것이 요점이다 — 되면 지금 루트에 없는 원문을 인용하게 된다.

    매칭이 0 건이면 `search()` 는 파일 스캔으로 보완한다(「색인 없음」이 「자료
    없음」으로 보이지 않게). 그래서 답은 비지 않고, 나오는 근거는 **이 루트의 것**이다.
    """
    from tybot import search_index

    _new_channel(tmp_path)
    store = ArchiveStore(tmp_path)

    class _Candidate:
        doc_path = "다른루트/tyit/C0FUND123__팀/archive/raw/2026-09-01.md"
        line_no = 1

    monkeypatch.setattr(
        search_index, "candidates", lambda query, channels: [_Candidate()],
    )

    hits = store.search("착공계", _channel_ctx())

    assert hits, "색인이 안 맞으면 파일 스캔이 받아야 한다"
    for hit in hits:
        assert hit.doc.path.is_relative_to(tmp_path)
        assert "다른루트" not in str(hit.line.source_path or hit.doc.path)
