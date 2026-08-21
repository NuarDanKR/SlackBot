"""로컬 MD 아카이브 읽기/검색.

환각방지 2겹: 색인이 아니라 **원문 라인**을 반환한다. 답변은 이 라인만 근거로 한다.
스키마: `.claude/skills/md-archive-schema` — archive/channels/<workspace>/<channel>.md
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
# 검색어 토큰: 2자 이상 한글/영숫자
TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


@dataclass(frozen=True)
class RawLine:
    """원문 한 줄. 편집 금지 대상."""

    ts: str
    speaker: str
    text: str
    lineno: int


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
        return f"{prefix}{self.doc.channel}, 📄{self.doc.path.name}({date})"


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
        raw_lines=lines,
    )


class ArchiveStore:
    """archive/channels 아래 MD 파일 집합.

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
        base = self.root / "channels"
        return sorted(base.rglob("*.md")) if base.is_dir() else []

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
        out: list[ArchiveDoc] = []
        for p in self._files():
            got = self._load(p)
            # 조용한 0건이 가장 위험 — 형식 위반은 건너뛰되 broken() 으로 감지 가능하게 남긴다.
            if isinstance(got, ArchiveDoc):
                out.append(got)
        return out

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
        tokens = [t.lower() for t in TOKEN_RE.findall(query)]
        if not tokens:
            return []
        hits: list[SearchHit] = []
        for doc in self.visible_docs(ctx):
            for line in doc.raw_lines:
                hay = f"{line.speaker} {line.text}".lower()
                score = sum(1 for t in tokens if t in hay)
                if score:
                    hits.append(SearchHit(doc=doc, line=line, score=score))
        hits.sort(key=lambda h: (-h.score, h.doc.path.name, h.line.lineno))
        return hits[:limit]
