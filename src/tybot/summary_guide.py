"""승인 요약을 **검색 길잡이로만** 쓴다 (B-56).

설계: [`docs/design/summary-review.md`](../../docs/design/summary-review.md) §반영은 별도 승인 요약만

## 무엇을 하는 모듈인가

검토자가 승인한 요약(`approved_summary_item`)은 사람이 확인한 파생 정보지만
**원문이 아니다.** 그래서 이 모듈은 승인 문장을 답변 근거로 넘기지 않는다.
질문과 맞는 승인 항목을 찾아 그 항목이 가리키는 **원문 좌표만** 꺼내고,
그 좌표를 요청자의 **현재 권한**으로 다시 열어 원문 줄을 돌려준다.

```text
질문 ─▶ 승인 요약과 대조(길잡이) ─▶ evidence_locator ─▶ 현재 ACL 로 원문 재개봉 ─▶ 원문 줄
                                                   └─ 못 열거나 어긋나면 버린다
```

## 왜 이렇게까지 하나

승인 문장을 그대로 근거로 쓰면 두 가지가 한꺼번에 무너진다.

1. **요약 재귀**(원칙 1) — 봇이 만든 문장을 사람이 한 번 승인했다고 원문이 되지
   않는다. 그 문장을 근거로 또 요약하면 한 번 잘못 읽은 숫자가 사실로 굳는다.
2. **권한**(원칙 3) — 승인은 그때 그 검토자의 권한으로 한 것이다. 지금 묻는
   사람이 그 채널을 볼 수 있다는 뜻이 아니다. 그래서 좌표는 매번 다시 연다.

## 지키는 선 셋

1. **판정은 한 곳씩.** ACL 은 `ArchiveStore.resolve_refs()` 가, 점수는
   `search_index.score_line()` 이 소유한다. 여기서 다시 적지 않는다.
2. **DB 를 못 보면 조용히 아무것도 더하지 않는다.** 길잡이는 검색을 **넓히는**
   장치라, 없으면 예전과 같은 결과가 나온다 — 답을 막지 않는다.
3. **어긋난 항목은 조용히 다른 문장으로 대체하지 않는다.** 근거 원문이 사라졌거나
   좌표가 바뀌었으면 그 승인은 `stale` 로 표시해 길잡이에서 제외한다.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger("tybot.summary_guide")

# 한 질문이 길잡이로 쓰는 승인 항목 수. 넓히면 질문과 느슨하게 맞은 항목이
# 근거 자리를 차지하고, 그건 검색을 넓힌 것이 아니라 흐린 것이다.
MAX_ITEMS = 5

# 길잡이로 **더해지는** 원문 줄 상한. 검색이 찾은 줄을 밀어내지 않을 만큼만.
MAX_EXTRA_HITS = 8

# 한 워크스페이스에서 한 번에 훑는 승인 항목 상한.
CANDIDATE_LIMIT = 500

STALE_SOURCE_MISSING = "source_missing"
STALE_EVIDENCE_MOVED = "evidence_moved"


@dataclass(frozen=True)
class GuideItem:
    """승인 요약 한 건과 그것이 가리키는 원문 좌표.

    `body` 는 **찾는 데만** 쓰고 답변 근거로는 넘기지 않는다.
    """

    candidate_id: str
    workspace: str
    channel_id: str
    body: str
    evidence_quote: str
    evidence_at: str
    evidence_author: str
    evidence_locator: str
    evidence_hash: str

    @property
    def file_name(self) -> str:
        """`2026-09-16.md:42` → `2026-09-16.md`."""
        name, _, _ = str(self.evidence_locator or "").rpartition(":")
        return name.strip()

    @property
    def line_no(self) -> int:
        _, _, tail = str(self.evidence_locator or "").rpartition(":")
        try:
            return int(tail)
        except ValueError:
            return 0


def _connect():
    """DB 손잡이. **못 붙으면 `None`** — 길잡이 없이 그냥 검색한다."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:  # noqa: BLE001 - 길잡이 실패가 검색을 막으면 안 된다
        logger.warning("승인 요약 길잡이 DB 에 붙지 못했습니다: %s", exc)
        return None


def _value(row, key: str) -> str:
    if isinstance(row, dict):
        return str(row.get(key) or "")
    return ""


