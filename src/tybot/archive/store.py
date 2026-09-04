"""로컬 MD 아카이브 읽기/검색.

환각방지 2겹: 색인이 아니라 **원문 라인**을 반환한다. 답변은 이 라인만 근거로 한다.
v1 평면 파일과 v2 ``workspaces/<ws>/channels/<id>__<name>/raw/<date>.md``를 함께 읽는다.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..access import RequestContext, can_access

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
RAW_HEADING_RE = re.compile(r"^##\s*원문", re.MULTILINE)
SUMMARY_HEADING_RE = re.compile(r"^##\s*요약", re.MULTILINE)
# > [2026-08-12 09:15] 홍길동: 내용
RAW_LINE_RE = re.compile(r"^>\s*\[(?P<ts>[^\]]+)\]\s*(?P<speaker>[^:]+):\s*(?P<text>.*)$")
# 검색어 토큰 규칙은 `search_index.TOKEN_RE` 하나뿐이다.
# 여기에 또 적으면 색인 후보와 파일 스캔이 다른 토큰으로 찾게 되고,
# 그건 에러가 아니라 **같은 질문에 다른 답**으로 나타난다.


def workspace_from_path(path: Path, root: Path | str) -> str:
    """v1/v2 원문 경로에서 워크스페이스 키를 얻는다."""
    try:
        parts = path.relative_to(Path(root)).parts
    except ValueError:
        return "unknown"
    if len(parts) >= 2 and parts[0] in {"channels", "workspaces"}:
        return parts[1]
    return "unknown"


@dataclass(frozen=True)
class RawLine:
    """원문 한 줄. 편집 금지 대상."""

    ts: str
    speaker: str
    text: str
    lineno: int
    source_path: Path | None = None


@dataclass
class ArchiveDoc:
    path: Path
    workspace: str
    channel: str
    visibility: str
    acl: frozenset[str]
    # 이 문서를 넘길 다른 워크스페이스 목록(선택). 비어 있으면 동등 워크스페이스로 안 나간다.
    share_with: frozenset[str]
    last_ingested: str | None
    channel_id: str | None = None
    schema_version: int = 1
    # 채널명에서 뽑은 조직 정보(선택). 조직 트리 연결·개편 추적에 쓴다.
    org_code: str | None = None
    org_kind: str | None = None
    org_name: str | None = None
    raw_lines: list[RawLine] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.channel or self.path.stem


@dataclass(frozen=True)
class SearchHit:
    doc: ArchiveDoc
    line: RawLine
    score: int

    def citation(self, *, with_workspace: bool = False) -> str:
        """출처 문자열 (4겹: 출처 강제).

        다른 워크스페이스 자료를 인용할 때는 워크스페이스를 함께 밝힌다 -
        읽는 사람이 "이건 우리 자료가 아니다"를 알 수 있어야 한다.
        """
        date = self.line.ts.split()[0] if self.line.ts else ""
        prefix = f"[{self.doc.workspace}] " if with_workspace else ""
        source = self.line.source_path or self.doc.path
        return f"{prefix}{self.doc.channel}, 📄{source.name}({date})"


class SchemaError(ValueError):
    """프론트매터/구조 위반. 게시 전 형식 검사에서 사용."""


def _strip_comment(line: str) -> str:
    """인라인 주석 제거. 채널명의 '#'(따옴표/대괄호 안)은 주석이 아니다."""
    quote: str | None = None
    depth = 0
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0 and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _parse_scalar(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """의존성 없는 최소 YAML 파서. `key: value` 와 `key: [a, b]` 만 지원."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise SchemaError("프론트매터(--- ... ---)가 없다")
    out: dict[str, str | list[str]] = {}
    for raw in m.group(1).splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            out[key.strip()] = [_parse_scalar(p) for p in inner.split(",") if p.strip()]
        else:
            out[key.strip()] = _parse_scalar(value)
    return out


REQUIRED_FIELDS = ("workspace", "channel", "visibility", "acl")


