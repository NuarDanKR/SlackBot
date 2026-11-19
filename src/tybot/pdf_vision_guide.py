"""PDF 쪽 Vision 전사를 **답변 근거**로 붙인다 (B-80 마지막 단계).

## 흐름

```text
질문 ─▶ pdf_page_search_entry(성공 전사 투영)와 낱말 대조 ─▶ 쪽 좌표
        (ws·채널·file·page·original_sha256·revision_id)
     ─▶ 직전 재검증
        1. 요청자의 **현재** 권한: 그 채널이 visible 이고, 정본 문서도 같은 판정을 통과
           (VIPBot `evidence_sources`·DM·워크스페이스 경계는 `can_access` 한 곳이 정한다)
        2. 현재 첨부 정본(AttachmentDoc)의 file_id·original_sha256 이 투영 좌표와 같다
        3. 원본 PDF 가 수집 규칙의 자리에 하나만 있고, 바이트 해시가 같다
        4. 전사 본문을 **지금의** PII 규칙으로 다시 본다
     ─▶ `[AI 문서 전사·파생 · PDF N쪽]` 표시를 단 근거 줄(`VisionPageHit`)
```

## 이미지 길잡이(`image_guide`)와 다른 점

이미지 설명은 찾는 데만 쓰고 원문 줄을 돌려준다. PDF 쪽 전사는 그 쪽의 **유일한
본문**이라(로컬 추출·OCR 이 실패한 쪽만 보냈다) 돌려줄 원문 줄이 없다. 그래서 전사
문장을 근거로 쓰되, 줄 앞에 파생 표시를 박고 출처를 원본 PDF 의 그 쪽으로 붙인다.
사람이 출처를 눌러 원본 쪽과 대조할 수 있어야 한다(원칙 2).

`extracted` 만 근거다. `uncertain` 은 투영에 있어도 쓰지 않는다 — 같은 쪽의 **가장 새
revision** 이 extracted 가 아니면 옛 extracted 로 되돌아가지도 않는다. 실패·보류 revision 은
투영에 **본문 없는 묘비**로 들어오고(`VisionStore._PROJECT`), 그것이 최신 판이면 그 쪽은
최초 검색·후속 질문 양쪽에서 빠진다. 판의 순서는 투영 시각이 아니라 원장 revision 시각이다.

## 후속 질문

참조(`EvidenceRef`, kind=`vision_page`)에는 revision_id·file_id·page·original_sha256
좌표만 남는다. 전사 문장은 감사 기록에 복제하지 않는다. 다음 질문에서 DB 를 다시 읽고
위 재검증을 **처음부터 다시** 한다.

## 「사람 업로드 확인」 은 요구하지 않는다 (오너 결정 2026-11-19)

이미지 설명(B-81)은 정본의 `human_upload_confirmed` 가 참일 때만 쓴다. PDF 쪽 전사는 요구하지
않는다. 그 값은 실시간 수집이 이벤트에서 확인했을 때만 참이라, 변환 앱 PDF(사람이 올린 HWP 의
변환본)·옛 문서·파일 목록 소급은 모두 거짓이다. 요구하면 정확히 텍스트로 만들려던 문서 대부분이
빠진다. 봇 출력 되먹임은 **수집 입구**가 막는다 — 봇·앱 메시지는 버리고 지정 변환 앱의 PDF 만
받는다(`collect`, `archive.converter_bots`).

## 실패는 닫힌다

DB 를 못 보면 아무것도 더하지 않는다(기존 검색 그대로). 투영이 사라졌거나 좌표가
어긋났거나 권한이 바뀌었으면 그 쪽은 빠진다. 로그에는 좌표·건수·코드만 남긴다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .archive.store import RawLine, SearchHit

logger = logging.getLogger("tybot.pdf_vision_guide")

LABEL = "AI 문서 전사·파생"
STATUS_EXTRACTED = "extracted"

#: 한 질문이 더하는 쪽 수.
MAX_PAGES = 4
#: 쪽 하나에서 근거로 싣는 글자 수(표시 머리 제외). 쪽 전체가 20,000자일 수 있다.
MAX_PAGE_CHARS = 1500
#: 한 번에 훑는 투영 줄 상한.
CANDIDATE_LIMIT = 300
MAX_FILTER_TOKENS = 12
#: 원본 재확인에서 읽는 PDF 최대 크기.
MAX_ORIGINAL_BYTES = 200 * 1024 * 1024

#: `archiver_vision_schema.sql` 의 색인 식과 같아야 한다.
ENTRY_COMPACT_BODY = "lower(replace(e.body, ' ', ''))"

DROP_DB = "vision_db_unavailable"
DROP_PROJECTION = "projection_missing"
DROP_STATUS = "not_extracted"
DROP_PII = "entry_pii"
DROP_ACL = "permission_changed"
DROP_DOC = "attachment_doc_missing"
DROP_COORDINATE = "coordinate_mismatch"
DROP_ORIGINAL = "original_unverified"

_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_DOC_STATES = frozenset({"succeeded", "partial", "failed"})


@dataclass(frozen=True)
class PageEntry:
    """투영 한 줄."""

    revision_id: str
    workspace: str
    channel_id: str
    file_id: str
    page_number: int
    original_sha256: str
    status: str
    body: str
    created_at: str = ""

    @property
    def page_key(self) -> tuple[str, str, str, str, int]:
        return (self.workspace, self.channel_id, self.file_id, self.original_sha256,
                self.page_number)


@dataclass(frozen=True)
class VisionPageHit(SearchHit):
    """전사 쪽 하나를 담은 근거 줄. 좌표를 같이 들고 다닌다(후속 질문용)."""

    revision_id: str = ""
    file_id: str = ""
    page_number: int = 0
    original_sha256: str = ""
    pdf_name: str = ""

    def citation(self, *, with_workspace: bool = False) -> str:
        """출처는 **원본 PDF 의 그 쪽**이다. 파생 표시를 지우지 않는다."""
        from .channel_lifecycle import mark_for
        from .channels import source_label

        date = self.line.ts.split()[0] if self.line.ts else ""
        tail = f" ({self.doc.workspace})" if with_workspace else ""
        tail += mark_for(self.doc.workspace, self.doc.channel_id, self.doc.channel)
        name = self.pdf_name or self.file_id
        return (
            f"{source_label(self.doc.channel)}{tail}, 📄{name} {self.page_number}쪽({date})"
            f" · [{LABEL}]"
        )


def label_for(page_number: int) -> str:
    return f"[{LABEL} · PDF {page_number}쪽]"


# ── DB ────────────────────────────────────────────────────────────────────


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - 길잡이 실패가 검색을 막으면 안 된다
        logger.warning("PDF 전사 길잡이 DB 에 붙지 못했습니다: %s", type(exc).__name__)
        return None


def _close(handle) -> None:
    try:
        handle.close()
    except Exception:
        logger.debug("PDF 전사 길잡이 DB 를 닫지 못했습니다", exc_info=True)


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def keyword_filter(tokens: Iterable[str] | None) -> tuple[str, dict]:
    words = list(
        dict.fromkeys(str(t).lower().replace(" ", "") for t in (tokens or ()) if str(t).strip())
    )[:MAX_FILTER_TOKENS]
    if not words:
        return "", {}
    params = {f"kw{i}": _like(w) for i, w in enumerate(words)}
    clause = " OR ".join(f"{ENTRY_COMPACT_BODY} LIKE %(kw{i})s" for i in range(len(words)))
    return f"AND ({clause})", params


_COLUMNS = """e.revision_id, e.workspace, e.channel_id, e.file_id, e.page_number,
              e.original_sha256, e.status, e.body, e.created_at"""


def _stamp(value) -> str:
    """판 순서 비교용 시각. datetime 은 UTC 마이크로초까지 고정 폭으로 — 문자열 비교가
    시간 순서와 같아야 한다(세션 시간대·소수 자릿수에 흔들리지 않게)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return str(value or "")


