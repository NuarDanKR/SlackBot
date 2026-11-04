"""순수 청크 분할 코어 시험 (B-12).

**여기 쓰는 글은 전부 합성이다.** 실제 사내 원문·채널명·사람 이름을 넣지 않는다 —
시험 픽스처는 저장소에 남고, 저장소는 아카이브가 아니다(원칙 5).

시험이 지키는 것은 넷이다.

1. 조각 본문은 입력의 **정확한 조각**이다 (`text[start:end]`)
2. 조각들은 본문을 **빠짐없이** 덮고, 안 덮은 자리는 공백뿐이다
3. 경계(제목·메시지·표 행)를 가운데서 끊지 않는다
4. 같은 입력이면 **언제나 같은 결과**다
"""
from __future__ import annotations

import ast
import pathlib
import unicodedata
from itertools import pairwise

import pytest

from tybot.archive import chunking
from tybot.archive.chunking import Chunk, ChunkSpec, SpecError, restore, split, uncovered

SMALL = ChunkSpec(max_chars=200, overlap=40)
TINY = ChunkSpec(max_chars=64, overlap=16)


# --- 합성 자료 ---------------------------------------------------------------


def _messages(count: int, *, body: str = "합성 본문입니다") -> str:
    """아카이브 원문 모양의 짧은 메시지 묶음."""
    return "".join(
        f"> [2026-01-02 09:{i % 60:02d}:00 | 17676{i:05d}.000100] 화자{i % 7}: {body} {i}\n"
        for i in range(count)
    )


def _table(rows: int, *, cols: int = 4) -> str:
    head = "| " + " | ".join(f"칸{c}" for c in range(cols)) + " |\n"
    rule = "|" + "|".join(" --- " for _ in range(cols)) + "|\n"
    body = "".join(
        "| " + " | ".join(f"값{r}-{c}" for c in range(cols)) + " |\n" for r in range(rows)
    )
    return head + rule + body


