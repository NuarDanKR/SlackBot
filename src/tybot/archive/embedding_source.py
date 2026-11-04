"""벡터 색인과 근거 복원이 **같은 본문**을 보게 하는 정본 창구 (B-12).

## 왜 창구가 하나여야 하나

벡터 DB 가 돌려주는 것은 근거가 아니라 **좌표**다(`embedding_search` 머리말).
그 좌표로 본문을 여는 자리가 둘이면 — 색인할 때 한 번, 답할 때 또 한 번 —
두 본문은 언젠가 반드시 갈라진다. 갈라져도 **오류는 안 난다.** 지워진 문장이
벡터로는 아직 살아 있어 답변에 실리고, 사람이 출처를 눌러 가면 그 자리에 그
문장이 없다. 그래서 이 모듈 하나가 두 경로에 같은 글자를 준다
(`canonical_text` · `source_revision`).

## 네 가지를 지킨다

1. **resolver 는 원본 MD 를 직접 열지 않는다.** `VisibleSources.open` 이
   `ArchiveStore` 를 거쳐 연다. 직접 열면 `visible_docs()` 의 권한 판정을
   건너뛰고, 그 길로 수정·삭제된 줄이 되살아난다
2. **ACL 판정 전에 요청받은 경로를 열지 않는다.** 문이 둘이다 — 첫 문은
   **파일을 안 여는 문**이다. 좌표가 「지금 이 사람에게 보이는 채널」 안에
   있는지 문자열만으로 보고, 그 다음에야 본문을 읽는다. 열고 나서 거르면
   「없는 파일」 과 「권한 없는 파일」 이 응답 시간으로 구분돼 존재가 샌다
3. **후보 하나가 첨부 전부를 깨우지 않는다.** 채널 원문은 묶어서 한 번,
   첨부는 **요청받은 그 가족만** 연다(`attachment_reader` 의 `paths=`)
4. **DM 은 본인 것만 연다.** `ArchiveStore._files` 가 애초에 남의 DM 을 안 읽고,
   여기서 `dm_user` 를 한 번 더 본다. 개인 기록은 막는 문이 둘이어야 한다

## 정본 본문 계약 (요구 6)

| 항목 | 값 |
|---|---|
| 인코딩 | UTF-8. `source_revision` 은 UTF-8 바이트의 SHA-256 소문자 16진수 |
| 줄바꿈 | `\\n` 만. CRLF·단독 CR 은 `\\n` 으로 바꾼다 |
| NUL | 제거한다(`text_normalize.remove_nul`). PostgreSQL `text` 가 못 받는다 |
| offset | **Python `str` 인덱스**(유니코드 코드포인트). 바이트도 자소도 아니다 |
| 채널·DM 한 줄 | `[<표시시각>] <화자>: <본문>\\n` |
| 첨부 한 줄 | 본문 그대로 + `\\n` (Markdown 표·제목을 살려 청크 경계에 쓴다) |
| 끝 | 줄이 하나라도 있으면 항상 `\\n` 으로 끝난다 |

본문은 **파일에서 다시 읽지 않는다.** `ArchiveStore` 가 ACL 과
`revision_reader` 를 적용하고 남긴 `RawLine` 으로만 만든다. 그래서 수정·삭제된
줄은 정본에 **없고**, 그 문서의 `source_revision` 이 달라져 옛 벡터는 전부
떨어진다(요구 2).

## 좌표 계약 (요구 5)

`(workspace, channel_id, doc_path)` 셋이 한 벌이다.

- `workspace` — 문서 프론트매터의 값
- `channel_id` — 병합된 채널 문서의 `channel_id`, 없으면 표시명
- `doc_path` — 아카이브 뿌리 기준 **파일** 상대 경로(POSIX).
  키워드 색인(`search_index.rel_path`)과 **같은 함수**를 쓴다

**문서는 파일 단위다.** `ArchiveStore._docs()` 가 일자 파일을 채널 하나로
합치지만, 합친 문서의 `path` 는 그중 최신 파일 하나라 날마다 바뀐다. 그걸
좌표로 쓰면 어제 색인한 행이 오늘 못 열린다. 그래서 합쳐서 권한·revision 을
적용한 뒤, 살아남은 줄을 **원래 파일별로 다시 묶어** 정본을 만든다.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from ..evidence_refs import safe_relative_path
from ..search_index import rel_path
from . import attachment_reader, revision_reader
from .store import ArchiveDoc, ArchiveStore
from .text_normalize import remove_nul

log = logging.getLogger("tybot.archive.embedding_source")

CHANNEL = "channel"
ATTACHMENT = "attachment"
DM = "dm"

#: 정본 계약의 판. 줄 모양·정규화 규칙을 바꾸면 올린다. 올리면 기존 벡터와 섞지
#: 않고 새 revision 으로 재색인해야 한다 — 같은 판에서만 본문이 같다고 말할 수 있다.
SOURCE_PLAN = "source-v1"
SEOUL = ZoneInfo("Asia/Seoul")


class SourceUnavailable(RuntimeError):
    """지금 상태에서는 색인용 정본을 만들 수 없다."""


@dataclass(frozen=True)
class VectorSource:
    """좌표 하나의 정본. 색인도 복원도 이 모양을 본다.

    `embedding_resolver.VisibleVectorSource` 가 읽는 네 칸
    (`workspace`·`channel_id`·`doc_path`·`text`)을 그대로 갖는다. 그쪽 타입을
    import 하지 않는 이유는 방향 때문이다 — 아카이브가 검색 모듈을 알면 안 된다.
    같은 모양인지는 시험이 붙들고 있다.
    """

    workspace: str
    channel_id: str
    doc_path: str
    kind: str
    text: str
    source_revision: str


@dataclass(frozen=True)
class LineSpan:
    """정본 본문 안에서 원문 한 줄이 차지한 자리. `end` 는 줄바꿈을 포함한다."""

    start: int
    end: int
    line: object

    def touches(self, start: int, end: int) -> bool:
        """`[start, end)` 구간과 한 글자라도 겹치나."""
        return self.start < end and self.end > start


def source_revision(text: str) -> str:
    """정본 본문의 지문. `embedding_index_store.source_revision` 과 같은 규칙이다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_chunker_revision(chunker_plan: str) -> str:
    """Bind canonical-text and chunk-boundary revisions into one DB value."""
    plan = str(chunker_plan or "").strip()
    if not plan or plan != chunker_plan:
        raise ValueError("chunker_plan must be non-empty and trimmed")
    return f"{SOURCE_PLAN}:{plan}"


