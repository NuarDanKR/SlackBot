"""벡터 후보를 **원문 좌표**로 바꾼다 — 출처와 후속 질문의 입구 (B-12).

## 무엇을 돌려주나

벡터 DB 가 주는 것은 `(doc_path, start_offset, end_offset)` 이고, 창구
(`embedding_source.VisibleSources`)가 그 구간의 **정본 글자**를 돌려준다.
그런데 답변에 붙어야 하는 것은 글자가 아니다.

- **출처**(원칙 2) — `[조직명]업무명, 📄문서(날짜)`. 그건 `SearchHit.citation()`
  이 만든다. 즉 벡터 결과도 `SearchHit` 이 되어야 한다
- **후속 질문**(원칙 1) — "방금 그 문서 다시" 가 이어 가는 것은 답변 문장이
  아니라 `EvidenceRef` 다. 그 좌표를 매번 현재 권한으로 다시 연다

그래서 이 모듈이 하는 일은 하나다. **청크 구간 → 그 구간에 걸친 원문 줄들.**

## 청크 본문은 근거가 아니다

돌려주는 `SearchHit.line` 은 **원문 한 줄**이지 청크 조각이 아니다. 청크는
정본에서 잘라낸 파생물이라, 그걸 근거로 실으면 사람이 출처를 눌러 갔을 때
그 모양의 문장이 파일에 없다(원칙 1·2). 청크는 **어느 줄인지 고르는 데만**
쓰이고 여기서 버려진다.

## 점수는 매기지 않는다

`SearchHit.score` 는 `resolve_refs` 와 같은 `1` 이다. 키워드 점수와 섞는 일은
별도 결정이고(B-12 완료 조건: 재현율·지연·비용을 함께 재서 판단), 여기서
임의로 섞으면 그 측정 자체가 불가능해진다. 점수를 만드는 자리는 여전히
`search_index.score_line` 하나다.

## 사유 코드

업무 내용을 담지 않는 고정 낱말이라 그대로 로그에 남길 수 있다. 「없다」 와
「권한이 없다」 를 구분하지 않는 것도 resolver 와 같은 이유다 — 구분해 주면
벡터 캐시가 문서 존재 여부를 묻는 창구가 된다.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..evidence_refs import MAX_REFS, EvidenceRef, ref_from_hit
from .embedding_source import SourceEntry, VisibleSources
from .store import SearchHit

log = logging.getLogger("tybot.archive.embedding_evidence")

#: 청크 하나가 끌고 올 수 있는 원문 줄 상한. 상한이 없으면 긴 첨부 한 조각이
#: 참조 예산을 통째로 먹고, 그러면 후속 질문의 범위가 「채널 전체」와 다를 바 없다.
MAX_LINES_PER_CHUNK = 12

#: 벡터로 찾은 줄의 점수. `resolve_refs` 와 같은 값이다 — 결합은 별도 결정이다.
VECTOR_HIT_SCORE = 1

SOURCE_NOT_VISIBLE = "source_not_visible"
CHUNK_LINES_MISSING = "chunk_lines_missing"
CHUNK_LINES_CAPPED = "chunk_lines_capped"
NO_LINE_COORDINATE = "no_line_coordinate"
REF_LIMIT_REACHED = "ref_limit_reached"
SOURCE_CHANGED = "source_changed"


@dataclass(frozen=True)
class VectorEvidence:
    """벡터 후보에서 나온 **근거와 좌표.** 청크 본문은 들지 않는다."""

    hits: tuple[SearchHit, ...] = ()
    refs: tuple[EvidenceRef, ...] = ()
    dropped: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.hits)


def lines_for_chunk(
    entry: SourceEntry,
    start: int,
    end: int,
    *,
    per_chunk: int = MAX_LINES_PER_CHUNK,
) -> tuple[list, bool]:
    """청크 구간에 걸친 원문 줄들과 **상한에 잘렸는지**.

    경계에 반 줄만 걸쳐도 그 줄을 집는다. 반만 걸쳤다고 버리면 문장이 두 조각에
    나뉜 자리에서 근거가 사라지고, 그 자리는 하필 겹침을 둔 긴 행이다.
    """
    picked = entry.lines_in(start, end)
    if per_chunk > 0 and len(picked) > per_chunk:
        return picked[:per_chunk], True
    return picked, False


def evidence_for(
    resolved: Iterable,
    sources: VisibleSources,
    root: Path | str,
    *,
    limit: int = MAX_REFS,
    per_chunk: int = MAX_LINES_PER_CHUNK,
) -> VectorEvidence:
    """복원된 청크들 → `SearchHit` + `EvidenceRef`.

    `resolved` 는 `embedding_resolver.Resolution.resolved` 다. 그 타입을 import
    하지 않는 이유는 방향이다 — 아카이브가 검색 모듈을 알면 안 된다. 읽는 칸은
    `.candidate` 의 `workspace` · `channel_id` · `doc_path` ·
    `start_offset` · `end_offset` 다섯뿐이다.

    `sources` 는 **그 후보들을 이미 연 창구**여야 한다. 여기서 다시 열지 않으므로
    권한 판정이 두 번 돌지 않고, 건너뛰지도 않는다 — 안 열린 좌표는 창구에
    없으니 그대로 빠진다.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REFS:
        raise ValueError(f"limit must be between 1 and {MAX_REFS}")
    if (
        isinstance(per_chunk, bool)
        or not isinstance(per_chunk, int)
        or not 1 <= per_chunk <= MAX_LINES_PER_CHUNK
    ):
        raise ValueError(f"per_chunk must be between 1 and {MAX_LINES_PER_CHUNK}")
    hits: list[SearchHit] = []
    refs: list[EvidenceRef] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, int]] = set()
    chunks = 0

    def drop(code: str) -> None:
        if code not in dropped:
            dropped.append(code)

    for item in resolved or ():
        chunks += 1
        candidate = getattr(item, "candidate", None)
        if candidate is None:
            drop(SOURCE_NOT_VISIBLE)
            continue
        entry = sources.entry(
            str(getattr(candidate, "workspace", "") or ""),
            str(getattr(candidate, "channel_id", "") or ""),
            str(getattr(candidate, "doc_path", "") or ""),
        )
        if entry is None:
            # 창구가 안 연 좌표다. 복원된 뒤 권한이 바뀌었거나, 애초에 이 창구로
            # 복원한 것이 아니다. 둘 다 근거로 쓰면 안 된다.
            drop(SOURCE_NOT_VISIBLE)
            continue
        start = int(getattr(candidate, "start_offset", 0) or 0)
        end = int(getattr(candidate, "end_offset", 0) or 0)
        current = entry.source
        resolved_text = getattr(item, "text", None)
        if not 0 <= start < end <= len(current.text):
            drop(CHUNK_LINES_MISSING)
            continue
        if current.source_revision != str(
            getattr(candidate, "source_revision", "") or ""
        ):
            drop(SOURCE_CHANGED)
            continue
        current_chunk = current.text[start:end]
        expected_hash = str(getattr(candidate, "text_sha256", "") or "")
        if (
            not isinstance(resolved_text, str)
            or current_chunk != resolved_text
            or hashlib.sha256(current_chunk.encode("utf-8")).hexdigest() != expected_hash
        ):
            drop(SOURCE_CHANGED)
            continue
        picked, capped = lines_for_chunk(entry, start, end, per_chunk=per_chunk)
        if capped:
            drop(CHUNK_LINES_CAPPED)
        if not picked:
            # 구간이 정본 밖이거나 빈 구간이다. resolver 가 이미
            # `chunk_bounds_changed` 로 거르지만, 그 문이 하나뿐이면 안 된다.
            drop(CHUNK_LINES_MISSING)
            continue
        for span in picked:
            if len(refs) >= limit:
                drop(REF_LIMIT_REACHED)
                break
            hit = SearchHit(doc=entry.doc, line=span.line, score=VECTOR_HIT_SCORE)
            ref = ref_from_hit(hit, root)
            if ref is None:
                # 채널 ID 가 없으면 다음 요청에서 범위를 확인할 수 없다.
                # 확인할 수 없는 참조는 남기지 않는다(막는 쪽이 기본값).
                drop(NO_LINE_COORDINATE)
                continue
            key = (ref.workspace, ref.document_path, ref.line_no)
            if key in seen:
                # 긴 행은 겹침을 두고 쪼개지므로 같은 줄이 두 청크에 들어 있다.
                # 출처가 두 번 붙으면 사람은 두 군데서 확인된 것으로 읽는다.
                continue
            seen.add(key)
            hits.append(hit)
            refs.append(ref)
        if len(refs) >= limit:
            drop(REF_LIMIT_REACHED)
            break

    # 수와 사유만 남긴다. 경로·본문·질문은 적지 않는다.
    log.info(
        "벡터 근거 좌표 복원 청크=%d 줄=%d 제외=%s",
        chunks,
        len(hits),
        "|".join(dropped) or "-",
    )
    return VectorEvidence(tuple(hits), tuple(refs), tuple(dropped))