def active_items(workspaces: str | Iterable[str], *, conn=None) -> list[GuideItem]:
    """길잡이로 쓸 수 있는 승인 항목.

    **승인이고, 대체되지 않았고, 근거가 어긋나지 않은 것만** 든다. 반려·보류·
    미응답 폐기 후보와 정정 모달 입력은 여기에 들어올 길이 없다 —
    `state='approved'` 가 그 문이고, 정정 문장(`correction`)은 아예 읽지 않는다.
    """
    if isinstance(workspaces, str):
        scopes = [workspaces.strip()] if workspaces.strip() else []
    else:
        scopes = sorted({str(value).strip() for value in workspaces if str(value).strip()})
    if not scopes:
        return []
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        return []
    try:
        with handle.cursor() as cur:
            cur.execute(
                """
                SELECT a.candidate_id, a.workspace, a.channel_id, a.body,
                       c.evidence_quote, c.evidence_at, c.evidence_author,
                       c.evidence_locator, c.evidence_hash
                  FROM approved_summary_item a
                  JOIN summary_review_candidate c ON c.id = a.candidate_id
                 WHERE a.workspace = ANY(%(workspaces)s)
                   AND a.superseded_at IS NULL
                   AND a.stale_at IS NULL
                   AND c.state = 'approved'
                   AND c.evidence_hash <> ''
                 ORDER BY a.approved_at DESC
                 LIMIT %(lim)s
                """,
                {"workspaces": scopes, "lim": CANDIDATE_LIMIT},
            )
            rows = list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 길잡이 없음으로 내려간다
        logger.warning("승인 요약을 읽지 못했습니다 — 길잡이 없이 검색합니다: %s", exc)
        return []
    finally:
        if owned:
            _close(handle)

    out: list[GuideItem] = []
    for row in rows:
        item = GuideItem(
            candidate_id=_value(row, "candidate_id"),
            workspace=_value(row, "workspace"),
            channel_id=_value(row, "channel_id"),
            body=_value(row, "body"),
            evidence_quote=_value(row, "evidence_quote"),
            evidence_at=_value(row, "evidence_at"),
            evidence_author=_value(row, "evidence_author"),
            evidence_locator=_value(row, "evidence_locator"),
            evidence_hash=_value(row, "evidence_hash"),
        )
        # 좌표가 없는 승인은 길잡이가 될 수 없다. 원문을 다시 열 방법이 없으면
        # 남는 것은 승인 문장뿐이고, 그 문장으로 답하는 것이 바로 원칙 1 위반이다.
        if item.body and item.file_name and item.line_no > 0:
            out.append(item)
    return out


def matched(query: str, items: list[GuideItem], *, limit: int = MAX_ITEMS) -> list[GuideItem]:
    """질문과 맞는 승인 항목. **점수는 검색과 같은 함수로** 낸다.

    따로 점수를 매기면 같은 질문에 길잡이와 검색이 다른 순서를 보고, 그건
    오류가 아니라 「어떤 날은 나오고 어떤 날은 안 나오는」 답으로 나타난다.
    """
    from . import search_index

    tokens = search_index.tokens_of(query)
    if not tokens or not items:
        return []
    scored = [
        (search_index.score_line(tokens, query, "", item.body), index, item)
        for index, item in enumerate(items)
    ]
    hot = [row for row in scored if row[0] > 0]
    hot.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in hot[:limit]]


def _sources(store, docs=None) -> dict[tuple[str, str, str], object]:
    """(워크스페이스, 채널 ID, 파일명) → 원문 파일 문서.

    **권한을 보지 않는다.** 여기서 하는 일은 「그 좌표에 아직 그 문장이 있나」 뿐이고,
    그건 요청자와 무관한 아카이브의 상태다. 요청자에게 보일지는 `resolve_refs()` 가
    정한다 — 두 판정을 한 자리에서 하면 권한 사고가 파일 정리처럼 보인다.
    """
    out: dict[tuple[str, str, str], object] = {}
    for doc in docs if docs is not None else store.source_docs():
        channel_id = str(getattr(doc, "channel_id", "") or "")
        if not channel_id:
            continue
        key = (str(getattr(doc, "workspace", "") or ""), channel_id, doc.path.name)
        out.setdefault(key, doc)
    return out


def _line_of(doc, item: GuideItem):
    """좌표가 가리키는 줄. 인용·시각·작성자가 그대로일 때만 돌려준다."""
    from .summary_review import _normalized

    line = next((ln for ln in doc.raw_lines if ln.lineno == item.line_no), None)
    if line is None:
        return None
    quote = _normalized(item.evidence_quote)
    if not quote or quote not in _normalized(line.text):
        return None
    if item.evidence_at and line.ts != item.evidence_at:
        return None
    if item.evidence_author and line.speaker != item.evidence_author:
        return None
    from .evidence_refs import content_hash

    if not item.evidence_hash or content_hash(line.ts, line.speaker, line.text) != item.evidence_hash:
        return None
    return line