def _entry(row) -> PageEntry | None:
    try:
        entry = PageEntry(
            revision_id=str(row.get("revision_id") or ""),
            workspace=str(row.get("workspace") or ""),
            channel_id=str(row.get("channel_id") or ""),
            file_id=str(row.get("file_id") or ""),
            page_number=int(row.get("page_number") or 0),
            original_sha256=str(row.get("original_sha256") or ""),
            status=str(row.get("status") or ""),
            body=str(row.get("body") or ""),
            created_at=_stamp(row.get("created_at")),
        )
    except (TypeError, ValueError):
        return None
    if (
        not entry.revision_id or not entry.workspace or not entry.channel_id
        or not entry.file_id or entry.page_number <= 0
        or not _HEX64.match(entry.original_sha256)
    ):
        return None
    return entry


def _latest_per_page(entries: Iterable[PageEntry]) -> list[PageEntry]:
    """쪽마다 **가장 새 revision 하나**. 그것이 extracted 가 아니면 그 쪽은 없다."""
    newest: dict[tuple, PageEntry] = {}
    for entry in entries:
        current = newest.get(entry.page_key)
        if current is None or (entry.created_at, entry.revision_id) > (
            current.created_at, current.revision_id
        ):
            newest[entry.page_key] = entry
    return [e for e in newest.values() if e.status == STATUS_EXTRACTED and e.body]