def _long_line(length: int) -> str:
    """줄바꿈 없는 한 행. 실제 자료의 최대 길이(11,922자)를 재현한다."""
    unit = "합성 문장입니다. 숫자는 백이십삼이고 날짜는 이천이십육년입니다. "
    raw = (unit * (length // len(unit) + 2))[:length]
    assert len(raw) == length
    return raw


def _document() -> str:
    """제목·본문·표·메시지·긴 행이 다 든 한 장."""
    return (
        "# 합성 문서\n"
        "\n"
        "앞머리 문단입니다. 길지 않습니다.\n"
        "\n"
        "## 표\n"
        "\n" + _table(12) + "\n"
        "## 대화\n"
        "\n" + _messages(30) + "\n"
        "## 긴 행\n"
        "\n" + _long_line(11922) + "\n"
    )


def _lines(chunk: Chunk) -> list[str]:
    return [ln for ln in chunk.text.splitlines() if ln.strip()]


# --- 1. 좌표와 본문이 어긋나지 않는다 -----------------------------------------


def test_every_chunk_is_an_exact_slice_of_the_source():
    text = _document()
    for chunk in split(text, SMALL):
        assert chunk.text == text[chunk.start : chunk.end]
        assert chunk.start < chunk.end


def test_the_core_does_not_touch_the_input():
    """공백·NUL·CRLF 를 다듬지 않는다. 다듬는 것은 부르는 쪽 일이다."""
    text = "# 제목  \r\n\r\n본문에 NUL\x00 이 있고 끝에 공백이 있다   \r\n"
    before = text
    chunks = split(text, SMALL)

    assert text == before
    joined = restore(text, chunks)
    assert "\x00" in joined
    assert "\r\n" in joined
    assert "   \r\n" in joined


def test_a_unicode_line_separator_is_not_treated_as_a_line_break():
    """`str.splitlines()` 였다면 U+2028 에서 줄을 나눠 좌표가 밀렸을 자리다."""
    text = "앞 뒤 한 줄입니다.\n"
    chunks = split(text, SMALL)

    assert len(chunks) == 1
    assert chunks[0].line_start == chunks[0].line_end == 1
    assert chunks[0].text == text


def test_line_numbers_are_one_based_and_never_go_backwards():
    text = _document()
    chunks = split(text, SMALL)

    assert chunks[0].line_start == 1
    last = 0
    for chunk in chunks:
        assert 1 <= chunk.line_start <= chunk.line_end
        assert chunk.line_start >= last
        last = chunk.line_start
    assert chunks[-1].line_end <= len(text.split("\n"))


# --- 2. 빠짐없이 덮는다 -------------------------------------------------------


def test_the_body_is_restored_character_for_character():
    text = _document()
    chunks = split(text, SMALL)

    assert restore(text, chunks) == text[chunks[0].start : chunks[-1].end]


def test_what_is_not_covered_is_only_whitespace():
    text = "\n\n  " + _document() + "\n\n   \n"
    chunks = split(text, SMALL)

    for a, b in uncovered(text, chunks):
        assert text[a:b].strip() == ""


def test_an_empty_body_makes_no_chunks():
    assert split("", SMALL) == []
    assert restore("", []) == ""


@pytest.mark.parametrize("blank", ["\n", "\n\n\n", "   ", "  \n\t\n   \n", "\r\n\r\n"])
def test_a_blank_only_body_makes_no_chunks(blank):
    """공백뿐인 본문에 조각을 만들면 검색이 「내용 없는 근거」 를 갖게 된다."""
    chunks = split(blank, SMALL)

    assert chunks == []
    assert uncovered(blank, chunks) == [(0, len(blank))]


def test_no_chunk_is_empty_and_none_exceeds_the_cap():
    for spec in (TINY, SMALL, ChunkSpec()):
        for chunk in split(_document(), spec):
            # 공백만 든 조각은 「근거를 찾았는데 내용이 없다」 는 상태가 된다.
            assert chunk.text.strip()
            assert len(chunk.text) <= spec.max_chars


# --- 3. 경계를 가운데서 끊지 않는다 -------------------------------------------


def test_short_messages_are_never_cut_in_half():
    text = _messages(40)
    chunks = split(text, SMALL)

    assert len(chunks) > 1  # 상한에 걸려 실제로 나뉘었나
    for chunk in chunks:
        for line in _lines(chunk):
            assert chunking.MESSAGE_RE.match(line), line


def test_short_messages_pack_together_instead_of_one_each():
    """메시지 하나를 조각 하나로 만들면 문맥이 없어 검색이 못 쓴다."""
    text = _messages(40)
    chunks = split(text, SMALL)

    assert len(chunks) < 40
    assert max(chunk.line_end - chunk.line_start for chunk in chunks) >= 1


def test_a_table_is_cut_only_at_row_boundaries():
    text = _table(60)
    chunks = split(text, SMALL)

    assert len(chunks) > 1
    for chunk in chunks:
        for line in _lines(chunk):
            assert line.startswith("|") and line.endswith("|"), line
        assert chunk.kind == chunking.TABLE


def test_a_short_table_stays_whole():
    text = _table(3)
    chunks = split(text, ChunkSpec())

    assert len(chunks) == 1
    assert chunks[0].text == text


def test_a_table_is_not_mixed_with_the_text_around_it():
    text = "앞 문단입니다.\n" + _table(3) + "뒤 문단입니다.\n"
    chunks = split(text, ChunkSpec())

    kinds = [chunk.kind for chunk in chunks]
    assert chunking.TABLE in kinds
    for chunk in chunks:
        if chunk.kind == chunking.TABLE:
            assert all(line.startswith("|") for line in _lines(chunk))
        else:
            assert not any(line.startswith("|") for line in _lines(chunk))


def test_a_heading_always_opens_a_chunk():
    text = _document()
    chunks = split(text, SMALL)

    seen = 0
    for chunk in chunks:
        heads = [i for i, line in enumerate(_lines(chunk)) if chunking.HEADING_RE.match(line)]
        assert heads in ([], [0]), chunk.text[:80]
        seen += len(heads)
    assert seen == text.count("\n# ") + text.count("\n## ") + 1


def test_a_heading_keeps_the_text_under_it():
    text = "## 작은 제목\n\n그 아래 한 문단입니다.\n"
    chunks = split(text, ChunkSpec())

    assert len(chunks) == 1
    assert chunks[0].kind == chunking.HEADING
    assert "그 아래 한 문단입니다." in chunks[0].text


# --- 4. 긴 단일 행 (11,922자) -------------------------------------------------


def test_the_11922_char_line_is_split_with_overlap():
    body = _long_line(11922)
    text = body + "\n"
    chunks = split(text, ChunkSpec())

    assert len(chunks) > 1
    assert all(chunk.parts == len(chunks) for chunk in chunks)
    assert [chunk.part for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.split_line for chunk in chunks)
    # 한 줄이므로 줄 번호는 전부 같다 — 역추적은 offset 이 맡는다.
    assert {(chunk.line_start, chunk.line_end) for chunk in chunks} == {(1, 1)}


def test_the_long_line_is_not_silently_truncated():
    body = _long_line(11922)
    text = body + "\n"
    spec = ChunkSpec()
    chunks = split(text, spec)

    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)
    assert restore(text, chunks) == text
    assert uncovered(text, chunks) == []
    # 꼬리가 남아 있나 — 마지막 100자는 어느 조각엔가 통째로 들어 있어야 한다.
    assert any(body[-100:] in chunk.text for chunk in chunks)


def test_consecutive_parts_of_a_long_line_really_overlap():
    text = _long_line(11922) + "\n"
    spec = ChunkSpec()
    chunks = split(text, spec)

    for prev, cur in pairwise(chunks):
        shared = prev.end - cur.start
        assert 0 < shared <= spec.overlap
        assert text[cur.start : prev.end] == prev.text[-shared:]


def test_a_long_line_prefers_a_sentence_or_space_boundary():
    """문장 끝·공백에서 끊어야 조각 하나만 읽어도 말이 된다."""
    text = _long_line(11922) + "\n"
    chunks = split(text, ChunkSpec())

    cut_endings = [chunk.text[-1] for chunk in chunks[:-1]]
    assert all(ch.isspace() or ch in chunking.SENTENCE_END for ch in cut_endings)


def test_a_long_line_with_no_boundary_at_all_still_terminates():
    """공백도 문장부호도 없는 한 줄. 그래도 멈추고, 그래도 다 덮는다."""
    text = "가" * 5000
    spec = ChunkSpec(max_chars=100, overlap=10)
    chunks = split(text, spec)

    assert len(chunks) > 40
    assert all(len(chunk.text) <= spec.max_chars for chunk in chunks)
    assert restore(text, chunks) == text


def test_a_long_message_line_is_split_but_keeps_its_kind():
    text = f"> [2026-01-02 09:00:00 | 1767600000.000100] 화자0: {_long_line(3000)}\n"
    chunks = split(text, ChunkSpec())

    assert len(chunks) > 1
    assert all(chunk.kind == chunking.MESSAGE for chunk in chunks)
    assert restore(text, chunks) == text


def test_a_long_table_row_is_split_as_a_last_resort():
    row = "| " + _long_line(3000) + " |\n"
    text = _table(2) + row
    chunks = split(text, ChunkSpec())

    split_parts = [chunk for chunk in chunks if chunk.split_line]
    assert split_parts
    assert all(chunk.kind == chunking.TABLE for chunk in chunks)
    assert restore(text, chunks) == text


def test_overlap_zero_is_allowed_and_still_covers_everything():
    text = _long_line(11922) + "\n"
    spec = ChunkSpec(max_chars=500, overlap=0)
    chunks = split(text, spec)

    assert restore(text, chunks) == text
    for prev, cur in pairwise(chunks):
        assert cur.start == prev.end


# --- 5. Unicode --------------------------------------------------------------


EMOJI_FAMILY = "👨‍👩‍👧‍👦"
FLAG = "🇰🇷"
COMBINING = "éà"  # 결합 악센트
VARIATION = "✔️"


def test_graphemes_are_never_cut_in_half():
    unit = f"가족 {EMOJI_FAMILY} 국기 {FLAG} 결합 {COMBINING} 변이 {VARIATION} 끝. "
    text = unit * 400
    chunks = split(text, ChunkSpec(max_chars=120, overlap=20))

    assert len(chunks) > 50
    for chunk in chunks:
        assert not chunk.text.startswith("‍")
        assert not chunk.text.endswith("‍")
        assert not chunk.text.startswith("️")
        assert unicodedata.combining(chunk.text[0]) == 0, repr(chunk.text[:4])


def test_unicode_offsets_are_code_point_offsets():
    text = f"{EMOJI_FAMILY}{FLAG} 뒤에 오는 문장입니다.\n"
    chunks = split(text, ChunkSpec())

    assert len(chunks) == 1
    assert chunks[0].start == 0
    assert chunks[0].end == len(text)
    assert text[chunks[0].start : chunks[0].end] == text


def test_unicode_restores_exactly():
    text = (f"{EMOJI_FAMILY} {FLAG} {COMBINING} {VARIATION} 합성 문장입니다. " * 300) + "\n"
    chunks = split(text, ChunkSpec(max_chars=300, overlap=50))

    assert restore(text, chunks) == text


def test_a_flag_pair_survives_a_split():
    """지역 표시자 둘이 모여 국기 하나다. 가운데서 자르면 글자가 바뀐다."""
    text = (FLAG * 2000) + "\n"
    chunks = split(text, ChunkSpec(max_chars=100, overlap=20))

    assert restore(text, chunks) == text
    for chunk in chunks:
        assert chunk.text.count("\U0001f1f0") == chunk.text.count("\U0001f1f7")


def test_a_flag_run_at_an_odd_offset_is_not_cut_mid_pair():
    """공백도 문장부호도 없는 국기 줄. 여기서는 상한 자리가 곧 자르는 자리다.

    앞에 한 글자를 둬 국기가 **홀수 자리**에서 시작하게 만든다. 글자 보호가 없으면
    상한이 국기 한 쌍 가운데에 떨어져, 조각 안의 지역 표시자 수가 홀수가 된다 —
    그 조각은 원문에 없는 글자를 들고 있는 셈이다.
    """
    text = "가" + FLAG * 1000 + "\n"
    chunks = split(text, ChunkSpec(max_chars=100, overlap=20))

    for chunk in chunks:
        indicators = sum(1 for ch in chunk.text if ord(ch) in chunking.REGIONAL)
        assert indicators % 2 == 0, repr(chunk.text[:6])
    assert restore(text, chunks) == text


def test_grapheme_protection_can_never_open_a_gap():
    """글자 보호가 다음 조각의 시작을 **자른 자리 뒤로** 밀면 안 된다.

    밀리면 그 사이 글자가 어느 조각에도 안 들어가고, 손실은 오류 없이
    「원문에 없는 내용」 으로 나타난다. 국기가 줄줄이 이어진 줄에서 실제로 그랬다.
    """
    cases = (
        FLAG * 2000 + "\n",
        EMOJI_FAMILY * 500 + "\n",
        COMBINING * 3000 + "\n",
        # 결합 문자만 길게 이어진 자리. 보호가 「안전한 자리」를 찾아 앞으로 걷다
        # 자른 자리를 지나칠 수 있는 유일한 모양이라 따로 둔다.
        "가" + "́" * 500 + " 뒤 문장입니다.\n",
    )
    for text in cases:
        chunks = split(text, ChunkSpec(max_chars=100, overlap=20))

        assert uncovered(text, chunks) == []
        for prev, cur in pairwise(chunks):
            assert cur.start <= prev.end
        assert restore(text, chunks) == text


# --- 6. 결정론 ---------------------------------------------------------------


def test_the_same_input_gives_the_same_chunks():
    text = _document()

    assert split(text, SMALL) == split(text, SMALL)
    assert split(text, SMALL) == split(text, ChunkSpec(max_chars=200, overlap=40))


def test_the_result_does_not_depend_on_what_was_split_before():
    a, b = _document(), _messages(50)
    first = (split(a, SMALL), split(b, SMALL))
    second = (split(b, SMALL), split(a, SMALL))

    assert first[0] == second[1]
    assert first[1] == second[0]


def test_chunks_are_hashable_values():
    """값이어야 재색인이 「같은 조각인가」 를 비교로 판정할 수 있다."""
    chunks = split(_document(), SMALL)

    assert len(set(chunks)) == len(chunks)


# --- 7. 판(spec) 과 순수성 ----------------------------------------------------


def test_the_plan_identifies_the_chunk_version():
    assert ChunkSpec().plan.startswith(chunking.PLAN_VERSION)
    assert ChunkSpec(max_chars=800, overlap=100).plan != ChunkSpec().plan


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 10},
        {"max_chars": 1200, "overlap": -1},
        {"max_chars": 1200, "overlap": 601},
        {"max_chars": 200, "overlap": 200},
    ],
)
def test_a_spec_that_cannot_make_progress_is_refused(kwargs):
    with pytest.raises(SpecError):
        ChunkSpec(**kwargs)


def test_the_core_imports_nothing_that_touches_db_network_or_a_model():
    """순수 코어라는 말이 지켜지는지 **구문으로** 본다.

    주석으로만 적어 두면 다음 사람이 psycopg 하나를 들여오고, 그때부터 이 시험은
    DB 가 있는 곳에서만 도는 시험이 된다.
    """
    src = pathlib.Path(chunking.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "re", "unicodedata", "dataclasses"}
    for banned in ("open(", "psycopg", "requests", "urllib", "socket", "anthropic", "subprocess"):
        assert banned not in src