def normalize(text: str) -> str:
    """저장소와 PostgreSQL 이 같이 받을 수 있는 모양으로 고른다.

    **원문 파일은 안 고친다.** 고르는 것은 여기서 만드는 파생 본문뿐이고,
    offset 은 고른 뒤의 문자열 기준이다.
    """
    if not text:
        return ""
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned, _ = remove_nul(flat)
    return cleaned


def canonical_lines(
    doc: ArchiveDoc, lines: Iterable, *, kind: str
) -> tuple[str, tuple[LineSpan, ...]]:
    """정본 본문과 **그 본문 안에서 각 원문 줄이 차지한 자리**.

    둘을 한 함수가 같이 만드는 이유는 출처 때문이다(원칙 2). 벡터가 돌려주는
    것은 문자 offset 구간뿐인데, 답변에 붙일 출처는 **원문 한 줄**이라야 한다.
    자리를 따로 계산하면 정본을 만드는 규칙과 자리를 세는 규칙이 갈리고, 갈려도
    오류가 안 난다 — 사람이 출처를 눌러 가면 그 자리에 그 문장이 없다.

    줄 순서는 파일 안 줄 번호다. 병합 문서의 정렬(시각 우선)을 그대로 쓰면 같은
    파일의 정본이 다른 파일의 내용에 따라 달라진다 — 좌표가 흔들린다.
    """
    ordered = sorted(lines, key=lambda line: (int(line.lineno), str(line.ts)))
    if kind == ATTACHMENT:
        # 첨부는 본문 그대로다. 줄마다 파일명을 붙이면 표·제목이 깨지고,
        # 청크 분할이 경계를 못 본다(`chunking` 의 표·제목 규칙).
        rows = [(line, str(line.text or "")) for line in ordered]
    else:
        rows = [
            (line, f"[{line.ts}] {line.speaker}: {line.text}") for line in ordered
        ]
    rows = [(line, row) for line, row in rows if row.strip()]
    if not rows:
        return "", ()
    _ = doc  # 서명을 좌표와 함께 읽히게 둔다. 본문은 줄에서만 나온다.
    parts: list[str] = []
    spans: list[LineSpan] = []
    at = 0
    for line, row in rows:
        # 줄마다 고른 뒤 이어 붙인다. 다 이어 붙인 뒤 고르면 같은 글자가 나오지만
        # **자리를 셀 수 없다** — NUL 하나가 빠질 때마다 뒤쪽 offset 이 통째로 밀린다.
        # 기존 계약은 `normalize("\n".join(rows) + "\n")` 이다. 경계의
        # 줄바꿈까지 함께 정규화해야 단독 CR 로 끝나는 줄이 CRLF 가 되어 줄바꿈
        # 하나로 남는다. `normalize(row) + "\n"` 이면 그 경우 두 줄이 된다.
        piece = normalize(row + "\n")
        parts.append(piece)
        spans.append(LineSpan(start=at, end=at + len(piece), line=line))
        at += len(piece)
    return "".join(parts), tuple(spans)


