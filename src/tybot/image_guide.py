"""사진 검색 길잡이 — AI 이미지 설명·OCR 전사로 **사진 좌표를 찾고 원문을 다시 연다** (B-81 후속).

## 흐름

```text
질문 ─▶ image_search_entry(설명·전사 투영)와 낱말 대조 ─▶ 사진 좌표(ws·채널·파일·원본 해시)
     ─▶ 직전 재검증: 투영 본문 PII · 원본 파일 존재·해시·사람 업로드(load_original)
     ─▶ 요청자의 현재 ACL(visible_docs)로 그 사진의 로컬 OCR 줄·채널 첨부 줄을 다시 연다
     ─▶ SearchHit(원문 줄)만 돌려준다
```

## 설명·전사 문장은 근거로 나가지 않는다

투영 본문(`[AI 이미지 설명·검색용]`·`[AI 텍스트 인식·검색용]`)은 **찾는 데만** 쓴다.
돌려주는 것은 아카이브의 원문 줄 — 그 사진의 첨부 정본에 수집 때 로컬 OCR 이 남긴
줄과, 채널 원문의 `[첨부:…] · id:F…` 줄 — 이다. 출처도 그 줄의 것이 붙는다(원칙 2).

그래서 Vision 모델이 실패·timeout·무료 한도 소진이면 투영 줄이 생기지 않을 뿐이고,
검색은 예전처럼 로컬 OCR 줄을 직접 찾는다. 수집은 이 경로와 무관하게 이미 끝났다.

## 지키는 선

1. **ACL 은 `ArchiveStore.visible_docs()` 가 정한다.** 여기서 다시 적지 않는다
2. **DB 를 못 보면 아무것도 더하지 않는다.** 길잡이는 검색을 넓히는 장치다
3. **원본을 다시 확인하지 못하면 더하지 않는다.** 원본이 없거나 바뀐 사진의 좌표로
   답하면 사람이 출처를 눌렀을 때 다른 것이 열린다
4. 로그에는 좌표·건수·코드만 남긴다. 투영 본문은 남기지 않는다
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tybot.image_guide")

#: 한 질문이 길잡이로 쓰는 사진 수.
MAX_IMAGES = 5
#: 사진 한 장에서 더하는 원문 줄 상한(첨부 줄 포함).
MAX_LINES_PER_IMAGE = 4
#: 길잡이로 **더해지는** 원문 줄 상한.
MAX_EXTRA_HITS = 8
#: 한 번에 훑는 투영 줄 상한.
CANDIDATE_LIMIT = 500
MAX_FILTER_TOKENS = 12
#: 원본 재확인에서 읽는 최대 크기. 정책의 전송 상한과 별개다 — 여기서는 보내지 않는다.
MAX_ORIGINAL_BYTES = 50 * 1024 * 1024

#: `archiver_image_transcript_schema.sql` 의 색인 식과 글자까지 같아야 한다.
ENTRY_COMPACT_BODY = "lower(replace(e.body, ' ', ''))"

DROP_PII = "entry_pii"
DROP_NO_ORIGINAL_DOC = "original_doc_missing"
DROP_ACL = "not_visible"

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


@dataclass(frozen=True)
class ImageEntry:
    """투영 한 줄. `body` 는 찾는 데만 쓴다."""

    kind: str
    workspace: str
    channel_id: str
    file_id: str
    original_sha256: str
    body: str
    provider: str = ""
    model: str = ""
    prompt_revision: str = ""
    status: str = ""

    @property
    def coordinate(self) -> tuple[str, str, str, str]:
        return (self.workspace, self.channel_id, self.file_id, self.original_sha256)


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - 길잡이 실패가 검색을 막으면 안 된다
        logger.warning("사진 길잡이 DB 에 붙지 못했습니다: %s", type(exc).__name__)
        return None


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def keyword_filter(tokens: Iterable[str] | None) -> tuple[str, dict]:
    """낱말 중 **하나라도** 든 줄만. 점수(`score_line`)가 0 보다 클 줄은 빠지지 않는다."""
    words = list(
        dict.fromkeys(str(t).lower().replace(" ", "") for t in (tokens or ()) if str(t).strip())
    )[:MAX_FILTER_TOKENS]
    if not words:
        return "", {}
    params = {f"kw{i}": _like(w) for i, w in enumerate(words)}
    clause = " OR ".join(f"{ENTRY_COMPACT_BODY} LIKE %(kw{i})s" for i in range(len(words)))
    return f"AND ({clause})", params


def active_entries(workspaces: Iterable[str], *, tokens, conn=None) -> list[ImageEntry]:
    """질문 낱말이 든 투영 줄. 좌표·종류마다 **가장 새 revision 하나**만."""
    scopes = sorted({str(w).strip() for w in workspaces if str(w).strip()})
    keyword_sql, keyword_params = keyword_filter(tokens)
    if not scopes or not keyword_sql:
        return []
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        return []
    try:
        with handle.cursor() as cur:
            cur.execute(
                """
                SELECT e.kind, e.workspace, e.channel_id, e.file_id, e.original_sha256,
                       e.body, e.provider, e.model, e.prompt_revision, e.status
                  FROM image_search_entry e
                 WHERE e.workspace = ANY(%(workspaces)s)
                   {keywords}
                 ORDER BY e.created_at DESC
                 LIMIT %(lim)s
                """.replace("{keywords}", keyword_sql),
                {"workspaces": scopes, "lim": CANDIDATE_LIMIT, **keyword_params},
            )
            rows = list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 길잡이 없음으로 내려간다
        logger.warning("사진 길잡이 투영을 읽지 못했습니다: %s", type(exc).__name__)
        return []
    finally:
        if owned:
            try:
                handle.close()
            except Exception:
                logger.debug("사진 길잡이 DB 를 닫지 못했습니다", exc_info=True)
    seen: set[tuple] = set()
    out: list[ImageEntry] = []
    for row in rows:
        entry = ImageEntry(
            **{
                key: str(row.get(key) or "")
                for key in (
                    "kind",
                    "workspace",
                    "channel_id",
                    "file_id",
                    "original_sha256",
                    "body",
                    "provider",
                    "model",
                    "prompt_revision",
                    "status",
                )
            }
        )
        key = (*entry.coordinate, entry.kind)
        if key in seen or not entry.body or not all(entry.coordinate):
            continue
        seen.add(key)
        out.append(entry)
    return out


def matched(query: str, entries: list[ImageEntry], *, limit: int = MAX_IMAGES) -> list[ImageEntry]:
    """질문과 맞는 사진. 점수는 검색과 **같은 함수**로 낸다. 좌표마다 최고 점수 하나."""
    from . import search_index

    tokens = search_index.tokens_of(query)
    if not tokens:
        return []
    best: dict[tuple, tuple[int, int, ImageEntry]] = {}
    for index, entry in enumerate(entries):
        score = search_index.score_line(tokens, query, "", entry.body)
        if score <= 0:
            continue
        current = best.get(entry.coordinate)
        if current is None or score > current[0]:
            best[entry.coordinate] = (score, index, entry)
    ranked = sorted(best.values(), key=lambda row: (-row[0], row[1]))
    return [entry for _, _, entry in ranked[:limit]]


def entry_is_clean(entry: ImageEntry) -> bool:
    """투영 본문을 **지금** 다시 본다. 저장 뒤 규칙이 바뀌었을 수 있다."""
    from .pii_screen import screen_line
    from .sensitive_mask import contains_sensitive

    return not (screen_line(entry.body) or contains_sensitive(entry.body))


def _attachment_paths(root: Path, entry: ImageEntry) -> list[Path]:
    """그 파일의 첨부 정본 revision 경로들. 두 배치(옛·shadow)를 다 본다."""
    from .archive.attachment_doc import _safe

    ws, ch, fid = _safe(entry.workspace), _safe(entry.channel_id), _safe(entry.file_id)
    legacy = root / "workspaces" / ws / "channels" / ch / "attachments" / fid
    paths = sorted(legacy.glob("*.md")) if legacy.is_dir() else []
    paths += sorted(root.glob(f"{ws}/{ch}__*/archive/attachments/{fid}/*.md"))
    return paths


def original_code(store, entry: ImageEntry) -> str:
    """원본이 **지금도 그 바이트로** 있나. 아니면 사유 코드(빈 문자열 = 확인됨).

    수집 정본(첨부 revision)에서 같은 해시의 문서를 찾고, 전송 경로와 **같은 로더**
    (`load_original`)로 경로·symlink·해시·사람 업로드를 다시 확인한다.
    """
    from .archive import attachment_reader
    from .archive.image_description_original import load_original
    from .archive.image_description_source import ImageCandidate

    root = Path(store.root)
    docs = [
        doc
        for doc in (attachment_reader.load(p) for p in _attachment_paths(root, entry))
        if doc and doc.workspace == entry.workspace and doc.channel_id == entry.channel_id
    ]
    if not docs:
        return DROP_NO_ORIGINAL_DOC
    # **가장 새 정본**이 기준이다. 옛 revision 중 해시가 맞는 것을 골라 쓰지 않는다 —
    # 그러면 재수집으로 바뀐 사진을 옛 설명으로 계속 찾게 된다. 해시 대조는 로더가 한다.
    doc = max(docs, key=lambda d: (d.converted_at or d.staged_at or "", d.revision))
    filetype = (doc.filetype or Path(doc.name or "").suffix.lstrip(".")).lower()
    candidate = ImageCandidate(
        workspace=entry.workspace,
        channel_id=entry.channel_id,
        file_id=entry.file_id,
        original_sha256=entry.original_sha256,
        filetype=filetype,
        mime=_MIME.get(filetype, ""),
    )
    loaded = load_original(
        candidate, archive_root=root, doc=doc, max_image_bytes=MAX_ORIGINAL_BYTES
    )
    return "" if loaded.ok else (loaded.error_code or "original_unverified")


def _is_attachment_of(doc, entry: ImageEntry) -> bool:
    path = Path(getattr(doc, "path", ""))
    return (
        getattr(doc, "workspace", "") == entry.workspace
        and (getattr(doc, "channel_id", "") or "") == entry.channel_id
        and path.parent.name == entry.file_id
        and path.parent.parent.name == "attachments"
    )


def reopen(docs, entry: ImageEntry, query: str) -> list:
    """요청자에게 **보이는** 문서에서 그 사진의 원문 줄을 다시 연다.

    - 첨부 정본(로컬 OCR 본문): 질문 점수가 높은 줄부터
    - 채널 원문의 첨부 줄(`· id:F…`): 사진이 올라온 자리. 출처가 Slack 메시지로 이어진다
    """
    from . import search_index
    from .archive.store import SearchHit

    tokens = search_index.tokens_of(query)
    marker = f"· id:{entry.file_id}"
    picked: list = []
    for doc in docs:
        if _is_attachment_of(doc, entry):
            scored = sorted(
                (
                    (
                        search_index.score_line(tokens, query, doc.channel, line.text),
                        line.lineno,
                        line,
                    )
                    for line in doc.raw_lines
                ),
                key=lambda row: (-row[0], row[1]),
            )
            for score, _, line in scored[: MAX_LINES_PER_IMAGE - 1]:
                picked.append(SearchHit(doc=doc, line=line, score=max(1, score)))
        elif (
            getattr(doc, "workspace", "") == entry.workspace
            and (getattr(doc, "channel_id", "") or "") == entry.channel_id
        ):
            for line in doc.raw_lines:
                if marker in line.text:
                    picked.append(SearchHit(doc=doc, line=line, score=1))
                    break
    return picked[:MAX_LINES_PER_IMAGE]


def expand(store, ctx, query: str, *, conn=None, limit: int = MAX_EXTRA_HITS) -> list:
    """질문과 맞는 사진의 **원문 줄**. 투영 문장은 한 글자도 따라 나가지 않는다."""
    from . import search_index

    tokens = search_index.tokens_of(query)
    if not tokens:
        return []
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        # DB 가 없으면 투영도 없다. 원문 문서를 읽기 전에 끝낸다.
        return []
    try:
        docs = store.visible_docs(ctx)
        workspaces = sorted({str(getattr(d, "workspace", "") or "") for d in docs} - {""})
        entries = active_entries(workspaces, tokens=tokens, conn=handle)
    finally:
        if owned:
            try:
                handle.close()
            except Exception:
                logger.debug("사진 길잡이 DB 를 닫지 못했습니다", exc_info=True)
    if not entries:
        return []
    picked = matched(query, entries)
    dropped: list[str] = []
    hits: list = []
    seen: set[tuple[str, int]] = set()
    for entry in picked:
        if not entry_is_clean(entry):
            dropped.append(DROP_PII)
            continue
        code = original_code(store, entry)
        if code:
            dropped.append(code)
            continue
        found = reopen(docs, entry, query)
        if not found:
            # 원본은 있으나 요청자가 그 채널을 볼 수 없다(또는 원문 줄이 없다).
            dropped.append(DROP_ACL)
            continue
        for hit in found:
            key = (str(hit.doc.path), hit.line.lineno)
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    logger.info(
        "사진 길잡이 ws=%s 투영=%d 사진=%d 원문=%d 제외=%s",
        getattr(ctx, "workspace", "-"),
        len(entries),
        len(picked),
        len(hits),
        "|".join(sorted(set(dropped))) or "-",
    )
    return hits[:limit]


__all__ = [
    "ENTRY_COMPACT_BODY",
    "MAX_EXTRA_HITS",
    "MAX_IMAGES",
    "ImageEntry",
    "active_entries",
    "entry_is_clean",
    "expand",
    "keyword_filter",
    "matched",
    "original_code",
    "reopen",
]