def active_entries(scopes: set[tuple[str, str]], *, tokens, conn) -> list[PageEntry]:
    """요청자가 볼 수 있는 채널 범위 안의, 질문 낱말이 든 투영 줄.

    예외를 올린다 — 호출자가 「DB 장애 = 아무것도 더하지 않음」 으로 닫는다.
    """
    keyword_sql, keyword_params = keyword_filter(tokens)
    if not scopes or not keyword_sql:
        return []
    # 워크스페이스·채널을 따로 ANY 로 걸면 (A,ch2)·(B,ch1) 같은 **보이지 않는 조합**이 후보
    # 상한(LIMIT)을 먹는다. 정확한 좌표 쌍으로 LIMIT 전에 자른다.
    pairs = sorted(scopes)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
              FROM pdf_page_search_entry e
             WHERE (e.workspace, e.channel_id) IN
                   (SELECT * FROM unnest(%(scope_ws)s::text[], %(scope_ch)s::text[]))
               AND e.status IN ('extracted', 'uncertain')
               {keyword_sql}
             ORDER BY e.created_at DESC
             LIMIT %(lim)s
            """,
            {"scope_ws": [ws for ws, _ in pairs], "scope_ch": [ch for _, ch in pairs],
             "lim": CANDIDATE_LIMIT, **keyword_params},
        )
        rows = list(cur.fetchall())
    picked = [e for e in (_entry(r) for r in rows) if e is not None]
    picked = [e for e in picked if (e.workspace, e.channel_id) in scopes]
    if not picked:
        return []
    # 같은 쪽의 더 새 revision(낱말이 안 맞아 위에서 빠진 것)이 있으면 그쪽이 기준이다.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
              FROM pdf_page_search_entry e
             WHERE (e.workspace, e.channel_id, e.file_id, e.page_number) IN
                   (SELECT * FROM unnest(%(ws)s::text[], %(ch)s::text[], %(fid)s::text[],
                                         %(pg)s::int[]))
            """,
            {
                "ws": [e.workspace for e in picked],
                "ch": [e.channel_id for e in picked],
                "fid": [e.file_id for e in picked],
                "pg": [e.page_number for e in picked],
            },
        )
        siblings = [e for e in (_entry(r) for r in cur.fetchall()) if e is not None]
    matched_keys = {e.page_key for e in picked}
    return [e for e in _latest_per_page([*picked, *siblings]) if e.page_key in matched_keys]


def entries_by_revision(revision_ids: list[str], *, conn) -> dict[str, PageEntry]:
    """후속 질문 — revision_id 로 투영을 다시 읽고, 그 쪽의 최신 revision 과 견준다."""
    if not revision_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM pdf_page_search_entry e WHERE e.revision_id = ANY(%(ids)s)",
            {"ids": list(revision_ids)},
        )
        found = [e for e in (_entry(r) for r in cur.fetchall()) if e is not None]
        if not found:
            return {}
        cur.execute(
            f"""
            SELECT {_COLUMNS}
              FROM pdf_page_search_entry e
             WHERE (e.workspace, e.channel_id, e.file_id, e.page_number) IN
                   (SELECT * FROM unnest(%(ws)s::text[], %(ch)s::text[], %(fid)s::text[],
                                         %(pg)s::int[]))
            """,
            {
                "ws": [e.workspace for e in found],
                "ch": [e.channel_id for e in found],
                "fid": [e.file_id for e in found],
                "pg": [e.page_number for e in found],
            },
        )
        siblings = [e for e in (_entry(r) for r in cur.fetchall()) if e is not None]
    latest = {e.page_key: e for e in _latest_per_page([*found, *siblings])}
    out: dict[str, PageEntry] = {}
    for entry in found:
        current = latest.get(entry.page_key)
        # 더 새 revision 이 나왔으면 옛 좌표는 그 쪽의 현재 전사가 아니다.
        if current is not None and current.revision_id == entry.revision_id:
            out[entry.revision_id] = entry
    return out


