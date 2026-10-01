"""읽을 때도 **좌표가 다르면 다른 메시지다.**

결정: 2026-10-01 오너 지시. `2e066cc` 의 후속.

## 왜 writer 만으로는 반쪽인가

writer 는 좌표가 다른 두 줄을 각각 파일에 쓴다(`2e066cc`). 그런데 읽는 쪽
`ArchiveStore._merge()` 는 `(표시시각, 화자, 본문)` 으로 합쳤다 — **좌표를 안
본다.** 그래서 파일에는 두 줄이 있는데 답변에는 한 줄로 나왔다.

사람이 두 번 말한 것이 한 번으로 보이는 상태이고, 그 차이는 파일을 직접 열기
전에는 안 드러난다(절대 원칙 1).

## 지켜야 하는 넷

1. 서로 다른 `message_ts` 는 **모두** 읽힌다
2. 같은 좌표의 재수집은 **한 건**이다
3. 좌표 없는 옛 줄과 좌표 있는 줄이 함께 있으면 **좌표 있는 쪽**이 남는다
4. **옛 줄 하나 때문에 서로 다른 좌표 두 건이 합쳐지지 않는다**

4번이 요점이다. 옛 줄 하나로는 A 와 B 를 구분할 수 없는데, 그 줄을 근거로 둘을
합치면 사람이 한 말이 사라진다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.store import ArchiveStore

WS = "tyit"
CHANNEL = "#팀-전산_ABB110-회의"
CID = "C0FUND123"
WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

A = "1759100000.000100"
B = "1759100030.000200"


def _msg(text: str, *, source_ts: str = "", speaker: str = "김현장", when=WHEN):
    return writer.IncomingMessage(
        ts=when, speaker=speaker, text=text, source_ts=source_ts,
    )


def _ingest(root, messages, *, day: int | None = None) -> None:
    writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CID,
        messages=messages, acl=[CHANNEL],
    )


def _ctx() -> RequestContext:
    """그 채널에서 묻는 사람. DM 판정과 무관하다."""
    return RequestContext(
        workspace=WS, channels=frozenset({CHANNEL}),
        channel_id=CID, channel=CHANNEL,
    )


def _read(root) -> list[tuple[str, str]]:
    """`docs()` 가 보여 주는 (본문, 좌표)."""
    return [
        (line.text, line.message_ts)
        for doc in ArchiveStore(root).docs()
        for line in doc.raw_lines
    ]


def _visible(root) -> list[tuple[str, str]]:
    return [
        (line.text, line.message_ts)
        for doc in ArchiveStore(root).visible_docs(_ctx())
        for line in doc.raw_lines
    ]


# --- 1. 서로 다른 좌표는 모두 읽힌다 -----------------------------------------------

def test_two_coordinates_are_both_read(tmp_path):
    """**이 시험이 이 수정의 이유다.** 같은 분·화자·본문이어도 다른 메시지다."""
    _ingest(tmp_path, [_msg("확인했습니다", source_ts=A),
                       _msg("확인했습니다", source_ts=B)])

    assert _read(tmp_path) == [("확인했습니다", A), ("확인했습니다", B)]


def test_two_coordinates_are_both_visible(tmp_path):
    """답변이 보는 길도 같아야 한다. `visible_docs` 는 `docs` 를 지난다."""
    _ingest(tmp_path, [_msg("네", source_ts=A), _msg("네", source_ts=B)])

    assert _visible(tmp_path) == [("네", A), ("네", B)]


def test_two_coordinates_across_two_day_files_are_both_read(tmp_path):
    """날짜 파일이 갈려도 같다. `_merge` 가 실제로 묶는 경우가 이것이다."""
    _ingest(tmp_path, [_msg("네", source_ts=A)])
    _ingest(tmp_path, [_msg("네", source_ts=B, when=datetime(
        2026, 9, 2, 10, 0, tzinfo=UTC))])

    assert sorted(ts for _, ts in _read(tmp_path)) == sorted([A, B])


# --- 2. 같은 좌표의 재수집은 한 건이다 ---------------------------------------------

def test_the_same_coordinate_reads_once(tmp_path):
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts=A)])
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts=A)])

    assert _read(tmp_path) == [("착공계 올립니다", A)]


# --- 3. 좌표 있는 쪽이 남는다 ------------------------------------------------------

def test_the_coordinated_copy_wins_over_the_old_one(tmp_path):
    """출처가 조용히 채널 링크로 내려앉으면 사람이 그 줄을 못 찾는다."""
    _ingest(tmp_path, [_msg("착공계 올립니다")])
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts=A)])

    assert _read(tmp_path) == [("착공계 올립니다", A)]


def test_an_uncoordinated_line_alone_still_reads(tmp_path):
    """좌표가 아예 없던 시절의 자료도 그대로 읽혀야 한다."""
    _ingest(tmp_path, [_msg("옛날 줄입니다")])

    assert _read(tmp_path) == [("옛날 줄입니다", "")]


def test_two_uncoordinated_copies_read_once(tmp_path):
    """좌표가 둘 다 없으면 가를 근거가 없다. 전과 같이 한 줄이다."""
    root = tmp_path / "a"
    _ingest(root, [_msg("네")])
    _ingest(root, [_msg("네")])

    assert _read(root) == [("네", "")]


# --- 4. 옛 줄 하나가 두 좌표를 합치지 않는다 ---------------------------------------

def test_one_old_line_does_not_collapse_two_coordinates(tmp_path):
    """**요점.** 옛 줄 하나로는 A 와 B 를 구분할 수 없다.

    그 줄을 근거로 둘을 합치면 사람이 한 말이 아카이브에서 사라진다.
    """
    _ingest(tmp_path, [_msg("네")])
    _ingest(tmp_path, [_msg("네", source_ts=A)])
    _ingest(tmp_path, [_msg("네", source_ts=B)])

    assert _read(tmp_path) == [("네", A), ("네", B)]


def test_the_same_case_is_visible(tmp_path):
    _ingest(tmp_path, [_msg("네")])
    _ingest(tmp_path, [_msg("네", source_ts=A)])
    _ingest(tmp_path, [_msg("네", source_ts=B)])

    assert _visible(tmp_path) == [("네", A), ("네", B)]


def test_an_old_line_with_no_coordinated_twin_survives(tmp_path):
    """흡수는 **같은 말**일 때만이다. 다른 말이 덩달아 사라지면 안 된다."""
    _ingest(tmp_path, [_msg("예전에만 한 말")])
    _ingest(tmp_path, [_msg("요즘 한 말", source_ts=A)])

    assert sorted(_read(tmp_path)) == sorted(
        [("예전에만 한 말", ""), ("요즘 한 말", A)]
    )


def test_different_speakers_are_not_collapsed(tmp_path):
    """화자가 다르면 다른 줄이다. 좌표를 공유해도 그렇다."""
    _ingest(tmp_path, [_msg("네", source_ts=A),
                       _msg("네", source_ts=A, speaker="이감리")])

    assert len(_read(tmp_path)) == 2


# --- 같은 좌표가 두 파일에 있는 경우 ------------------------------------------------
#
# writer 는 한 채널의 `raw/*.md` 를 전부 읽고 쓰므로 같은 좌표를 두 번 쓰지 않는다.
# 그래서 `_merge` 의 좌표 중복 판정에 닿으려면 파일이 밖에서 생겨야 한다 — 복구본을
# 되돌려 놓거나, 두 writer 가 각자 쓴 경우다.

def test_the_same_coordinate_in_two_files_reads_once(tmp_path):
    """`_merge` 자신의 중복 판정. writer 가 막아 주지 않는 자리다."""
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts=A)])
    written = next(tmp_path.rglob("raw/*.md"))
    (written.parent / "2026-09-02.md").write_text(
        written.read_text(encoding="utf-8"), encoding="utf-8",
    )

    assert _read(tmp_path) == [("착공계 올립니다", A)]


def test_two_files_with_different_coordinates_read_both(tmp_path):
    """막는 쪽만 보면 반대 실수를 못 잡는다."""
    _ingest(tmp_path, [_msg("네", source_ts=A)])
    written = next(tmp_path.rglob("raw/*.md"))
    (written.parent / "2026-09-02.md").write_text(
        written.read_text(encoding="utf-8").replace(A, B), encoding="utf-8",
    )

    assert sorted(ts for _, ts in _read(tmp_path)) == sorted([A, B])
