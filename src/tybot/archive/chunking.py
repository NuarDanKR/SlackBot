"""순수 청크 분할 코어 — 임베딩 색인이 쓸 조각을 만든다 (B-12).

## 이 모듈이 하지 않는 것

DB·네트워크·모델 호출이 **없다.** 입력은 문자열 하나, 출력은 좌표가 붙은 조각
목록이다. 그래서 색인 스키마·임베딩 모델·ACL 이 바뀌어도 이 파일은 그대로 돌고,
시험이 실제 원문 없이 합성 문자열만으로 전부 돈다.

## 원문을 고치지 않는다 (원칙 1)

조각의 본문은 **입력의 정확한 조각**이다 — `text[chunk.start:chunk.end]` 와 글자
하나까지 같다. 공백을 다듬거나 NUL 을 빼거나 줄바꿈을 고르지 않는다. 그런 정리가
필요하면 **이 코어를 부르기 전에** 하고(`text_normalize.remove_nul`), 그 결과를
원문으로 삼아 좌표를 매긴다. 여기서 한 글자라도 손대면 `start`·`end` 가 가리키는
자리와 조각 본문이 어긋나고, 그 어긋남은 **오류 없이** 「원문에 없는 문장을 인용한
근거」로 나타난다.

## 경계를 먼저 본다

고정 길이로 조용히 자르면 표가 반 토막 나고 메시지 하나가 둘로 쪼개져, 사람이
확인하러 갔을 때 그 자리에 그 문장이 없다. 그래서 순서가 있다.

1. **제목** — `#` 줄은 언제나 새 조각을 연다
2. **메시지** — `> [ts] 화자: 본문` 한 줄은 쪼개지 않는다. 여러 줄을 한 조각에
   담되 중간에서 끊지 않는다
3. **표** — `|` 로 시작하는 줄 묶음은 행 경계에서만 끊는다. 표와 표 아닌 것을
   한 조각에 섞지 않는다
4. **긴 단일 행** — 위 경계가 하나도 없는 11,922자짜리 한 줄은 어쩔 수 없이
   쪼갠다. 이때만 **겹침**을 둬서 경계에 걸친 문장이 양쪽 조각에 다 남게 한다

## 덮는 범위

조각들은 본문을 **빠짐없이** 덮는다. 덮이지 않은 부분은 문서 앞뒤의 공백뿐이다
(`uncovered()` 로 확인할 수 있고 시험이 그걸 단언한다). 겹침은 긴 행을 쪼갠
자리에만 생기며, `restore()` 로 원문을 그대로 되살릴 수 있다.

줄 번호(`line_start`·`line_end`)는 **입력 문자열 기준 1부터**다. 파일의 일부
구간을 넘겼다면 부르는 쪽이 그 구간의 시작 줄을 더해 파일 좌표로 옮긴다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: 청크 판 식별자. 판이 바뀌면 기존 벡터와 섞지 않고 새 revision 으로 재색인한다
#: (B-12 완료 조건). 경계 규칙이나 자르는 자리를 바꾸면 이 값을 올린다.
PLAN_VERSION = "chunk-v1"

#: 상한이 이보다 작으면 겹침과 경계 탐색이 의미를 잃는다.
MIN_MAX_CHARS = 64

HEADING_RE = re.compile(r"^#{1,6}\s")
#: 아카이브 원문 한 줄. `store.RAW_LINE_RE` 와 같은 모양을 느슨하게 본다 —
#: 여기서 하는 일은 파싱이 아니라 **경계 판정**이라 화자·본문까지 볼 필요가 없다.
MESSAGE_RE = re.compile(r"^>\s*\[[^\]]+\]")
#: Markdown 표 행. 코드블록 안의 `|` 까지 표로 보지만, 그래도 행 경계에서만
#: 끊으므로 잘못 봐서 생기는 해가 없다.
TABLE_RE = re.compile(r"^\s{0,3}\|")
#: 줄 나누기는 `\n` 하나만 본다. `str.splitlines()` 는 U+2028·U+000B 까지
#: 줄로 보기 때문에, 그걸 쓰면 본문에 그 글자가 든 문서에서 좌표가 어긋난다.
LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")

BLANK = "blank"
HEADING = "heading"
MESSAGE = "message"
TABLE = "table"
TEXT = "text"

#: 긴 행을 자를 자리를 고를 때 보는 글자들. 앞쪽이 더 좋은 자리다.
SENTENCE_END = ".!?…。！？"
SOFT_BREAK = ",;:·、)]}》」』"

ZWJ = "‍"
VARIATION = range(0xFE00, 0xFE10)
REGIONAL = range(0x1F1E6, 0x1F200)


class SpecError(ValueError):
    """청크 판 설정이 성립하지 않는다."""


@dataclass(frozen=True)
class ChunkSpec:
    """청크 판. 값이 바뀌면 `plan` 이 바뀌고, 그게 재색인의 근거가 된다."""

    max_chars: int = 1200
    overlap: int = 120

    def __post_init__(self) -> None:
        if self.max_chars < MIN_MAX_CHARS:
            raise SpecError(f"max_chars 는 {MIN_MAX_CHARS} 이상이어야 한다: {self.max_chars}")
        if self.overlap < 0:
            raise SpecError(f"overlap 은 음수일 수 없다: {self.overlap}")
        # 겹침이 상한의 절반을 넘으면 한 번 자를 때마다 앞으로 가는 거리가 겹침보다
        # 짧아진다. 조각 수가 길이에 비례하지 않고 늘며, 같은 문장이 여러 벌
        # 색인돼 검색 결과가 자기 자신으로 채워진다.
        if self.overlap > self.max_chars // 2:
            raise SpecError(
                f"overlap 은 max_chars 의 절반을 넘을 수 없다: "
                f"{self.overlap} > {self.max_chars // 2}"
            )

    @property
    def plan(self) -> str:
        """저장해 둘 판 식별자. 모델·차원과 함께 적어 재색인 범위를 가린다."""
        return f"{PLAN_VERSION}:max={self.max_chars}:ov={self.overlap}"


DEFAULT_SPEC = ChunkSpec()


@dataclass(frozen=True)
class Chunk:
    """조각 하나. `text` 는 입력의 정확한 조각이다."""

    text: str
    start: int
    end: int
    kind: str
    line_start: int
    line_end: int
    #: 긴 단일 행을 쪼갠 경우의 조각 번호(0부터)와 총 개수. 안 쪼갰으면 (0, 1).
    part: int = 0
    parts: int = 1

    @property
    def split_line(self) -> bool:
        """경계가 없어 어쩔 수 없이 행 가운데를 자른 조각인가."""
        return self.parts > 1


@dataclass(frozen=True)
class _Line:
    start: int
    end: int
    no: int
    kind: str


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    kind: str
    lines: tuple[_Line, ...]

    @property
    def line_start(self) -> int:
        return self.lines[0].no

    @property
    def line_end(self) -> int:
        return self.lines[-1].no


def split(text: str, spec: ChunkSpec = DEFAULT_SPEC) -> list[Chunk]:
    """원문을 조각으로 나눈다. 같은 입력이면 언제나 같은 결과다."""
    if not text:
        return []
    blocks = _blocks(_scan(text))
    if not blocks:
        # 공백뿐인 본문. 색인할 것이 없다 — 빈 조각을 만들어 두면 검색이
        # 「근거를 찾았는데 내용이 없다」 는 상태를 갖게 된다.
        return []

    chunks: list[Chunk] = []
    open_: list[_Block] = []
    for block in blocks:
        if open_ and (
            not _joins(open_[0].kind, block.kind)
            or block.end - open_[0].start > spec.max_chars
        ):
            chunks.append(_merge(text, open_))
            open_ = []
        if block.end - block.start > spec.max_chars:
            # 블록 하나가 혼자서도 넘친다. 안에서 줄 경계로 쪼갠다.
            chunks.extend(_split_block(text, block, spec))
            continue
        open_.append(block)
    if open_:
        chunks.append(_merge(text, open_))
    return chunks


def restore(text: str, chunks: list[Chunk]) -> str:
    """조각들을 이어 붙여 **덮은 구간의 원문**을 되살린다.

    겹침은 한 번만 센다. 결과는 `text[chunks[0].start:chunks[-1].end]` 와 같아야
    하고, 그게 「조용히 잘라 먹지 않았다」 는 증거다.
    """
    if not chunks:
        return ""
    out: list[str] = []
    cursor = chunks[0].start
    for chunk in chunks:
        if chunk.end <= cursor:
            continue
        out.append(text[max(cursor, chunk.start) : chunk.end])
        cursor = chunk.end
    return "".join(out)


def uncovered(text: str, chunks: list[Chunk]) -> list[tuple[int, int]]:
    """어느 조각도 덮지 않은 자리. 문서 앞뒤 공백 말고는 비어 있어야 한다."""
    if not chunks:
        return [(0, len(text))] if text else []
    gaps: list[tuple[int, int]] = []
    if chunks[0].start > 0:
        gaps.append((0, chunks[0].start))
    cursor = chunks[0].end
    for chunk in chunks[1:]:
        if chunk.start > cursor:
            gaps.append((cursor, chunk.start))
        cursor = max(cursor, chunk.end)
    if cursor < len(text):
        gaps.append((cursor, len(text)))
    return gaps


# --- 줄과 블록 -------------------------------------------------------------


def _scan(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for no, m in enumerate(LINE_RE.finditer(text), start=1):
        body = m.group(0).rstrip("\r\n")
        lines.append(_Line(m.start(), m.end(), no, _classify(body)))
    return lines


def _classify(body: str) -> str:
    if not body.strip():
        return BLANK
    if HEADING_RE.match(body):
        return HEADING
    if MESSAGE_RE.match(body):
        return MESSAGE
    if TABLE_RE.match(body):
        return TABLE
    return TEXT


def _groups(prev: str, kind: str) -> bool:
    """앞 줄과 같은 블록으로 묶이나.

    제목과 메시지는 **그 자체가 경계**라 절대 묶이지 않는다. 표와 본문만 이어진다.
    """
    return prev == kind and kind in {TABLE, TEXT}


def _blocks(lines: list[_Line]) -> list[_Block]:
    blocks: list[_Block] = []
    cur: list[_Line] = []
    pending: list[_Line] = []  # 아직 어느 블록에도 안 붙은 앞쪽 빈 줄

    def flush() -> None:
        if cur:
            blocks.append(_Block(cur[0].start, cur[-1].end, cur[0].kind, tuple(cur)))
            cur.clear()

    for line in lines:
        if line.kind == BLANK:
            # 빈 줄은 **앞 블록의 꼬리**로 붙인다. 앞이 없으면 다음 블록의 머리가
            # 된다 — 어느 쪽이든 좌표에 구멍이 생기지 않는다.
            if cur:
                cur.append(line)
            else:
                pending.append(line)
            continue
        if cur and not _groups(cur[0].kind, line.kind):
            flush()
        if not cur and pending:
            cur.extend(pending)
            pending.clear()
        cur.append(line)
    flush()
    # 문서 끝에 남은 빈 줄은 버린다. 공백만 든 조각은 색인할 것이 없다.
    return blocks


def _joins(chunk_kind: str, block_kind: str) -> bool:
    """이미 열린 조각에 이 블록을 더 담아도 되나."""
    if block_kind == HEADING:
        return False  # 제목은 언제나 새 조각을 연다
    # 표는 표끼리만. 표와 문단을 한 조각에 섞으면 행 경계가 조각 경계와 어긋난다.
    return TABLE not in (chunk_kind, block_kind)


def _merge(text: str, blocks: list[_Block]) -> Chunk:
    start, end = blocks[0].start, blocks[-1].end
    return Chunk(
        text=text[start:end],
        start=start,
        end=end,
        kind=blocks[0].kind,
        line_start=blocks[0].line_start,
        line_end=blocks[-1].line_end,
    )


def _split_block(text: str, block: _Block, spec: ChunkSpec) -> list[Chunk]:
    """블록 안에서 **줄 경계로** 쪼갠다. 표라면 그게 곧 행 경계다."""
    out: list[Chunk] = []
    cur: list[_Line] = []

    def flush() -> None:
        if cur:
            out.append(
                Chunk(
                    text=text[cur[0].start : cur[-1].end],
                    start=cur[0].start,
                    end=cur[-1].end,
                    kind=block.kind,
                    line_start=cur[0].no,
                    line_end=cur[-1].no,
                )
            )
            cur.clear()

    for line in block.lines:
        if line.end - line.start > spec.max_chars:
            flush()
            out.extend(_split_line(text, line, block.kind, spec))
            continue
        if cur and line.end - cur[0].start > spec.max_chars:
            flush()
        cur.append(line)
    flush()
    return out


def _split_line(text: str, line: _Line, kind: str, spec: ChunkSpec) -> list[Chunk]:
    """경계가 없는 긴 한 줄을 **겹침을 두고** 쪼갠다.

    겹침이 있어야 자른 자리에 걸친 문장이 양쪽 조각에 다 남는다. 겹침이 없으면
    그 문장은 어느 조각에서도 온전히 읽히지 않고, 검색은 그걸 「없다」 고 답한다.
    """
    spans: list[tuple[int, int]] = []
    pos = line.start
    while pos < line.end:
        limit = min(pos + spec.max_chars, line.end)
        cut = line.end if limit >= line.end else _cut_point(text, pos, limit)
        spans.append((pos, cut))
        if cut >= line.end:
            break
        # 다음 조각은 **반드시 `cut` 이전에서 시작한다.** 글자 보호가 자리를 앞으로
        # 밀더라도 `cut` 을 넘기지 못하게 막는다 — 넘기면 그 사이가 어느 조각에도
        # 안 들어가고, 그 손실은 오류 없이 「원문에 없는 내용」 으로 나타난다.
        nxt = _safe_forward(text, max(pos + 1, cut - spec.overlap), cut)
        # 앞으로 못 가면 겹침을 포기한다. 멈추는 것보다는 낫다.
        pos = nxt if nxt > pos else cut
    total = len(spans)
    return [
        Chunk(
            text=text[a:b],
            start=a,
            end=b,
            kind=kind,
            line_start=line.no,
            line_end=line.no,
            part=i,
            parts=total,
        )
        for i, (a, b) in enumerate(spans)
    ]


def _cut_point(text: str, start: int, limit: int) -> int:
    """`limit` 에서 뒤로 조금 물러서며 자르기 좋은 자리를 찾는다."""
    floor = max(start + 1, limit - max(1, (limit - start) // 4))
    for probe in (_is_sentence_end, _is_space, _is_soft_break):
        for j in range(limit, floor - 1, -1):
            if probe(text, j):
                return _safe_back(text, j, floor)
    return _safe_back(text, limit, floor)


def _is_sentence_end(text: str, j: int) -> bool:
    return text[j - 1] in SENTENCE_END


def _is_space(text: str, j: int) -> bool:
    return text[j - 1].isspace()


def _is_soft_break(text: str, j: int) -> bool:
    return text[j - 1] in SOFT_BREAK


# --- 글자 가운데를 가르지 않기 ---------------------------------------------


def _unsafe(text: str, i: int) -> bool:
    """`i` 앞에서 자르면 글자 하나가 반으로 갈라지나.

    결합 문자·이모지 ZWJ 연결·변이 선택자·국기(지역 표시자 쌍)를 본다. 여기서
    갈라진 조각은 보기에도 깨지고, 임베딩에도 원문에 없던 글자로 들어간다.
    """
    if i <= 0 or i >= len(text):
        return False
    cur, prev = text[i], text[i - 1]
    if unicodedata.combining(cur):
        return True
    if cur == ZWJ or prev == ZWJ:
        return True
    if ord(cur) in VARIATION:
        return True
    return ord(cur) in REGIONAL and _regional_run_is_odd(text, i)


def _regional_run_is_odd(text: str, i: int) -> bool:
    """`i` 바로 앞까지 이어진 지역 표시자가 홀수 개인가.

    둘이 모여 국기 하나다. 「앞 글자도 지역 표시자면 위험」 으로 보면 국기가
    줄줄이 이어진 자리에서 **모든 위치가 위험**이 돼, 자를 자리를 찾아 앞뒤로
    끝까지 걷게 된다. 짝수면 바로 앞이 국기 경계이므로 안전하다.

    세는 범위에 상한을 두면 안 된다. 상한에서 끊긴 개수의 짝/홀은 **실제 짝/홀과
    다르고**, 그러면 한 쌍 가운데를 「안전하다」 고 답한다. 대신 짝수 자리가 늘
    안전하므로 부르는 쪽 루프는 한두 걸음에 멈춘다 — 긴 줄에서도 비용이 쌓이지
    않는다.
    """
    run = 0
    j = i - 1
    while j >= 0 and ord(text[j]) in REGIONAL:
        run += 1
        j -= 1
    return run % 2 == 1


def _safe_back(text: str, i: int, floor: int) -> int:
    while i > floor and _unsafe(text, i):
        i -= 1
    return i


def _safe_forward(text: str, i: int, ceiling: int) -> int:
    while i < ceiling and _unsafe(text, i):
        i += 1
    return min(i, ceiling)