# ── 재검증 ────────────────────────────────────────────────────────────────


def entry_is_clean(body: str) -> bool:
    """전사 본문을 **지금** 규칙으로 다시 본다. 저장 뒤 규칙이 바뀌었을 수 있다."""
    from .pii_screen import screen_line
    from .sensitive_mask import contains_sensitive

    return not any(
        screen_line(line) or contains_sensitive(line) for line in body.splitlines() if line
    ) and not contains_sensitive(body)


def _attachment_paths(root: Path, entry: PageEntry) -> list[Path]:
    from .archive.attachment_doc import _safe

    ws, ch, fid = _safe(entry.workspace), _safe(entry.channel_id), _safe(entry.file_id)
    legacy = root / "workspaces" / ws / "channels" / ch / "attachments" / fid
    paths = sorted(legacy.glob("*.md")) if legacy.is_dir() else []
    paths += sorted(root.glob(f"{ws}/{ch}__*/archive/attachments/{fid}/*.md"))
    return paths


def current_doc(root: Path, entry: PageEntry):
    """그 파일의 **지금** 첨부 정본. 없으면 None."""
    from .archive import attachment_reader

    loaded = [
        doc
        for doc in (attachment_reader.load_checked(p, root) for p in _attachment_paths(root, entry))
        if doc is not None
        and doc.workspace == entry.workspace
        and doc.channel_id == entry.channel_id
        and doc.file_id == entry.file_id
    ]
    current = attachment_reader.current_by_file(loaded)
    return current[0] if len(current) == 1 else None


def coordinate_code(doc, entry: PageEntry) -> str:
    """정본과 투영이 **같은 원본**을 가리키나."""
    if doc is None:
        return DROP_DOC
    if (
        doc.workspace != entry.workspace
        or (doc.channel_id or "") != entry.channel_id
        or doc.file_id != entry.file_id
        or (doc.sha256 or "").strip().lower() != entry.original_sha256
    ):
        return DROP_COORDINATE
    if doc.conversion_state not in _ALLOWED_DOC_STATES:
        # 정본이 차단·격리·보류로 바뀌었다. 그 파일의 어떤 파생도 근거가 아니다.
        return DROP_COORDINATE
    filetype = (doc.filetype or Path(doc.name or "").suffix.lstrip(".")).lower()
    if filetype != "pdf":
        return DROP_COORDINATE
    return ""


_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_CACHE_MAX = 256


def _file_sha256(path: Path) -> tuple[str, str]:
    """상한 안에서 해시를 잰다. 같은 파일 지문(경로·크기·mtime)이면 다시 읽지 않는다."""
    try:
        if path.is_symlink():
            return "", "original_symlink"
        info = path.lstat()
    except OSError:
        return "", "original_unreadable"
    from stat import S_ISREG

    if not S_ISREG(info.st_mode):
        return "", "original_not_a_regular_file"
    if info.st_size <= 0:
        return "", "original_empty"
    if info.st_size > MAX_ORIGINAL_BYTES:
        return "", "original_too_large"
    key = (str(path), int(info.st_size), int(info.st_mtime_ns))
    cached = _HASH_CACHE.get(key)
    if cached:
        return cached, ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            head = handle.read(5)
            if head != b"%PDF-":
                return "", "original_not_pdf"
            digest.update(head)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "", "original_unreadable"
    value = digest.hexdigest()
    if len(_HASH_CACHE) >= _HASH_CACHE_MAX:
        _HASH_CACHE.clear()
    _HASH_CACHE[key] = value
    return value, ""


