"""Archiver 의 개인 DM 경로와 그 경로를 여는 열쇠 (B-68).

설계: `docs/design/archiver-dm-handover.md` §Storage and reading

경로: `<root>/<workspace>/dm/<user-id>/archive/raw/<날짜>.md`

## 이 파일이 지키는 넷

1. **경로가 개인별로 갈린다.** 「기본 제외」 가 필터가 아니라 경로의 성질이어야
   한다 — 채널을 훑는 소비자는 한 줄도 고치지 않아도 개인 기록을 안 본다
2. **본인이 같은 워크스페이스의 DM 에서 물을 때만 열린다.** exec·root 도 예외가
   아니고, 본인이라도 채널에서 물으면 안 나온다
3. **1:1 이 아니면 저장하지 않는다.** 다자 DM 은 한 사람의 개인 공간에 들어갈 수
   없다 — 들어가면 나머지 참여자는 그 기록이 있다는 것조차 모른다
4. **봇 발언은 들어가지 않는다**(절대 원칙 1)

기존 TYBot DM 파일(`workspaces/<ws>/dm/<user>/raw/`)은 **건드리지 않는다.** 그쪽은
복구용 사본으로 남고, 여기서는 읽기만 한다.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.shadow_paths import ShadowPathError, dm_archive_dir, dm_root
from tybot.archive.store import ArchiveStore

WS = "tyit"
ME = "U0BR12345"
SOMEONE_ELSE = "U0OTHER99"
DM_CHANNEL = "D0AB12345"


def _message(text: str = "착공계 초안입니다", *, is_bot: bool = False):
    return writer.IncomingMessage(
        ts=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        speaker="봇" if is_bot else "김현장",
        text=text,
        is_bot=is_bot,
        source_ts="1759100000.000100",
    )


def _write_dm(root, *, user: str = ME, workspace: str = WS, messages=None):
    """Archiver 가 쓰는 자리에 DM 원문을 쌓는다."""
    directory = dm_archive_dir(root, workspace, user, DM_CHANNEL)
    return writer.ingest(
        root,
        workspace=workspace,
        channel=writer.dm_channel(user),
        channel_id=DM_CHANNEL,
        messages=messages if messages is not None else [_message()],
        acl=[],
        dm_user=user,
        dm_directory=directory,
    )


def _ctx(**over) -> RequestContext:
    base = {"workspace": WS, "user_id": ME}
    base.update(over)
    return RequestContext(**base)


# --- 1. 경로가 개인별로 갈린다 ----------------------------------------------------

def test_the_path_separates_workspace_and_person(tmp_path):
    assert dm_root(tmp_path, WS, ME, DM_CHANNEL) == tmp_path / WS / "dm" / ME
    assert dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL).name == "archive"


def test_two_people_never_share_a_directory(tmp_path):
    mine = dm_root(tmp_path, WS, ME, DM_CHANNEL)
    theirs = dm_root(tmp_path, WS, SOMEONE_ELSE, DM_CHANNEL)

    assert mine != theirs
    assert not mine.is_relative_to(theirs)


def test_the_same_person_in_two_workspaces_is_separated(tmp_path):
    """워크스페이스를 떼면 다른 회사 공간의 개인 기록이 한 자리에 모인다(원칙 4)."""
    assert dm_root(tmp_path, WS, ME, DM_CHANNEL) != dm_root(
        tmp_path, "mgmt", ME, DM_CHANNEL
    )


def test_the_dm_path_escapes_nothing(tmp_path):
    for bad in ("../남의워크스페이스", "..", "/etc"):
        with pytest.raises(ShadowPathError):
            dm_root(tmp_path, bad, ME, DM_CHANNEL)


def test_a_symlinked_dm_directory_is_refused(tmp_path):
    """링크를 따라가면 개인 공간이 다른 곳을 가리킬 수 있다."""
    target = tmp_path / "밖"
    target.mkdir()
    base = tmp_path / WS / "dm"
    base.mkdir(parents=True)
    try:
        (base / ME).symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    with pytest.raises(ShadowPathError, match="symlink"):
        dm_root(tmp_path, WS, ME, DM_CHANNEL)


# --- 2. 1:1 이 아니면 저장하지 않는다 ----------------------------------------------

@pytest.mark.parametrize("channel_id", [
    "G0GROUP12",   # 다자 DM(mpim)
    "C0CHAN123",   # 공개 채널
    "",
    "D",
])
def test_a_conversation_that_is_not_a_one_to_one_dm_is_refused(tmp_path, channel_id):
    """다자 DM 이 한 사람의 개인 공간에 들어가면 나머지 참여자는 모른다."""
    with pytest.raises(ShadowPathError, match="1:1"):
        dm_root(tmp_path, WS, ME, channel_id)


def test_a_group_dm_cannot_be_written_at_all(tmp_path):
    """경로를 못 만들면 쓸 수도 없다. 판정이 경로에 있는 것이 요점이다."""
    with pytest.raises(ShadowPathError, match="1:1"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id="G0GROUP12", messages=[_message()], acl=[], dm_user=ME,
            dm_directory=dm_archive_dir(tmp_path, WS, ME, "G0GROUP12"),
        )
    assert not (tmp_path / WS).exists()


def test_a_bad_user_id_is_refused(tmp_path):
    for bad in ("", "현장소장", "C0FUND123"):
        with pytest.raises(ShadowPathError, match="user ID"):
            dm_root(tmp_path, WS, bad, DM_CHANNEL)


# --- 3. 봇 발언은 들어가지 않는다 --------------------------------------------------

def test_a_bot_reply_is_not_stored(tmp_path):
    """봇이 만든 문장을 사람이 확인하기 전에 근거로 저장하지 않는다(절대 원칙 1)."""
    result = _write_dm(tmp_path, messages=[_message("제가 찾은 답은", is_bot=True)])

    assert result.written == 0
    assert result.skipped_bot == 1


def test_a_bot_reply_mixed_with_a_question_keeps_only_the_person(tmp_path):
    result = _write_dm(tmp_path, messages=[
        _message("착공계 어디 있나요"),
        _message("여기 있습니다", is_bot=True),
    ])

    assert (result.written, result.skipped_bot) == (1, 1)
    assert "여기 있습니다" not in result.path.read_text(encoding="utf-8")


# --- 4. 본인이, 같은 워크스페이스에서, DM 으로 물을 때만 ---------------------------

def _store_with_my_dm(tmp_path) -> ArchiveStore:
    _write_dm(tmp_path)
    return ArchiveStore(tmp_path)


def test_the_owner_reads_their_own_dm(tmp_path):
    docs = _store_with_my_dm(tmp_path).visible_docs(_ctx())

    assert [doc.dm_user for doc in docs] == [ME]


def test_another_person_never_reads_it(tmp_path):
    assert _store_with_my_dm(tmp_path).visible_docs(_ctx(user_id=SOMEONE_ELSE)) == []


def test_an_exec_does_not_read_it(tmp_path):
    """통합조회 권한은 조직의 기록을 보는 권한이지 개인 작업공간을 보는 권한이 아니다."""
    store = _store_with_my_dm(tmp_path)

    assert store.visible_docs(_ctx(user_id=SOMEONE_ELSE, role="exec")) == []


def test_root_does_not_read_it(tmp_path):
    store = _store_with_my_dm(tmp_path)

    assert store.visible_docs(_ctx(user_id=SOMEONE_ELSE, is_root=True)) == []


def test_the_owner_asking_in_a_channel_does_not_read_it(tmp_path):
    """답을 그 채널 사람들이 함께 본다. 본인이 물었어도 개인 자료가 공개로 나간다."""
    store = _store_with_my_dm(tmp_path)

    assert store.visible_docs(_ctx(channel_id="C0FUND123")) == []
    assert store.visible_docs(_ctx(channel="#팀-전산_ABB110-회의")) == []


def test_the_owner_in_another_workspace_does_not_read_it(tmp_path):
    store = _store_with_my_dm(tmp_path)

    assert store.visible_docs(_ctx(workspace="mgmt")) == []


def test_a_request_without_a_person_reads_nothing(tmp_path):
    """콘솔·배치처럼 사람이 특정되지 않는 경로가 개인 기록을 읽지 않는다."""
    store = _store_with_my_dm(tmp_path)

    assert store.visible_docs(_ctx(user_id="")) == []


# --- 5. 채널 검색·Hermes 기본 검색에서 제외 ----------------------------------------

def test_channel_enumeration_never_sees_the_dm(tmp_path):
    """`dm_scope` 를 안 주면 파일을 **열지도 않는다.** 걸러 내는 구조가 아니다."""
    store = _store_with_my_dm(tmp_path)

    assert store.docs() == []
    assert store.source_docs() == []


def test_the_dm_is_outside_the_channel_globs(tmp_path):
    """경로가 채널 글롭에 안 걸린다는 사실 자체를 고정한다."""
    _write_dm(tmp_path)

    assert sorted(tmp_path.glob("*/*__*/archive/raw/*.md")) == []
    assert sorted((tmp_path / "workspaces").glob("*/channels/*/raw/*.md")) == []


def test_the_owner_scope_opens_it(tmp_path):
    """제외가 기본값이라는 것이 「아예 못 읽는다」 는 뜻은 아니다."""
    store = _store_with_my_dm(tmp_path)

    assert [doc.dm_user for doc in store.docs(dm_scope=ME)] == [ME]


def test_another_persons_scope_does_not_open_it(tmp_path):
    store = _store_with_my_dm(tmp_path)

    assert store.docs(dm_scope=SOMEONE_ELSE) == []


# --- 6. 기존 TYBot DM 파일을 건드리지 않는다 ---------------------------------------

def test_the_old_master_dm_files_are_untouched(tmp_path):
    """기존 자료는 복구용 사본으로 남는다. 조용히 합치지 않는다."""
    old = tmp_path / "workspaces" / WS / "dm" / "u0br12345" / "raw" / "2026-08-01.md"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("옛 Master DM 문서", encoding="utf-8")
    before = (old.read_bytes(), old.stat().st_mtime_ns)

    _write_dm(tmp_path)

    assert (old.read_bytes(), old.stat().st_mtime_ns) == before


def test_the_new_path_does_not_overwrite_the_old_one(tmp_path):
    """두 경로가 겹치면 새 writer 가 옛 파일 위에 쓴다."""
    result = _write_dm(tmp_path)

    old_dir = tmp_path / "workspaces" / WS / "dm"
    assert not result.path.is_relative_to(old_dir)


# --- 7. writer 가 두 경로를 섞지 않는다 --------------------------------------------

def test_a_dm_directory_without_a_person_is_refused(tmp_path):
    """`dm_user` 가 없으면 문서에 그 값이 안 들어가고, 권한이 채널처럼 판정된다."""
    with pytest.raises(ValueError, match="dm_user"):
        writer.ingest(
            tmp_path, workspace=WS, channel="DM:x", channel_id=DM_CHANNEL,
            messages=[_message()], acl=[],
            dm_directory=dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL),
        )


def test_a_document_cannot_be_both_a_channel_and_a_dm(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        writer.ingest(
            tmp_path, workspace=WS, channel="DM:x", channel_id=DM_CHANNEL,
            messages=[_message()], acl=[], dm_user=ME,
            dm_directory=dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL),
            channel_directory=tmp_path / "어딘가",
        )


def test_a_dm_directory_outside_the_root_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must be exactly"):
        writer.ingest(
            tmp_path / "root", workspace=WS, channel="DM:x", channel_id=DM_CHANNEL,
            messages=[_message()], acl=[], dm_user=ME,
            dm_directory=tmp_path / "밖" / "archive",
        )


def test_the_document_keeps_the_person_and_the_source_coordinate(tmp_path):
    """`dm_user` 가 빠지면 권한 판정이 그 문서를 채널 문서처럼 다룬다."""
    text = _write_dm(tmp_path).path.read_text(encoding="utf-8")

    assert f"dm_user: {ME}" in text
    assert "1759100000.000100" in text


def test_an_unscoped_read_opens_nothing_even_for_a_directory_named_unnamed(tmp_path):
    """`dm_scope` 가 비면 **글롭을 만들지도 않아야** 한다.

    `_slugify("")` 는 `"unnamed"` 를 돌려준다. 그래서 `if dm_scope:` 가드가 사라지면
    범위 없는 읽기가 `*/dm/unnamed/...` 를 훑는다. 그 이름의 디렉터리가 하나라도
    있으면 사람이 특정되지 않은 경로가 개인 기록을 연다.

    돌연변이로 확인했다 — 가드를 `if True:` 로 바꿔도 이 시험이 없으면 통과했다.
    """
    _write_dm(tmp_path)
    for raw in (
        tmp_path / WS / "dm" / "unnamed" / "archive" / "raw" / "2026-09-01.md",
        tmp_path / "workspaces" / WS / "dm" / "unnamed" / "raw" / "2026-09-01.md",
    ):
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(
            "---\nworkspace: tyit\nchannel: \"DM:unnamed\"\nvisibility: private\n"
            "acl: []\ndm_user: U0GHOST99\nlast_ingested: 2026-09-01T10:00+09:00\n"
            "---\n\n## 원문\n\n> [2026-09-01 10:00] 누군가: 열리면 안 되는 줄\n",
            encoding="utf-8",
        )

    assert ArchiveStore(tmp_path).docs() == []
    assert ArchiveStore(tmp_path).source_docs() == []


# --- 8. writer 는 **정확히 그 경로만** 받는다 --------------------------------------
#
# 2026-10-01 지적. 「루트 안」 조건만으로는 부족하다. 그 조건은 채널 디렉터리도
# 통과시키고, 그러면 개인 문서가 채널을 훑는 글롭 아래에 쌓인다.

def test_a_channel_directory_cannot_be_passed_as_a_dm_directory(tmp_path):
    """**이 시험이 이 정정의 핵심이다.**

    채널 디렉터리는 루트 안에 있다. 그것만 보면 통과하고, 그 순간 개인 기록이
    `<ws>/<channel-id>__<이름>/archive/raw/` 에 생긴다 — 채널 글롭이 잡는 자리다.
    그 뒤로는 아무도 그 문서가 개인 것이라는 사실을 모른다.
    """
    from tybot.archive.shadow_paths import archive_dir as channel_archive_dir

    channel = channel_archive_dir(tmp_path, WS, "C0FUND123", "팀-전산_ABB110-회의")

    with pytest.raises(ValueError, match="must be exactly"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=DM_CHANNEL, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=channel,
        )
    assert not channel.exists()


def test_another_persons_dm_directory_is_refused(tmp_path):
    """`dm_user` 와 경로가 어긋나면 남의 공간에 내 이름표가 붙은 문서가 생긴다."""
    theirs = dm_archive_dir(tmp_path, WS, SOMEONE_ELSE, DM_CHANNEL)

    with pytest.raises(ValueError, match="must be exactly"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=DM_CHANNEL, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=theirs,
        )
    assert not theirs.exists()


def test_another_workspaces_dm_directory_is_refused(tmp_path):
    other = dm_archive_dir(tmp_path, "mgmt", ME, DM_CHANNEL)

    with pytest.raises(ValueError, match="must be exactly"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=DM_CHANNEL, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=other,
        )
    assert not other.exists()


def test_a_directory_beside_the_archive_one_is_refused(tmp_path):
    """`archive` 가 아니라 `objects`·`staging` 에 원문을 쓰면 reader 가 못 본다."""
    beside = dm_root(tmp_path, WS, ME, DM_CHANNEL) / "objects"

    with pytest.raises(ValueError, match="must be exactly"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=DM_CHANNEL, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=beside,
        )


@pytest.mark.parametrize("channel_id", ["G0GROUP12", "C0CHAN123", "", None])
def test_the_writer_requires_a_one_to_one_dm_channel_id(tmp_path, channel_id):
    """다자 DM·채널 ID 로는 개인 경로를 만들 수 없으니 쓸 수도 없다."""
    with pytest.raises(ValueError, match="not a private DM path"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=channel_id, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL),
        )


def test_the_expected_path_is_accepted(tmp_path):
    """막기만 하면 쓸 수 없다. 맞는 경로는 그대로 통과해야 한다."""
    result = _write_dm(tmp_path)

    assert result.written == 1
    assert result.path.parent.parent == dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL)


# --- 9. 중간 경로의 심볼릭 링크 ----------------------------------------------------
#
# 마지막 칸만 보면 `<workspace>` 나 `dm` 이 다른 곳을 가리킬 때 통과한다. 경로
# 문자열은 개인 공간처럼 보이고 실제 파일은 남의 자리에 쌓인다.

def _link_or_skip(link: pathlib.Path, target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")


@pytest.mark.parametrize("where", ["workspace", "dm", "user", "archive"])
def test_a_symlink_anywhere_in_the_dm_path_is_refused(tmp_path, where):
    link = {
        "workspace": tmp_path / WS,
        "dm": tmp_path / WS / "dm",
        "user": tmp_path / WS / "dm" / ME,
        "archive": tmp_path / WS / "dm" / ME / "archive",
    }[where]
    _link_or_skip(link, tmp_path / "밖" / where)

    with pytest.raises(ShadowPathError, match="symlink"):
        dm_archive_dir(tmp_path, WS, ME, DM_CHANNEL)


@pytest.mark.parametrize("where", ["workspace", "dm", "user"])
def test_the_writer_refuses_a_symlinked_dm_path(tmp_path, where):
    """경로 판정이 writer 입구에서도 같은 답을 내야 한다."""
    link = {
        "workspace": tmp_path / WS,
        "dm": tmp_path / WS / "dm",
        "user": tmp_path / WS / "dm" / ME,
    }[where]
    _link_or_skip(link, tmp_path / "밖" / where)

    with pytest.raises(ValueError, match="not a private DM path"):
        writer.ingest(
            tmp_path, workspace=WS, channel=writer.dm_channel(ME),
            channel_id=DM_CHANNEL, messages=[_message()], acl=[], dm_user=ME,
            dm_directory=tmp_path / WS / "dm" / ME / "archive",
        )