def validate(text: str, *, path: str = "<memory>") -> dict[str, str | list[str]]:
    """게시 전 형식 검사. 실패 시 SchemaError — 호출측이 그날 취합을 롤백한다."""
    fm = parse_frontmatter(text)
    missing = [k for k in REQUIRED_FIELDS if k not in fm]
    if missing:
        raise SchemaError(f"{path}: 프론트매터 필수 필드 누락 {missing}")
    if fm.get("visibility") not in ("public", "private"):
        raise SchemaError(f"{path}: visibility 는 public|private 만 허용")
    version = str(fm.get("schema_version") or "1")
    if version not in {"1", "2"}:
        raise SchemaError(f"{path}: 지원하지 않는 schema_version {version}")
    if version == "2":
        missing_v2 = [key for key in ("channel_id", "source_date") if not fm.get(key)]
        if missing_v2:
            raise SchemaError(f"{path}: v2 필수 필드 누락 {missing_v2}")
    if not RAW_HEADING_RE.search(text):
        raise SchemaError(f"{path}: '## 원문' 섹션 없음")
    return fm


def _raw_section(text: str) -> tuple[str, int]:
    """(원문 섹션 본문, 시작 라인 오프셋). 요약 섹션은 근거로 쓰지 않는다."""
    m = RAW_HEADING_RE.search(text)
    if not m:
        return "", 0
    start = m.end()
    offset = text.count("\n", 0, start)
    rest = text[start:]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest), offset


def load_doc(path: Path) -> ArchiveDoc:
    text = path.read_text(encoding="utf-8")
    fm = validate(text, path=str(path))
    acl_raw = fm.get("acl") or []
    acl = frozenset(acl_raw if isinstance(acl_raw, list) else [acl_raw])
    sw_raw = fm.get("share_with") or []
    share_with = frozenset(sw_raw if isinstance(sw_raw, list) else [sw_raw])
    body, offset = _raw_section(text)
    lines: list[RawLine] = []
    for i, ln in enumerate(body.splitlines(), start=offset + 1):
        m = RAW_LINE_RE.match(ln.strip())
        if m:
            lines.append(
                RawLine(
                    ts=m.group("ts").strip(),
                    speaker=m.group("speaker").strip(),
                    text=m.group("text").strip(),
                    lineno=i,
                    source_path=path,
                )
            )
    return ArchiveDoc(
        path=path,
        workspace=str(fm["workspace"]),
        channel=str(fm["channel"]),
        visibility=str(fm.get("visibility", "private")),
        acl=acl,
        share_with=share_with,
        last_ingested=str(fm.get("last_ingested")) if fm.get("last_ingested") else None,
        channel_id=str(fm["channel_id"]) if fm.get("channel_id") else None,
        schema_version=int(str(fm.get("schema_version") or "1")),
        raw_lines=lines,
        org_code=str(fm["org_code"]) if fm.get("org_code") else None,
        org_kind=str(fm["org_kind"]) if fm.get("org_kind") else None,
        org_name=str(fm["org_name"]) if fm.get("org_name") else None,
    )