def original_code(root: Path, doc, entry: PageEntry) -> str:
    """원본 PDF 가 **지금도 그 바이트로** 있나. 빈 문자열 = 확인됨.

    경로는 metadata 문자열이 아니라 수집과 **같은 규칙**(`files.attachment_storage`)으로
    만든다. 이미지 원본 로더와 같은 자리 규칙·링크 거절·「하나가 아니면 고르지 않음」.
    """
    from .archive import image_description_original as orig
    from .archive.shadow_paths import ShadowPathError, refuse_symlinked_chain

    coordinate = orig._coordinate_code(entry)
    if coordinate:
        return coordinate
    source = getattr(doc, "source_path", None)
    if source is not None and not orig._inside(Path(source), root):
        return "original_outside_root"
    try:
        channel_root = orig._channel_root(root, doc)
        objects = orig._objects_dir(root, entry, channel_root)
    except (OSError, ValueError, IndexError):
        return "original_outside_root"
    try:
        refuse_symlinked_chain(root if channel_root is not None else root.parent, objects)
    except ShadowPathError:
        return "original_symlink"
    except (OSError, ValueError):
        return "original_outside_root"
    path, code = orig._one_file(objects, objects.parent)
    if code:
        return code
    if not orig._inside(path, objects):
        return "original_outside_root"
    digest, code = _file_sha256(path)
    if code:
        return code
    if digest != entry.original_sha256:
        return "original_hash_mismatch"
    return ""


def _visible_scopes(store, ctx) -> set[tuple[str, str]]:
    """요청자가 **지금** 볼 수 있는 채널 좌표. VIP evidence_sources 도 이 판정 안이다."""
    return {
        (str(d.workspace or ""), str(d.channel_id or ""))
        for d in store.visible_channel_docs(ctx)
        if d.workspace and d.channel_id and not getattr(d, "dm_user", None)
    }


def _excerpt(body: str, query: str) -> str:
    """근거로 싣는 부분. 질문과 맞는 줄을 앞세우되 원래 순서로 잇는다."""
    from . import search_index

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    tokens = search_index.tokens_of(query) if query else []
    if tokens:
        ranked = sorted(
            range(len(lines)),
            key=lambda i: (-search_index.score_line(tokens, query, "", lines[i]), i),
        )
    else:
        ranked = list(range(len(lines)))
    chosen: list[int] = []
    used = 0
    for index in ranked:
        size = len(lines[index]) + 1
        if chosen and used + size > MAX_PAGE_CHARS:
            continue
        chosen.append(index)
        used += size
        if used >= MAX_PAGE_CHARS:
            break
    text = " / ".join(lines[i] for i in sorted(chosen))
    return text[:MAX_PAGE_CHARS]


def _hit(store, root: Path, doc, entry: PageEntry, query: str, score: int) -> VisionPageHit:
    from .archive.attachment_reader import as_archive_doc

    archive_doc = as_archive_doc(doc, root)
    stamp = archive_doc.last_ingested or ""
    path = archive_doc.path
    line = RawLine(
        ts=stamp[:16].replace("T", " "),
        speaker=doc.name or doc.file_id,
        text=f"{label_for(entry.page_number)} {_excerpt(entry.body, query)}",
        # 실제 첨부 줄(1..)과 겹치지 않는 자리. 쪽마다 하나다.
        lineno=-entry.page_number,
        source_path=path,
        message_ts=doc.message_ts,
    )
    return VisionPageHit(
        doc=replace(archive_doc, raw_lines=[line]),
        line=line,
        score=max(1, score),
        revision_id=entry.revision_id,
        file_id=entry.file_id,
        page_number=entry.page_number,
        original_sha256=entry.original_sha256,
        pdf_name=doc.name or "",
    )


def verify(store, ctx, entry: PageEntry, scopes, *, query: str = "", score: int = 1):
    """한 쪽을 근거로 쓸 수 있나. `(hit, "")` 또는 `(None, 사유 코드)`."""
    if entry.status != STATUS_EXTRACTED or not entry.body:
        return None, DROP_STATUS
    if (entry.workspace, entry.channel_id) not in scopes:
        return None, DROP_ACL
    if not entry_is_clean(entry.body):
        return None, DROP_PII
    root = Path(store.root)
    doc = current_doc(root, entry)
    code = coordinate_code(doc, entry)
    if code:
        return None, code
    hit = _hit(store, root, doc, entry, query, score)
    # 채널만이 아니라 **정본 문서 자체**도 같은 권한 판정을 지나야 한다.
    if not store.visible_predicate(ctx)(hit.doc):
        return None, DROP_ACL
    code = original_code(root, doc, entry)
    if code:
        return None, code
    return hit, ""