def canonical_text(doc: ArchiveDoc, lines: Iterable, *, kind: str) -> str:
    """한 파일의 정본 본문. **색인과 복원이 둘 다 이 함수를 부른다.**"""
    return canonical_lines(doc, lines, kind=kind)[0]


def doc_kind(doc: ArchiveDoc) -> str:
    """`channel` · `attachment` · `dm` 중 하나. **경로와 메타로만 판정한다.**"""
    if attachment_reader.is_attachment_doc(doc):
        return ATTACHMENT
    if getattr(doc, "dm_user", None):
        return DM
    return CHANNEL


def channel_key(doc: ArchiveDoc) -> str:
    """좌표의 둘째 칸. ID 가 없으면 표시명으로 내려간다(v1 원문)."""
    return str(doc.channel_id or doc.channel or "").strip()


def attachment_coordinates(doc_path: str) -> tuple[str, str] | None:
    """첨부 경로에서 **파일을 열지 않고** `(workspace, channel_id)` 를 읽는다.

    `attachment_reader._family_key` 와 같은 자리를 본다. 그쪽은 실제 경로를
    `resolve()` 하므로 요청받은 문자열에 쓸 수 없다 — 없는 경로를 만져 존재
    여부가 새는 자리가 바로 거기다.
    """
    parts = PurePosixPath(doc_path).parts
    if (
        len(parts) == 7
        and parts[0] == "workspaces"
        and parts[2] == "channels"
        and parts[4] == "attachments"
    ):
        return parts[1], parts[3].split("__", 1)[0]
    if len(parts) == 6 and parts[2] == "archive" and parts[3] == "attachments":
        return parts[0], parts[1].split("__", 1)[0]
    return None


def _by_file(doc: ArchiveDoc, root: Path) -> dict[str, list]:
    """병합 문서의 줄을 **원래 파일별로** 되돌린다."""
    grouped: dict[str, list] = {}
    for line in doc.raw_lines:
        rel = rel_path(line.source_path or doc.path, root)
        grouped.setdefault(rel, []).append(line)
    return grouped


@dataclass(frozen=True)
class SourceEntry:
    """정본 하나와 그것을 만든 문서·줄자리. **출처를 붙이려면 셋이 다 필요하다.**"""

    source: VectorSource
    doc: ArchiveDoc
    spans: tuple[LineSpan, ...]

    def lines_in(self, start: int, end: int) -> list[LineSpan]:
        """`[start, end)` 에 걸친 원문 줄들. 청크 하나가 여러 줄을 덮는다."""
        return [span for span in self.spans if span.touches(start, end)]


def _entries_of(doc: ArchiveDoc, root: Path) -> Iterator[SourceEntry]:
    kind = doc_kind(doc)
    channel = channel_key(doc)
    workspace = str(doc.workspace or "").strip()
    if not workspace or not channel:
        # 좌표가 없으면 색인하지 않는다. 가까운 채널로 밀어 넣으면 그 채널
        # 사람들이 본 적 없는 자료가 그 채널 근거가 된다.
        return
    for doc_path, lines in _by_file(doc, root).items():
        text, spans = canonical_lines(doc, lines, kind=kind)
        if not text:
            continue
        yield SourceEntry(
            source=VectorSource(
                workspace=workspace,
                channel_id=channel,
                doc_path=doc_path,
                kind=kind,
                text=text,
                source_revision=source_revision(text),
            ),
            doc=doc,
            spans=spans,
        )