def _ref_for(store, doc, item: GuideItem):
    from .evidence_refs import ARCHIVE_LINE, EvidenceRef, content_hash, safe_relative_path
    from .search_index import rel_path

    line = _line_of(doc, item)
    if line is None:
        return None
    path = safe_relative_path(rel_path(line.source_path or doc.path, store.root))
    if not path:
        return None
    return EvidenceRef(
        kind=ARCHIVE_LINE,
        workspace=str(getattr(doc, "workspace", "") or item.workspace),
        channel_id=str(getattr(doc, "channel_id", "") or item.channel_id),
        document_path=path,
        line_no=line.lineno,
        source_ts=line.ts,
        content_hash=content_hash(line.ts, line.speaker, line.text),
    )


def mark_stale(rows: list[tuple[str, str]], *, conn=None) -> int:
    """근거가 어긋난 승인을 길잡이에서 제외한다. 돌아온 값은 표시한 건수.

    **승인 이력을 지우지 않는다.** 누가 무엇을 승인했는지는 남고, 길잡이와
    「기존 승인 요약」 에서만 빠진다. 같은 사실이 새 원문으로 다시 수집되면 정상
    후보 생성 경로를 통해 다시 검토할 수 있다.
    """
    items = [(cid, reason) for cid, reason in rows if cid]
    if not items:
        return 0
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        return 0
    try:
        with handle.cursor() as cur:
            for candidate_id, reason in items:
                cur.execute(
                    """
                    UPDATE approved_summary_item
                       SET stale_at = now(), stale_reason = %s
                     WHERE candidate_id = %s AND stale_at IS NULL
                    """,
                    (reason, candidate_id),
                )
        handle.commit()
    except Exception as exc:  # noqa: BLE001 - 표시 실패가 답변을 막으면 안 된다
        logger.warning("승인 요약 stale 표시에 실패했습니다: %s", exc)
        return 0
    finally:
        if owned:
            _close(handle)
    logger.warning(
        "승인 요약 %d건을 길잡이에서 제외했습니다 (사유=%s)",
        len(items), "|".join(sorted({reason for _, reason in items})),
    )
    return len(items)


def _close(handle) -> None:
    try:
        handle.close()
    except Exception as exc:  # noqa: BLE001 - 닫기 실패로 답변을 막지 않는다
        logger.debug("길잡이 DB 손잡이를 닫지 못했습니다: %s", exc)


def expand(store, ctx, query: str, *, conn=None, limit: int = MAX_EXTRA_HITS) -> list:
    """질문과 맞는 승인 요약이 가리키는 **원문 줄**을 돌려준다.

    돌려주는 것은 `SearchHit` — 즉 지금 권한으로 다시 연 원문이다. 승인 문장은
    한 글자도 따라 나가지 않는다. 출처도 그 원문 줄의 것이 붙는다(원칙 2).
    """
    docs = store.source_docs()
    if getattr(ctx, "channel_id", "") or getattr(ctx, "channel", ""):
        workspaces = [getattr(ctx, "workspace", "")]
    else:
        workspaces = sorted({
            str(getattr(doc, "workspace", "") or "")
            for doc in docs
            if ctx.may_reach(str(getattr(doc, "workspace", "") or ""))
        })
    items = active_items(workspaces, conn=conn)
    if not items:
        return []
    picked = matched(query, items)
    if not picked:
        return []

    sources = _sources(store, docs)
    refs = []
    stale: list[tuple[str, str]] = []
    for item in picked:
        doc = sources.get((item.workspace, item.channel_id, item.file_name))
        ref = _ref_for(store, doc, item) if doc is not None else None
        if ref is None:
            # 원문이 사라졌거나 그 자리에 다른 문장이 있다. **비슷한 줄을 찾아
            # 대신 쓰지 않는다** — 사람이 승인한 문장과 답변의 근거가 갈린다.
            stale.append((
                item.candidate_id,
                STALE_SOURCE_MISSING if doc is None else STALE_EVIDENCE_MOVED,
            ))
            continue
        refs.append(ref)
    if stale:
        mark_stale(stale, conn=conn)
    if not refs:
        return []

    hits, dropped = store.resolve_refs(refs, ctx)
    logger.info(
        "승인 요약 길잡이 ws=%s 항목=%d 원문=%d stale=%d 제외=%s",
        getattr(ctx, "workspace", "-"), len(picked), len(hits), len(stale),
        "|".join(dropped) or "-",
    )
    return hits[:limit]


__all__ = [
    "MAX_EXTRA_HITS",
    "MAX_ITEMS",
    "GuideItem",
    "active_items",
    "expand",
    "mark_stale",
    "matched",
]
