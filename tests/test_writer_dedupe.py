"""원문 중복 판정 — **좌표가 다르면 다른 메시지다.**

결정: 2026-10-01 오너 지시.

## 무엇이 문제였나

`dedupe_line()` 은 Slack 좌표를 **떼고** 비교했다. 그러면 같은 분·같은 사람·같은
글자인 두 메시지가 한 줄로 합쳐진다. 짧은 말일수록 흔하다 — 「네」, 「확인했습니다」,
「ㅇㅇ」 는 같은 회의 중에 몇 번이고 나온다.

합쳐진 쪽은 오류를 내지 않는다. 사람이 두 번 말한 것이 한 번으로 남을 뿐이고,
그 차이는 원문을 직접 세어 보기 전에는 드러나지 않는다(절대 원칙 1).

좌표를 떼고 본 데는 이유가 있었다. 좌표를 남기기 시작한 날, 이미 아카이브에 있던
줄에는 좌표가 없다. 표기까지 비교하면 같은 메시지가 좌표 있는 것과 없는 것으로
두 번 쌓인다. **그 호환은 그대로 두고** 좌표가 둘 다 있을 때만 좌표로 가른다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tybot.archive import writer

WS = "tyit"
CHANNEL = "#팀-전산_ABB110-회의"
CID = "C0FUND123"
WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _msg(text: str, *, source_ts: str = "", speaker: str = "김현장", when=WHEN):
    return writer.IncomingMessage(
        ts=when, speaker=speaker, text=text, source_ts=source_ts,
    )


def _ingest(root, messages) -> writer.IngestResult:
    return writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CID,
        messages=messages, acl=[CHANNEL],
    )


def _raw(root) -> list[str]:
    """파일에 실제로 쓰인 원문 줄. **reader 를 거치지 않는다.**

    `ArchiveStore.docs()` 로 세면 `_merge()` 가 `(시각, 화자, 본문)` 으로 한 번 더
    합친다. 그건 reader 쪽 판정이고, 이 파일은 writer 가 무엇을 썼는지만 본다.
    """
    return [
        line.strip()
        for path in sorted(root.rglob("raw/*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("> [")
    ]


def _lines(root) -> list[str]:
    return [writer.RAW_LINE_RE.match(ln).group("text").strip() for ln in _raw(root)]


def _coords(root) -> list[str]:
    return [
        writer.split_stamp(writer.RAW_LINE_RE.match(ln).group("ts"))[1]
        for ln in _raw(root)
    ]


# --- 서로 다른 좌표는 각각 남는다 --------------------------------------------------

def test_two_messages_in_one_minute_are_both_kept(tmp_path):
    """**이 시험이 이 수정의 이유다.**

    같은 분에 같은 사람이 같은 말을 두 번 했다. 좌표가 다르므로 다른 메시지다.
    하나로 합치면 사람이 두 번 말한 것이 한 번으로 남는다.
    """
    result = _ingest(tmp_path, [
        _msg("확인했습니다", source_ts="1759100000.000100"),
        _msg("확인했습니다", source_ts="1759100030.000200"),
    ])

    assert result.written == 2
    assert _lines(tmp_path) == ["확인했습니다", "확인했습니다"]
    assert _coords(tmp_path) == ["1759100000.000100", "1759100030.000200"]


def test_the_second_message_survives_a_separate_ingest(tmp_path):
    """한 번에 들어오든 나눠 들어오든 같아야 한다. 뒤엣것이 이미 있는 줄에 걸린다."""
    _ingest(tmp_path, [_msg("네", source_ts="1759100000.000100")])

    result = _ingest(tmp_path, [_msg("네", source_ts="1759100030.000200")])

    assert result.written == 1
    assert len(_lines(tmp_path)) == 2


def test_different_speakers_with_one_coordinate_are_kept(tmp_path):
    """한 메시지의 사람 줄과 첨부 줄은 좌표를 공유한다. 좌표만 보면 뒤엣것이 사라진다."""
    ts = "1759100000.000100"
    result = _ingest(tmp_path, [
        _msg("착공계 올립니다", source_ts=ts),
        _msg("[첨부:변환] 착공계.pdf (pdf, 120KB)", source_ts=ts),
    ])

    assert result.written == 2
    assert len(_lines(tmp_path)) == 2


# --- 같은 좌표의 재전달은 한 벌이다 ------------------------------------------------

def test_the_same_coordinate_twice_is_one_line(tmp_path):
    """Slack 재전달·소급 재실행이 같은 메시지를 다시 보낸다."""
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts="1759100000.000100")])

    result = _ingest(tmp_path, [_msg("착공계 올립니다", source_ts="1759100000.000100")])

    assert result.written == 0
    assert len(_lines(tmp_path)) == 1


def test_the_same_coordinate_twice_in_one_batch_is_one_line(tmp_path):
    result = _ingest(tmp_path, [
        _msg("착공계 올립니다", source_ts="1759100000.000100"),
        _msg("착공계 올립니다", source_ts="1759100000.000100"),
    ])

    assert result.written == 1


def test_an_edited_body_at_the_same_coordinate_is_a_new_line(tmp_path):
    """수정본은 다른 글자다. 같은 좌표라도 덮어쓰지 않고 따로 남긴다."""
    _ingest(tmp_path, [_msg("2월까지", source_ts="1759100000.000100")])

    result = _ingest(tmp_path, [_msg("3월까지", source_ts="1759100000.000100")])

    assert result.written == 1
    assert sorted(_lines(tmp_path)) == ["2월까지", "3월까지"]


# --- 좌표 없는 옛 줄과의 호환 ------------------------------------------------------

def test_a_coordinated_message_is_written_beside_an_old_uncoordinated_one(tmp_path):
    """좌표 없는 옛 줄 하나로는 **A 와 B 를 구분할 수 없다.**

    그 줄을 근거로 좌표 있는 새 줄을 흡수하면, 그 분의 같은 말이 전부 그 옛 줄에
    걸린다 — 두 번째 메시지가 영영 안 들어온다. 이 수정이 막으려던 것이 그것이다.

    그래서 좌표가 붙은 쪽을 **한 번** 더 쓴다. 같은 메시지가 좌표 있는 것과 없는
    것으로 공존하고, 읽을 때는 `store._merge()` 가 좌표 있는 쪽을 남긴다. 그 동작은
    이 수정 전부터 있던 계약이다.
    """
    _ingest(tmp_path, [_msg("착공계 올립니다")])

    result = _ingest(tmp_path, [_msg("착공계 올립니다", source_ts="1759100000.000100")])

    assert result.written == 1
    assert len(_raw(tmp_path)) == 2


def test_that_extra_line_is_written_only_once(tmp_path):
    """한 번만이어야 한다. 매번 늘면 소급을 돌릴 때마다 파일이 커진다."""
    _ingest(tmp_path, [_msg("착공계 올립니다")])
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts="1759100000.000100")])

    for _ in range(3):
        result = _ingest(tmp_path, [
            _msg("착공계 올립니다", source_ts="1759100000.000100"),
        ])
        assert result.written == 0
    assert len(_raw(tmp_path)) == 2


def test_an_old_line_does_not_duplicate_a_coordinated_one(tmp_path):
    """반대 순서도 같다. 소급이 좌표 없이 같은 줄을 다시 보낼 수 있다."""
    _ingest(tmp_path, [_msg("착공계 올립니다", source_ts="1759100000.000100")])

    result = _ingest(tmp_path, [_msg("착공계 올립니다")])

    assert result.written == 0
    assert len(_lines(tmp_path)) == 1


def test_two_uncoordinated_lines_still_collapse(tmp_path):
    """좌표가 둘 다 없으면 가를 근거가 없다. 전과 같이 한 줄로 둔다."""
    result = _ingest(tmp_path, [_msg("네"), _msg("네")])

    assert result.written == 1


def test_an_old_line_does_not_block_a_different_coordinate(tmp_path):
    """**옛 줄 하나가 그 분의 모든 같은 말을 막으면 안 된다.**

    「네」 는 한 회의에서 몇 번이고 나온다. 좌표 없는 옛 줄 하나 때문에 그 뒤의
    「네」 가 전부 사라지면, 사람이 한 말이 아카이브에 없는 상태가 된다.
    """
    _ingest(tmp_path, [_msg("네")])
    _ingest(tmp_path, [_msg("네", source_ts="1759100000.000100")])

    result = _ingest(tmp_path, [_msg("네", source_ts="1759100030.000200")])

    assert result.written == 1
    # 좌표 없는 옛 줄 1 + 좌표 A 1 + 좌표 B 1.
    assert len(_raw(tmp_path)) == 3
    assert _coords(tmp_path) == ["", "1759100000.000100", "1759100030.000200"]


# --- 판정 함수 자체 ----------------------------------------------------------------

def test_the_key_separates_two_coordinates():
    first = "> [2026-09-01 10:00|1759100000.000100] 김현장: 네"
    second = "> [2026-09-01 10:00|1759100030.000200] 김현장: 네"

    assert writer.dedupe_line(first) != writer.dedupe_line(second)


def test_the_key_is_stable_for_one_coordinate():
    line = "> [2026-09-01 10:00|1759100000.000100] 김현장: 네"

    assert writer.dedupe_line(line) == writer.dedupe_line(line)


def test_a_line_without_a_coordinate_keeps_the_old_key():
    """옛 키 모양이 바뀌면 이미 쌓인 줄과 안 맞아 전부 다시 들어온다."""
    line = "> [2026-09-01 10:00] 김현장: 네"

    assert writer.dedupe_line(line) == line


@pytest.mark.parametrize("line", ["", "원문이 아닌 줄", "## 원문"])
def test_a_line_that_is_not_raw_is_passed_through(line):
    assert writer.dedupe_line(line) == line.strip()