def _sources_of(doc: ArchiveDoc, root: Path) -> Iterator[VectorSource]:
    for entry in _entries_of(doc, root):
        yield entry.source


def iter_index_sources(
    store: ArchiveStore,
    *,
    dm_scope: str = "",
    require_revision_db: bool = True,
    today: date | None = None,
) -> Iterator[VectorSource]:
    """색인기가 읽을 정본들. **답변 경로가 쓰는 것과 같은 함수로 만든다.**

    `dm_scope` 는 `ArchiveStore._files` 와 같은 뜻이다 — 기본은 DM 을 **안**
    읽는다. 켜면 개인 기록도 색인에 들어가고, 답할 때 여는 문은
    `VisibleSources` 하나뿐이다(본인만).

    `require_revision_db` 를 끄지 않는 이유: revision DB 를 못 보는 동안
    `revision_reader` 는 수정·삭제 표시가 붙은 좌표를 **통째로 감춘다**(막는 쪽이
    기본값). 그 상태로 색인하면 「그 문장이 원래 없는 문서」 가 정본으로 굳고,
    DB 가 돌아오면 본문이 달라져 그 문서의 벡터가 전부 떨어진다. 색인은 지금
    무엇이 현재 본문인지 알 수 있을 때만 돌아야 한다.
    """
    if dm_scope:
        raise SourceUnavailable(
            "DM 벡터 색인은 kind 분리 스키마가 생길 때까지 비활성이다"
        )
    if require_revision_db and not revision_reader.available():
        raise SourceUnavailable(
            "revision 표를 못 보는 동안에는 색인하지 않는다 — "
            "지금 본문이 무엇인지 모르는 채로 정본을 굳히게 된다"
        )
    root = Path(store.root)
    open_day = today or datetime.now(SEOUL).date()
    for doc in store.docs(dm_scope=dm_scope):
        for source in _sources_of(doc, root):
            if source.kind == CHANNEL and Path(source.doc_path).stem == open_day.isoformat():
                continue
            yield source


