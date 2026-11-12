"""채널×일 다이제스트 길잡이 — 질문이 맞는 날의 **원문**을 검색 후보에 더한다 (B-93 2단계 §7-4).

설계: `docs/design/channel-day-digest.md` §5.

```text
질문 낱말 ─▶ channel_day_digest(body+terms) 대조 — 요청자가 볼 수 있는 채널만
         ─▶ 상위 다이제스트 → 그 날의 일자 원문과 첨부 정본
         ─▶ 그 문서들 안에서 줄 점수 ─▶ 원문 줄만 돌려준다
```

지키는 선:
- **다이제스트 문장은 반환 값에 없다.** `SearchHit` 은 원문 줄뿐이다(원칙 1)
- 권한은 `ArchiveStore.visible_*` 하나가 정한다. 다이제스트 행이 있어도 지금 못 보는
  채널·문서는 열지 않는다(원칙 3)
- 원문 파일의 현재 해시가 다이제스트에 적힌 것과 다르면 **그 다이제스트는 이번에 쓰지
  않는다.** 표시(`stale_at`)는 배치가 한다 — Master 는 이 표를 읽기만 한다
- 실패하면 빈 목록이다. 길잡이 때문에 답변이 막히면 안 된다

줄 점수에는 질문 낱말에 **다이제스트의 같은 뜻 낱말(terms)** 을 더해 쓴다. 「재무목표」 를
물었는데 문서에 「경영목표」 로 적혀 있으면 질문 낱말만으로는 그 줄이 0점이다. terms 는
줄을 **고르는 데만** 쓰이고 답변에 실리지 않는다.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("tybot.digest_guide")

#: 한 질문에 여는 다이제스트(채널×일) 수.
MAX_DIGESTS = 5
#: 검색 결과 뒤에 더하는 원문 줄 상한.
MAX_EXTRA_HITS = 20
#: 한 문서에서 더하는 줄 상한(한 날이 자리를 독차지하지 않게).
PER_DOCUMENT = 4
MAX_FILTER_TOKENS = 12

#: 색인 식. `deploy/sql/channel_day_digest_schema.sql` 의 색인과 **글자까지 같아야** 한다.
#: 다르면 오류 없이 전체 스캔이 된다.
DIGEST_COMPACT_TEXT = "lower(replace(body || ' ' || array_to_string(terms, ' '), ' ', ''))"


def _connect():
    """Master 읽기 손잡이. 못 붙으면 `None` — 길잡이 없이 그냥 검색한다."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("다이제스트 길잡이 DB 에 붙지 못했습니다: %s", type(exc).__name__)
        return None


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def query_sql(tokens, channels) -> tuple[str, dict]:
    """보이는 채널 안에서, 낱말을 많이 맞힌 현재 다이제스트부터. 낱말이 없으면 빈 SQL."""
    words = list(dict.fromkeys(str(t).lower().replace(" ", "") for t in tokens if str(t).strip()))[
        :MAX_FILTER_TOKENS
    ]
    pairs = sorted({(str(w), str(c)) for w, c in channels if w and c})
    if not words or not pairs:
        return "", {}
    params: dict = {f"kw{i}": _like(w) for i, w in enumerate(words)}
    hit = " + ".join(f"({DIGEST_COMPACT_TEXT} LIKE %(kw{i})s)::int" for i in range(len(words)))
    any_hit = " OR ".join(f"{DIGEST_COMPACT_TEXT} LIKE %(kw{i})s" for i in range(len(words)))
    params["ws"] = [w for w, _ in pairs]
    params["ch"] = [c for _, c in pairs]
    params["limit"] = MAX_DIGESTS
    sql = f"""SELECT workspace, channel_id, day, terms, source_files, source_attachments,
                     ({hit}) AS score
                FROM channel_day_digest
               WHERE stale_at IS NULL
                 AND (workspace, channel_id) IN (SELECT * FROM unnest(%(ws)s::text[], %(ch)s::text[]))
                 AND ({any_hit})
               ORDER BY score DESC, day DESC
               LIMIT %(limit)s"""
    return sql, params


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _rel(path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def expand(store, ctx, query: str, *, conn=None, limit: int = MAX_EXTRA_HITS) -> list:
    """질문과 맞는 날의 원문 줄(현재 권한으로 연 것)을 돌려준다. 실패하면 빈 목록."""
    from . import search_index
    from .archive import attachment_reader
    from .archive.store import SearchHit

    tokens = search_index.tokens_of(query)
    if not tokens:
        return []
    root = Path(store.root)
    channel_docs = store.visible_channel_docs(ctx)
    channels = {(d.workspace, d.channel_id) for d in channel_docs if d.channel_id}
    sql, params = query_sql(tokens, channels)
    if not sql:
        return []
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        return []
    try:
        rows = _rows(handle, sql, params)
    except Exception as exc:  # noqa: BLE001 - 표가 아직 없는 설치도 여기로 온다
        logger.info("다이제스트 길잡이 조회 실패: %s", type(exc).__name__)
        return []
    finally:
        if owned:
            with contextlib.suppress(Exception):
                handle.close()

    # 원문 줄을 (파일 경로 → [(doc, line)]) 로. 이미 권한을 거친 문서만 들어 있다.
    by_file: dict[str, list] = {}
    for doc in channel_docs:
        for line in doc.raw_lines:
            rel = _rel(line.source_path or doc.path, root)
            if rel:
                by_file.setdefault(rel, []).append((doc, line))
    permitted = store.visible_predicate(ctx)

    hits: list = []
    for row in rows:
        files = row.get("source_files") or []
        atts = row.get("source_attachments") or []
        # 원문이 바뀌었으면 이 다이제스트는 쓰지 않는다(그날 전체를 열지 않는다).
        if any(_sha256(root / f.get("path", "")) != f.get("sha256") for f in files):
            logger.info(
                "다이제스트 원문이 바뀌어 건너뜀 ws=%s ch=%s day=%s",
                row.get("workspace"),
                row.get("channel_id"),
                row.get("day"),
            )
            continue
        line_tokens = list(
            dict.fromkeys(
                tokens
                + [t for term in (row.get("terms") or []) for t in search_index.tokens_of(term)]
            )
        )
        joined = " ".join(line_tokens)
        candidates: list = []
        for f in files:
            candidates.extend(by_file.get(f.get("path", ""), []))
        for a in atts:
            path = root / a.get("path", "")
            att = attachment_reader.load(path) if path.is_file() else None
            if att is None or att.revision != a.get("revision"):
                continue
            adoc = attachment_reader.as_archive_doc(att, root)
            if not permitted(adoc):
                continue
            candidates.extend((adoc, line) for line in adoc.raw_lines)
        scored = []
        for doc, line in candidates:
            score = search_index.score_line(line_tokens, joined, line.speaker, line.text)
            if score:
                scored.append(SearchHit(doc=doc, line=line, score=score))
        scored.sort(key=lambda h: -h.score)
        per: dict[str, int] = {}
        for hit in scored:
            key = str(hit.line.source_path or hit.doc.path)
            if per.get(key, 0) >= PER_DOCUMENT:
                continue
            per[key] = per.get(key, 0) + 1
            hits.append(hit)
    return hits[:limit]


__all__ = ["DIGEST_COMPACT_TEXT", "MAX_EXTRA_HITS", "expand", "query_sql"]