class ArchiveStore:
    """v1/v2 원문 MD 파일을 논리 채널 단위로 합쳐 제공한다.

    파싱 결과는 **파일 mtime·크기 기준으로 캐시**한다. 한 질문을 처리하는 동안
    `visible_docs()` 가 여러 번 불리고(검색 → 0건이면 제목 목록), 매번 전 파일을 다시
    읽으면 문서 수에 비례해 느려진다. 수집기는 append 만 하므로 mtime 이 바뀌면
    그 파일만 다시 읽으면 된다.

    캐시는 워크스페이스 봇들이 공유하는 인스턴스에 얹히므로 락으로 감싼다.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        # path -> (stat 지문, 파싱 결과 또는 SchemaError 메시지)
        self._cache: dict[Path, tuple[tuple[int, int], ArchiveDoc | str]] = {}
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        v2 = self.root / "workspaces"
        legacy = self.root / "channels"
        # v2를 먼저 읽어 v1과 같은 라인이 있으면 새 경로를 출처로 남긴다.
        files = sorted(v2.glob("*/channels/*/raw/*.md")) if v2.is_dir() else []
        if legacy.is_dir():
            files.extend(sorted(legacy.glob("*/*.md")))
        return files

    def source_files(self) -> list[Path]:
        """점검·마이그레이션용 실제 원문 파일 목록."""
        return self._files()

    def _load(self, path: Path) -> ArchiveDoc | str:
        """캐시된 파싱 결과. 형식 위반은 사유 문자열로 캐시한다(재파싱 낭비 방지)."""
        try:
            st = path.stat()
        except OSError as e:
            return f"{path}: 읽을 수 없다 ({e})"
        fingerprint = (st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == fingerprint:
                return cached[1]
        try:
            result: ArchiveDoc | str = load_doc(path)
        except SchemaError as e:
            result = str(e)
        with self._lock:
            self._cache[path] = (fingerprint, result)
            # 삭제된 파일의 캐시는 흘려두지 않는다.
            if len(self._cache) > 4096:
                self._cache = {p: v for p, v in self._cache.items() if p.exists()}
        return result

    def docs(self) -> list[ArchiveDoc]:
        loaded = self.source_docs()

        # v1에는 channel_id가 없다. 같은 이름의 v2 문서가 있으면 그 ID로 묶어
        # 마이그레이션 전후 원문이 답변에 중복으로 들어가지 않게 한다.
        ids_by_name = {
            (doc.workspace, doc.channel): doc.channel_id for doc in loaded if doc.channel_id
        }
        grouped: dict[tuple[str, str], list[ArchiveDoc]] = {}
        for doc in loaded:
            identity = doc.channel_id or ids_by_name.get((doc.workspace, doc.channel)) or doc.channel
            grouped.setdefault((doc.workspace, identity), []).append(doc)
        return [self._merge(parts) for parts in grouped.values()]

    def source_docs(self) -> list[ArchiveDoc]:
        """실제 원문 파일별 문서. 콘솔 파일 목록과 점검에 사용한다."""
        loaded: list[ArchiveDoc] = []
        for p in self._files():
            got = self._load(p)
            # 조용한 0건이 가장 위험 — 형식 위반은 건너뛰되 broken() 으로 감지 가능하게 남긴다.
            if isinstance(got, ArchiveDoc):
                loaded.append(got)
        return loaded

    @staticmethod
    def _merge(parts: list[ArchiveDoc]) -> ArchiveDoc:
        """같은 채널의 일자 파일을 합친다. 권한 메타가 엇갈리면 막는 쪽으로 합친다."""
        newest = max(
            parts,
            key=lambda doc: (
                doc.last_ingested or "",
                max((line.ts for line in doc.raw_lines), default=""),
            ),
        )
        # ACL의 채널명은 이름 변경 전후 값이 함께 남을 수 있다. 같은 Slack 채널 ID로
        # 묶인 문서만 합치므로 현재 멤버십 이름과 겹치도록 합집합을 쓴다.
        acl: set[str] = set()
        share_with = set(parts[0].share_with)
        for doc in parts:
            acl.update(doc.acl)
        for doc in parts[1:]:
            share_with.intersection_update(doc.share_with)

        lines: list[RawLine] = []
        seen: set[tuple[str, str, str]] = set()
        for doc in parts:
            for line in doc.raw_lines:
                key = (line.ts, line.speaker, line.text)
                if key not in seen:
                    seen.add(key)
                    lines.append(line)
        lines.sort(key=lambda line: (line.ts, str(line.source_path), line.lineno))
        return ArchiveDoc(
            path=newest.path,
            workspace=newest.workspace,
            channel=newest.channel,
            visibility="public" if all(d.visibility == "public" for d in parts) else "private",
            acl=frozenset(acl),
            share_with=frozenset(share_with),
            last_ingested=max((d.last_ingested or "" for d in parts), default="") or None,
            channel_id=next((d.channel_id for d in parts if d.channel_id), None),
            schema_version=max(d.schema_version for d in parts),
            org_code=newest.org_code,
            org_kind=newest.org_kind,
            org_name=newest.org_name,
            raw_lines=lines,
        )

    def broken(self) -> list[tuple[Path, str]]:
        """형식 검사 실패 목록. 운영 알림용."""
        return [(p, got) for p in self._files() if isinstance(got := self._load(p), str)]

    def visible_docs(self, ctx: RequestContext) -> list[ArchiveDoc]:
        """3겹/권한: 답변 생성 **이전에** 검색 범위를 축소한다."""
        return [
            d
            for d in self.docs()
            if can_access(
                ctx,
                visibility=d.visibility,
                acl=d.acl if d.acl else None,
                owner_workspace=d.workspace,
                share_with=d.share_with if d.share_with else None,
            )
        ]

    def titles(self, ctx: RequestContext) -> list[str]:
        """검색 0건 폴백 — 권한 내 문서 제목 목록."""
        return [d.title for d in self.visible_docs(ctx)]

    def search(self, query: str, ctx: RequestContext, *, limit: int = 20) -> list[SearchHit]:
        """근거 줄 찾기. 색인(DB)을 먼저 보고, 못 보면 파일을 훑는다.

        **권한은 여기서, 코드가 판정한다**(`visible_docs`). 색인에는 이미 통과한
        채널 목록만 넘긴다 — 판정을 SQL 로 옮기면 ACL 이 두 곳으로 갈라진다(원칙 3).

        **DB 를 못 읽는 것과 색인에 없는 것을 구별한다.** 섞으면 DB 장애가
        「자료를 찾지 못했습니다」 로 나가고, 그건 장애가 아니라 정상 답으로 보인다.
        """
        from .. import search_index

        tokens = search_index.tokens_of(query)
        if not tokens:
            return []

        docs = self.visible_docs(ctx)
        found = search_index.candidates(query, sorted({d.channel for d in docs if d.channel}))
        if found is None:
            return self._scan(query, tokens, docs, limit)

        # 색인이 넣은 것과 **같은 함수로** 키를 만든다. 한쪽만 절대 경로면 매칭이
        # 전부 실패하고, 오류 없이 파일 스캔으로 되돌아간다.
        by_path = {search_index.rel_path(d.path, self.root): d for d in docs}
        hits: list[SearchHit] = []
        for cand in found:
            doc = by_path.get(cand.doc_path)
            # 색인에 있으나 지금 권한으로는 안 보이는 문서 — 조용히 건너뛴다.
            # 색인이 낡아 문서가 사라진 경우도 같은 자리로 떨어진다.
            if doc is None:
                continue
            line = next((ln for ln in doc.raw_lines if ln.lineno == cand.line_no), None)
            if line is None:
                continue
            score = search_index.score_line(tokens, query, line.speaker, line.text)
            if score:
                hits.append(SearchHit(doc=doc, line=line, score=score))
        if not hits:
            # 색인이 아직 안 돌았을 수 있다. 0건으로 답하기 전에 파일을 한 번 본다 —
            # 「색인 없음」이 「자료 없음」으로 보이는 것이 이 기능의 가장 나쁜 실패다.
            return self._scan(query, tokens, docs, limit)
        return self._rank(hits, limit)

    def _scan(
        self, query: str, tokens: list[str], docs: list[ArchiveDoc], limit: int
    ) -> list[SearchHit]:
        """파일 스캔 폴백. 색인 경로와 **같은 점수 함수**를 쓴다."""
        from .. import search_index

        hits = [
            SearchHit(doc=doc, line=line, score=score)
            for doc in docs
            for line in doc.raw_lines
            if (score := search_index.score_line(tokens, query, line.speaker, line.text))
        ]
        return self._rank(hits, limit)

    @staticmethod
    def _rank(hits: list[SearchHit], limit: int) -> list[SearchHit]:
        """점수 → **최근순** → 경로. 예전에는 점수 다음이 파일명이었다.

        같은 점수면 오래된 줄이 먼저 올라와, 바뀐 숫자를 묻는 질문에 옛 값이 근거로
        붙었다. 시각 표기가 없는 줄은 뒤로 보낸다(판정할 수 없는 것을 앞세우지 않는다).
        """
        # 파이썬 정렬은 안정적이라, **덜 중요한 것부터 차례로** 정렬하면 된다.
        # 문자열을 음수화할 수 없으니 이 방식이 보수(complement) 트릭보다 읽기 쉽다.
        hits.sort(key=lambda h: (h.doc.path.name, h.line.lineno))
        hits.sort(key=lambda h: h.line.ts or "", reverse=True)   # 최근순
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]