class VisibleSources:
    """한 요청의 권한으로 좌표를 열어 주는 창구.

    `embedding_resolver.resolve_candidates(..., open_visible=...)` 에 바로 넘길
    수 있다 — `open(workspace, channel_id, doc_path)` 한 벌이 그 계약이다.

    인스턴스 하나는 **한 요청**짜리다. 채널 원문은 처음 묻는 순간 한 번 모으고,
    첨부는 묻는 좌표마다 그 가족만 연다. 요청이 끝나면 버린다 — 들고 있으면
    권한이 바뀐 뒤에도 옛 판정으로 열어 준다.
    """

    def __init__(self, store: ArchiveStore, ctx) -> None:
        self._store = store
        self._ctx = ctx
        self._root = Path(store.root)
        self._channel_docs: list[ArchiveDoc] | None = None
        self._map: dict[tuple[str, str, str], VectorSource] | None = None
        self._channel_scope: frozenset[tuple[str, str]] = frozenset()
        self._attachments: dict[tuple[str, str, str], VectorSource | None] = {}
        # 연 좌표의 **문서와 줄자리**. 출처를 붙일 때 쓴다 — 다시 열지 않는다.
        # 줄 객체는 `ArchiveStore` 가 이미 들고 있는 것이라 사본이 아니다.
        self._entries: dict[tuple[str, str, str], SourceEntry] = {}

    # -- 채널·DM 원문 --------------------------------------------------------

    def _load_channels(self) -> dict[tuple[str, str, str], VectorSource]:
        if self._map is not None:
            return self._map
        # **권한 판정을 베끼지 않는다.** 여기서 따로 ACL 을 계산하면 판정이 두
        # 곳으로 갈라지고, 갈려도 오류가 안 난다(원칙 3). 첨부를 통째로 만들지
        # 않는 비공개 입구를 쓰는 이유는 요구 7 때문이다 — 후보 하나 때문에
        # 아카이브의 모든 첨부를 객체로 만들면 2026-11-02 OOM 과 같은 길이다.
        docs = self._store.visible_channel_docs(self._ctx)
        self._channel_docs = docs
        user = str(getattr(self._ctx, "user_id", "") or "")
        found: dict[tuple[str, str, str], VectorSource] = {}
        scope: set[tuple[str, str]] = set()
        for doc in docs:
            kind = doc_kind(doc)
            if kind == DM and str(doc.dm_user or "") != user:
                # 여기까지 올 수 없어야 한다(`_files` 가 남의 DM 을 안 읽는다).
                # 그래도 본다 — 개인 기록은 문이 둘이어야 하고, 하나가 조용히
                # 무너져도 다른 하나가 남아야 한다.
                log.warning("DM 원문이 본인 범위 밖에서 올라왔다 — 연결하지 않는다")
                continue
            for entry in _entries_of(doc, self._root):
                source = entry.source
                key = (source.workspace, source.channel_id, source.doc_path)
                found[key] = source
                self._entries[key] = entry
                if kind != DM:
                    scope.add((source.workspace, source.channel_id))
        self._channel_scope = frozenset(scope)
        self._map = found
        return found

    # -- 첨부 ----------------------------------------------------------------

    def _open_attachment(self, key: tuple[str, str, str]) -> VectorSource | None:
        if key in self._attachments:
            return self._attachments[key]
        self._attachments[key] = None  # 실패도 기억한다. 같은 좌표를 두 번 안 연다
        workspace, channel_id, doc_path = key

        # 문 1 — **파일을 안 여는 문.**
        derived = attachment_coordinates(doc_path)
        if derived is None or derived != (workspace, channel_id):
            return None
        if derived not in self._channel_scope:
            # 이 사람에게 보이는 채널이 아니다. 여기서 끝내야 「없는 첨부」 와
            # 「권한 없는 첨부」 가 구분되지 않는다.
            return None

        # 문 2 — 열고 나서 **같은 판정을 다시** 받는다.
        docs = attachment_reader.archive_docs(
            self._root,
            self._channel_docs or [],
            cache=self._store._attachments,
            paths={doc_path},
        )
        permitted = self._store.visible_predicate(self._ctx)
        for doc in docs:
            if not permitted(doc):
                continue
            for entry in _entries_of(doc, self._root):
                source = entry.source
                if (source.workspace, source.channel_id, source.doc_path) != key:
                    # 같은 가족의 **다른 판**이다. 첨부가 교체되면 지금 정본은
                    # 새 파일이고, 옛 경로는 더 이상 근거가 아니다.
                    continue
                self._attachments[key] = source
                self._entries[key] = entry
                return source
        return None

    # -- 창구 ----------------------------------------------------------------

    def open(
        self, workspace: str, channel_id: str, doc_path: str
    ) -> VectorSource | None:
        """좌표 하나를 **지금 권한으로** 연다. 못 열면 `None`.

        `None` 하나로 끝내는 이유는 resolver 와 같다 — 「없다」 와 「권한이
        없다」 를 구분해 돌려주면 벡터 캐시가 문서 존재 여부를 묻는 창구가 된다.
        """
        workspace = str(workspace or "").strip()
        channel_id = str(channel_id or "").strip()
        raw = str(doc_path or "")
        path = safe_relative_path(raw)
        # 들어온 글자 그대로여야 한다. 정규화해서 받아 주면 색인에 든 좌표와
        # 다른 문자열이 같은 문서를 열고, 그때부터 두 좌표가 같은 행을 가리킨다.
        if not path or path != raw or not workspace or not channel_id:
            return None
        key = (workspace, channel_id, path)
        found = self._load_channels().get(key)
        if found is not None:
            return found
        return self._open_attachment(key)

    def entry(
        self, workspace: str, channel_id: str, doc_path: str
    ) -> SourceEntry | None:
        """`open()` 과 **같은 문을 지난** 정본 + 문서 + 줄자리.

        출처를 붙이는 쪽이 쓴다. `open()` 을 먼저 부르므로 권한 판정이 한 번 더
        돌지 않고, 판정을 건너뛰지도 않는다 — 안 열린 좌표는 여기에도 없다.
        """
        if self.open(workspace, channel_id, doc_path) is None:
            return None
        return self._entries.get((workspace.strip(), channel_id.strip(), doc_path))