def _matched(query: str, entries: list[PageEntry]) -> list[tuple[int, PageEntry]]:
    from . import search_index

    tokens = search_index.tokens_of(query)
    scored = []
    for index, entry in enumerate(entries):
        score = max(
            (search_index.score_line(tokens, query, "", ln) for ln in entry.body.splitlines()),
            default=0,
        )
        if score > 0:
            scored.append((score, index, entry))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [(score, entry) for score, _, entry in scored]


def expand(store, ctx, query: str, *, conn=None, limit: int = MAX_PAGES) -> list[VisionPageHit]:
    """최초 질문 — 질문과 맞는 쪽 전사. 실패하면 빈 목록(기존 검색 그대로)."""
    from . import search_index

    tokens = search_index.tokens_of(query)
    if not tokens:
        return []
    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        # DB 가 없으면 투영도 없다. 권한 계산·원문 읽기 전에 끝낸다.
        return []
    try:
        scopes = _visible_scopes(store, ctx)
        entries = active_entries(scopes, tokens=tokens, conn=handle) if scopes else []
    except Exception as exc:  # noqa: BLE001 - DB 장애는 길잡이 없음으로 닫는다
        logger.warning("PDF 전사 투영을 읽지 못했습니다: %s", type(exc).__name__)
        return []
    finally:
        if owned:
            _close(handle)
    dropped: list[str] = []
    hits: list[VisionPageHit] = []
    for score, entry in _matched(query, entries):
        if len(hits) >= limit:
            break
        try:
            hit, code = verify(store, ctx, entry, scopes, query=query, score=score)
        except Exception as exc:  # noqa: BLE001 - 모르면 근거로 쓰지 않는다
            hit, code = None, type(exc).__name__
        if hit is None:
            dropped.append(code)
            continue
        hits.append(hit)
    logger.info(
        "PDF 전사 길잡이 ws=%s 투영=%d 근거=%d 제외=%s",
        getattr(ctx, "workspace", "-"), len(entries), len(hits),
        "|".join(sorted(set(dropped))) or "-",
    )
    return hits


def resolve_refs(store, ctx, refs, *, conn=None) -> tuple[list[VisionPageHit], list[str]]:
    """후속 질문 — 좌표로 DB 를 다시 읽고, 최초 질문과 **같은 재검증**을 처음부터 한다."""
    items = [r for r in refs or () if getattr(r, "revision_id", "")]
    if not items:
        return [], []
    dropped: list[str] = []

    def drop(code: str) -> None:
        if code not in dropped:
            dropped.append(code)

    handle, owned = (conn, False) if conn is not None else (_connect(), True)
    if handle is None:
        return [], [DROP_DB]
    try:
        scopes = _visible_scopes(store, ctx)
        found = entries_by_revision([r.revision_id for r in items], conn=handle)
    except Exception as exc:  # noqa: BLE001 - DB 장애면 그 좌표는 열지 않는다
        logger.warning("PDF 전사 좌표를 다시 읽지 못했습니다: %s", type(exc).__name__)
        return [], [DROP_DB]
    finally:
        if owned:
            _close(handle)
    hits: list[VisionPageHit] = []
    for ref in items:
        entry = found.get(ref.revision_id)
        if entry is None:
            drop(DROP_PROJECTION)
            continue
        if (
            entry.workspace != ref.workspace
            or entry.channel_id != ref.channel_id
            or entry.file_id != ref.file_id
            or entry.page_number != ref.page_number
            or entry.original_sha256 != ref.original_sha256
        ):
            drop(DROP_COORDINATE)
            continue
        try:
            hit, code = verify(store, ctx, entry, scopes)
        except Exception as exc:  # noqa: BLE001
            hit, code = None, type(exc).__name__
        if hit is None:
            drop(code)
            continue
        hits.append(hit)
    return hits, dropped


__all__ = [
    "LABEL",
    "MAX_PAGES",
    "PageEntry",
    "VisionPageHit",
    "active_entries",
    "entries_by_revision",
    "expand",
    "label_for",
    "original_code",
    "resolve_refs",
    "verify",
]
