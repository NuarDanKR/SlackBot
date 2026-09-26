"""로컬 MD 아카이브 읽기/검색.

환각방지 2겹: 색인이 아니라 **원문 라인**을 반환한다. 답변은 이 라인만 근거로 한다.
v1 평면 파일과 v2 ``workspaces/<ws>/channels/<id>__<name>/raw/<date>.md``를 함께 읽는다.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..access import RequestContext, can_access
from . import revision_reader

logger = logging.getLogger("tybot.archive.store")
_legacy_warning_counts: dict[Path, int] = {}
_legacy_warning_lock = threading.Lock()


def _legacy_changed(root: Path, count: int) -> bool:
    with _legacy_warning_lock:
        key = root.resolve()
        previous = _legacy_warning_counts.get(key, 0)
        _legacy_warning_counts[key] = count
        return bool(count and previous != count)

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
RAW_HEADING_RE = re.compile(r"^##\s*원문", re.MULTILINE)
SUMMARY_HEADING_RE = re.compile(r"^##\s*요약", re.MULTILINE)
# > [2026-08-12 09:15] 홍길동: 내용
# > [2026-08-12 09:15|1758012345.123456] 홍길동: 내용  ← Slack 메시지 좌표를 함께 적은 줄
RAW_LINE_RE = re.compile(r"^>\s*\[(?P<ts>[^\]]+)\]\s*(?P<speaker>[^:]+):\s*(?P<text>.*)$")

# Slack 메시지 ts(`1758012345.123456`). 이 모양이 아니면 좌표로 쓰지 않는다 —
# 사람이 손으로 적은 값이 permalink 로 나가면 열리지 않는 링크가 출처가 된다.
SLACK_TS_RE = re.compile(r"\A\d{1,12}\.\d{1,8}\Z")


def split_stamp(value: str) -> tuple[str, str]:
    """`2026-09-16 14:23|1758012345.123456` → (표시 시각, Slack 메시지 ts).

    좌표는 **시각 칸 안에** 적는다. 본문 뒤에 붙이면 그 문자열이 원문 텍스트의
    일부가 되고, 검색·요약·인용이 전부 우리가 덧붙인 글자를 원문으로 읽는다
    (원칙 1 — 원문 보존).

    좌표가 없는 옛 줄은 그대로 통과한다. 수집 시점 이전 원문에는 없다.
    """
    head, _, tail = (value or "").partition("|")
    ts = tail.strip()
    return head.strip(), ts if SLACK_TS_RE.match(ts) else ""

# 만들어 낸 channel_id 의 접두사. `writer._stable_channel_id()` 가 Slack ID 를 모를 때
# 붙인다. **여기서 다시 적는 이유는 순환 import 때문이다** — `writer` 가 이 모듈을
# 쓰므로 반대 방향으로 끌어올 수 없다. 값이 갈리면 같은 채널이 다시 둘로 갈리므로
# `tests/test_originals.py` 가 양쪽이 같은지 본다.
SYNTHETIC_ID_PREFIX = "legacy-"


def is_synthetic_channel_id(value: str | None) -> bool:
    """Slack 이 준 ID 가 아니라 우리가 만든 자리표시자인가.

    이 값을 신원으로 쓰면 같은 채널의 v1·v2 문서가 서로 다른 채널이 되고,
    출처가 두 줄 나오고 **권한 메타가 따로 계산된다**(2026-09-07 실제 발생).
    """
    return bool(value) and str(value).startswith(SYNTHETIC_ID_PREFIX)
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
    # Slack 메시지 ts. 있으면 이 줄 하나를 가리키는 permalink 를 만들 수 있다.
    # 수집 경로가 남기지 못한 옛 줄은 빈 문자열이고, 그때는 채널 링크로 내려간다.
    message_ts: str = ""


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
    # 값이 있으면 **봇과의 DM 원문**이다. 그 사람 한 명의 작업공간이므로
    # 채널 멤버십이 아니라 본인 여부로 열린다(설계 dm-workspace.md §2).
    dm_user: str | None = None
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

        채널명이 아니라 **조직 이름**으로 보인다 — `[전산팀]공지`. 예전에는
        `[tyit]#팀-전산_abb155-공지` 였는데, 그건 사람이 아니라 우리가 만든
        키라서 출처만 보고 어느 조직 자료인지 알 수 없었다.

        다른 워크스페이스 자료는 **끝에 밝힌다.** 앞자리는 조직이 가져갔지만
        「이건 우리 자료가 아니다」 를 지우면 안 된다(원칙 4).

        보관·삭제된 채널을 근거로 켠 경우에는 `(보관 채널)` 이 붙는다(B-51).
        **조용히 섞지 않는다** — 사람이 출처를 눌러도 채널이 없을 수 있다.
        """
        from ..channel_lifecycle import mark_for
        from ..channels import source_label

        date = self.line.ts.split()[0] if self.line.ts else ""
        source = self.line.source_path or self.doc.path
        tail = f" ({self.doc.workspace})" if with_workspace else ""
        if getattr(self.doc, "dm_user", None):
            # DM 은 조직이 없다. 채널명을 그대로 보이면 `DM:U0BR…` 라는 내부 키가
            # 사람에게 나간다 — 출처는 사람이 읽고 확인하러 갈 수 있어야 한다(원칙 2).
            return f"[DM]나와의 대화{tail}, 📄{source.name}({date})"
        tail += mark_for(self.doc.workspace, self.doc.channel_id, self.doc.channel)
        return f"{source_label(self.doc.channel)}{tail}, 📄{source.name}({date})"


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
            stamp, message_ts = split_stamp(m.group("ts"))
            lines.append(
                RawLine(
                    ts=stamp,
                    speaker=m.group("speaker").strip(),
                    text=m.group("text").strip(),
                    lineno=i,
                    source_path=path,
                    message_ts=message_ts,
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
        dm_user=str(fm["dm_user"]) if fm.get("dm_user") else None,
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

    def _files(self, *, dm_scope: str = "") -> list[Path]:
        """원문 파일 목록. **DM 은 기본으로 빼고 센다.**

        `dm_scope` 는 셋 중 하나다.

        | 값 | 읽는 DM |
        |---|---|
        | `""` (기본) | 없음 |
        | 사용자 ID | **그 사람 것만** |
        | `"*"` | 전부 (색인 재빌드 전용) |

        남의 DM 을 「읽고 나서 거르는」 구조를 만들지 않는다. 거르는 코드가 한 번
        어긋나면 그건 오류가 아니라 **개인 기록 노출**로 나타나고, 실제로 그렇게
        샜다 — `_merge()` 가 `dm_user` 를 떨어뜨려 통합조회 권한에 열렸다
        (2026-09-18 구현 중 발견). 그래서 애초에 파일을 열지 않는다.

        채널을 훑는 기존 소비자(콘솔 목록·채널 헬스·조직 매핑·요약 검토)는 기본값
        덕분에 한 줄도 고치지 않아도 개인 기록을 보지 않는다(설계 dm-workspace.md §3).
        """
        v2 = self.root / "workspaces"
        legacy = self.root / "channels"
        # v2를 먼저 읽어 v1과 같은 라인이 있으면 새 경로를 출처로 남긴다.
        files = sorted(v2.glob("*/channels/*/raw/*.md")) if v2.is_dir() else []
        # 구조 1 — 채널당 파일 하나(`channels/<slug>__<id>.md`). 아직 수집이 쓰지
        # 않는 모양이라 운영 아카이브에는 하나도 없다. 그런데 읽는 쪽이 이걸 모르면
        # 구조 1 로 만든 아카이브가 **오류 없이 통째로 안 보인다** — 파일은 있고
        # 검색만 비어서, 실측이 「구조 1 이 빠르다」 로 나온다(아무것도 안 읽으니까).
        # 구조를 고르기 전에 두 배치를 같은 눈으로 읽을 수 있어야 한다.
        if v2.is_dir():
            files.extend(sorted(v2.glob("*/channels/*.md")))
        if dm_scope and v2.is_dir():
            from .writer import _slugify

            who = "*" if dm_scope == "*" else _slugify(dm_scope)
            files.extend(sorted(v2.glob(f"*/dm/{who}/raw/*.md")))
        if legacy.is_dir():
            legacy_files = sorted(legacy.glob("*/*.md"))
            files.extend(legacy_files)
            # 수집은 v2 로만 쓴다(`writer.py`). v1 은 전환 전 원문이 남아 있는 동안만
            # 읽는다. 이 글롭을 떼는 날 **파일은 그대로 있고 근거만 사라진다** —
            # 그 조용함을 막으려고, 아직 남아 있다는 사실을 기동 로그에 남긴다.
            if _legacy_changed(legacy, len(legacy_files)):
                logger.warning(
                    "v1 아카이브 %d개 파일이 아직 답변 근거로 쓰인다 (%s). "
                    "`scripts/diagnose_collection.py` 로 v2 이전 여부를 확인하라 — "
                    "이 경로를 떼기 전에 옮겨야 근거가 줄지 않는다.",
                    len(legacy_files), legacy,
                )
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

    def docs(self, *, dm_scope: str = "") -> list[ArchiveDoc]:
        loaded = self.source_docs(dm_scope=dm_scope)

        # 같은 이름의 문서에 **진짜 Slack ID** 가 있으면 그 ID 로 묶는다.
        # 마이그레이션 전후 원문이 답변에 두 벌로 들어가지 않게 하는 장치다.
        #
        # **만들어 낸 ID(`legacy-…`)를 신원으로 쓰지 않는다.** 그건 디렉터리 이름용
        # 자리표시자인데, 프론트매터에 실려 여기까지 오면 같은 채널이 둘로 갈린다.
        # 갈리면 출처가 두 줄 나오고, 더 나쁘게는 **권한 메타가 따로 계산된다** —
        # `visibility`·`acl`·`share_with` 를 그룹마다 정하므로 같은 채널이 한쪽으로는
        # 보이고 다른 쪽으로는 안 보일 수 있다(2026-09-07 실제 발생).
        #
        # **빈 표시명은 별칭이 되지 않는다**(B-61). `channel: #이름` 을 따옴표 없이
        # 적으면 프론트매터 파서가 `#` 를 주석으로 읽어 표시명이 빈 문자열이 된다.
        # 그 상태로 별칭을 만들면 표시명이 빈 문서 전부가 같은 키를 공유하고,
        # 진짜 ID 가 하나뿐일 때 **서로 관계없는 문서가 그 채널의 근거가 된다.**
        # 2026-09-17 QC 에서 다른 채널의 내용이 요약에 들어간 경로가 이것이다.
        real_id_sets: dict[tuple[str, str], set[str]] = {}
        for doc in loaded:
            if not doc.channel:
                continue
            if doc.channel_id and not is_synthetic_channel_id(doc.channel_id):
                real_id_sets.setdefault((doc.workspace, doc.channel), set()).add(doc.channel_id)
        # A display name is only a safe migration alias when it identifies exactly one
        # real Slack channel. Renamed channels can share a historical display name.
        real_ids = {
            key: next(iter(ids)) for key, ids in real_id_sets.items() if len(ids) == 1
        }
        grouped: dict[tuple[str, str], list[ArchiveDoc]] = {}
        for doc in loaded:
            own = doc.channel_id
            if own and is_synthetic_channel_id(own):
                own = None
            alias = real_ids.get((doc.workspace, doc.channel)) if doc.channel else None
            identity = own or alias or doc.channel_id or doc.channel
            if not identity:
                # 진짜 ID 도 표시명도 없다. **어느 채널에도 붙이지 않는다** —
                # 가까운 채널로 밀어 넣으면 그 채널 검토자가 본 적 없는 자료를
                # 승인하게 된다. 자기 파일 경로를 신원으로 삼아 혼자 남는다.
                identity = f"unidentified:{doc.path}"
                logger.warning(
                    "채널을 식별할 수 없는 원문 문서라 어느 채널에도 붙이지 않는다: %s",
                    doc.path,
                )
            grouped.setdefault((doc.workspace, identity), []).append(doc)
        merged = [self._merge(parts) for parts in grouped.values()]
        # **수정·삭제된 줄은 여기서 빠진다.** 한 자리에서 거르는 이유는 직접
        # 조회와 색인 조회가 같은 결과를 내야 하기 때문이다 — 색인 경로도
        # `visible_docs` 가 준 문서에서 줄을 찾는다(`_scan`·`candidates`).
        #
        # 감사 조회는 `audit_docs()` 로 간다. 같은 함수에 플래그를 두면 호출부
        # 하나가 기본값을 잘못 줘서 지워진 문장이 답변에 나간다.
        return [revision_reader.apply(doc) for doc in merged]

    def audit_docs(self, *, dm_scope: str = "") -> list[ArchiveDoc]:
        """**전부** 돌려준다 — 과거 revision 포함. 감사 조회 전용.

        일반 답변 경로는 절대 이걸 부르지 않는다. 부르면 지워진 문장이 근거가
        되고, 그건 지운 사람의 뜻을 뒤집는 것이다.
        """
        loaded = self.source_docs(dm_scope=dm_scope)
        grouped: dict[tuple[str, str], list[ArchiveDoc]] = {}
        for doc in loaded:
            grouped.setdefault(
                (doc.workspace, doc.channel_id or doc.channel), []
            ).append(doc)
        return [self._merge(parts) for parts in grouped.values()]

    def source_docs(self, *, dm_scope: str = "") -> list[ArchiveDoc]:
        """실제 원문 파일별 문서. 콘솔 파일 목록과 점검에 사용한다.

        DM 은 기본으로 빠진다(`_files`). 켜는 곳은 답변 권한 필터와 색인뿐이다.
        """
        loaded: list[ArchiveDoc] = []
        for p in self._files(dm_scope=dm_scope):
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
        seen: dict[tuple[str, str, str], int] = {}
        for doc in parts:
            for line in doc.raw_lines:
                key = (line.ts, line.speaker, line.text)
                at = seen.get(key)
                if at is None:
                    seen[key] = len(lines)
                    lines.append(line)
                elif line.message_ts and not lines[at].message_ts:
                    # 같은 줄이 좌표 있는 것과 없는 것으로 두 번 들어올 수 있다
                    # (좌표를 남기기 전에 수집한 파일). **좌표 있는 쪽을 남긴다** —
                    # 없는 쪽을 남기면 출처가 조용히 채널 링크로 내려앉는다.
                    lines[at] = line
        lines.sort(key=lambda line: (line.ts, str(line.source_path), line.lineno))
        return ArchiveDoc(
            path=newest.path,
            workspace=newest.workspace,
            channel=newest.channel,
            visibility="public" if all(d.visibility == "public" for d in parts) else "private",
            acl=frozenset(acl),
            share_with=frozenset(share_with),
            last_ingested=max((d.last_ingested or "" for d in parts), default="") or None,
            # **진짜 Slack ID 를 앞세운다.** 첫 번째 값을 그냥 쓰면 파일 이름 순서에
            # 따라 `legacy-…` 가 대표 ID 가 되고, 그 값이 콘솔·색인·조직 매핑으로
            # 흘러가 같은 채널이 또 둘로 보인다.
            channel_id=next(
                (
                    d.channel_id
                    for d in parts
                    if d.channel_id and not is_synthetic_channel_id(d.channel_id)
                ),
                next((d.channel_id for d in parts if d.channel_id), None),
            ),
            schema_version=max(d.schema_version for d in parts),
            # **떨어뜨리면 개인 기록이 조직 자료가 된다.** 이 값이 없으면
            # `can_access` 의 DM 관문을 아예 지나가지 않고, 통합조회 권한에
            # 그대로 열린다(2026-09-18 실제로 그랬다).
            dm_user=next((d.dm_user for d in parts if d.dm_user), None),
            org_code=newest.org_code,
            org_kind=newest.org_kind,
            org_name=newest.org_name,
            raw_lines=lines,
        )

    def broken(self) -> list[tuple[Path, str]]:
        """형식 검사 실패 목록. 운영 알림용."""
        return [(p, got) for p in self._files() if isinstance(got := self._load(p), str)]

    def visible_docs(self, ctx: RequestContext) -> list[ArchiveDoc]:
        """3겹/권한: 답변 생성 **이전에** 검색 범위를 축소한다.

        보관·삭제된 채널의 원문도 여기서 뺀다(B-51). 없앤 채널의 이야기가 오늘
        답에 섞이면 사람은 출처를 눌러도 확인할 수 없다 — 「틀린 자료」 보다
        나쁘다. 원문은 그대로 두고 **근거로 쓰는 것만** 막는다.

        설정을 **문서마다 읽지 않는다.** 한 요청에서 값이 바뀌면 같은 답 안에서
        어떤 문서는 들어오고 어떤 문서는 빠진다.
        """
        from ..channel_lifecycle import include_retired, keep

        allow_retired = include_retired()
        # 요청자가 특정되지 않으면 DM 은 아예 읽지 않는다. 특정돼도 **그 사람
        # 것만** 읽는다. 판정을 `can_access` 하나에만 기대지 않는다 — 개인 기록은
        # 막는 문이 둘이어야 하고, 하나는 파일을 열지 않는 문이어야 한다.
        return [
            d
            for d in self.docs(dm_scope=str(getattr(ctx, "user_id", "") or ""))
            if keep(d, allow_retired=allow_retired)
            and can_access(
                ctx,
                visibility=d.visibility,
                acl=d.acl if d.acl else None,
                owner_workspace=d.workspace,
                share_with=d.share_with if d.share_with else None,
                channel_id=d.channel_id,
                channel=d.channel,
                dm_user=d.dm_user,
            )
        ]

    def titles(self, ctx: RequestContext) -> list[str]:
        """검색 0건 폴백 — 권한 내 문서 제목 목록."""
        return [d.title for d in self.visible_docs(ctx)]

    def resolve_refs(self, refs, ctx: RequestContext) -> tuple[list[SearchHit], list[str]]:
        """이전 답변이 읽은 원문 좌표를 **지금 권한으로** 다시 연다.

        설계: `docs/design/thread-follow-up-evidence.md` §6.2

        후속 질문("방금 그 문서 다시 봐줘")이 이어 가는 것은 이전 답변 문장이
        아니라 그 답변이 읽은 줄이다. 그 줄을 지금 다시 읽어야 하는 이유는 둘이다.

        1. **권한은 그 사이에 바뀔 수 있다.** 지난주에 보였다는 사실은 지금도
           보여도 된다는 뜻이 아니다. 그래서 경로를 찾기 **전에** `visible_docs()`
           를 계산한다 — 순서가 반대면 권한 밖 문서의 존재 여부가 먼저 새어 나간다.
        2. **원문은 바뀔 수 있다.** 줄 번호만 믿으면 파일 앞쪽에 줄이 끼어든 순간
           엉뚱한 줄을 「그 문서의 그 줄」 로 답하게 된다. 지문이 맞아야 같은 줄이다.

        돌려주는 것은 `(hits, dropped_codes)`. 사유 코드는 업무 내용을 담지 않는
        고정 낱말이라 그대로 로그에 남길 수 있다.

        **복원에 실패했다고 채널 전체 검색으로 넓히지 않는다.** 호출자가 판단한다 —
        조용히 넓히면 사용자는 좁은 질문을 했는데 넓은 답을 받고, 그 사실을 알 수 없다.
        """
        from ..evidence_refs import ARCHIVE_LINE, content_hash, safe_relative_path

        items = list(refs or ())
        if not items:
            return [], []

        # 1. 권한이 경로 조회보다 먼저다.
        #
        # 채널 범위도 여기서 함께 걸린다 — `can_access()` 의 0번 판정이 "채널에서
        # 온 질문이면 그 채널만" 이다. **여기에 같은 검사를 또 쓰지 않는다.** 판정이
        # 두 곳으로 갈리면 한쪽만 고쳐도 오류가 안 나고, 그게 원칙 3이 막으려는
        # 모양 그대로다. DM(`ctx.channel_id` 없음)은 그 판정을 건너뛰므로 사용자가
        # 볼 수 있는 여러 채널이 함께 살아난다 — 설계 §9 가 요구하는 동작이다.
        visible = self.visible_docs(ctx)

        by_path: dict[str, tuple[ArchiveDoc, list[RawLine]]] = {}
        for doc in visible:
            for line in doc.raw_lines:
                key = self._rel(line.source_path or doc.path)
                entry = by_path.get(key)
                if entry is None:
                    by_path[key] = (doc, [line])
                else:
                    entry[1].append(line)

        known: set[str] | None = None  # 권한 밖 문서까지 포함한 경로 집합(지연 계산)
        hits: list[SearchHit] = []
        dropped: list[str] = []
        moved = 0  # 줄 번호는 어긋났지만 지문으로 찾은 건수

        def drop(code: str) -> None:
            if code not in dropped:
                dropped.append(code)

        for ref in items:
            if getattr(ref, "kind", "") != ARCHIVE_LINE:
                # 실시간 메시지는 아카이브에 없다. 다시 가져오는 것은 Slack 을 아는
                # 계층의 몫이라, 여기서는 조용히 빼고 사유만 남긴다.
                drop("live_not_archived")
                continue
            if ref.workspace and ref.workspace != ctx.workspace and not (
                ctx.is_root or ref.workspace in ctx.readable_workspaces
            ):
                drop("workspace_scope")
                continue
            # 경로는 감사 기록(JSONL)을 거쳐 돌아온 값이다. **파일에서 읽은 값을
            # 그대로 경로로 쓰는 것**이라, 여기서 검사하지 않으면 그 자리가 곧
            # 경로 탈출이다. `EvidenceRef.from_json()` 이 이미 보지만, 코드가
            # 직접 만든 참조도 같은 문을 지나게 한다.
            path = safe_relative_path(ref.document_path)
            if not path:
                drop("path_rejected")
                continue
            entry = by_path.get(path)
            if entry is None:
                if known is None:
                    known = {
                        self._rel(line.source_path or doc.path)
                        for doc in self.docs()
                        for line in doc.raw_lines
                    }
                # 문서가 사라진 것과 지금 권한으로 안 보이는 것은 사람이 할 일이
                # 다르다. 둘을 같은 코드로 적으면 권한 사고가 파일 정리처럼 보인다.
                drop("source_missing" if path not in known else "permission_changed")
                continue
            doc, lines = entry
            if ref.channel_id and (doc.channel_id or "") and ref.channel_id != doc.channel_id:
                drop("channel_scope")
                continue
            line = next(
                (
                    ln
                    for ln in lines
                    if ln.lineno == ref.line_no
                    and (
                        not ref.content_hash
                        or content_hash(ln.ts, ln.speaker, ln.text) == ref.content_hash
                    )
                ),
                None,
            )
            if line is None and ref.content_hash:
                # 줄이 밀렸을 수 있다. 같은 문서 안에서 지문으로 한 번만 더 찾는다.
                line = next(
                    (
                        ln
                        for ln in lines
                        if (not ref.source_ts or ln.ts == ref.source_ts)
                        and content_hash(ln.ts, ln.speaker, ln.text) == ref.content_hash
                    ),
                    None,
                )
                if line is not None:
                    moved += 1
            if line is None:
                drop("hash_mismatch")
                continue
            hits.append(SearchHit(doc=doc, line=line, score=1))

        logger.info(
            "refs 복원 ws=%s ch=%s 요청=%d 복원=%d 줄밀림=%d 제외=%s",
            ctx.workspace,
            ctx.channel_id or "-",
            len(items),
            len(hits),
            moved,
            "|".join(dropped) or "-",
        )
        return hits, dropped

    def _rel(self, path: Path) -> str:
        from ..search_index import rel_path

        return rel_path(path, self.root)

    def search(
        self,
        query: str,
        ctx: RequestContext,
        *,
        limit: int = 20,
        channels: frozenset[str] | None = None,
    ) -> list[SearchHit]:
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
        if channels is not None:
            # 전문 봇의 `where`는 검색 결과를 받은 뒤 자르는 조건이 아니다.
            # 먼저 권한을 적용한 문서에서 범위를 더 좁힌다. 반대로 이 인자로
            # visible_docs 밖 문서를 열 수는 없다.
            docs = [doc for doc in docs if doc.channel in channels]
        found = search_index.candidates(query, sorted({d.channel for d in docs if d.channel}))
        if found is None:
            return self._scan(query, tokens, docs, limit)

        # 색인이 넣은 것과 같은 실제 파일 좌표로 찾는다. 병합 문서의 `doc.path`는
        # 최신 일자 파일 하나뿐이지만 line_no는 일자 파일마다 다시 시작한다.
        indexed_lines: dict[tuple[str, int], tuple[ArchiveDoc, RawLine]] = {}
        expected_counts: dict[str, int] = {}
        for doc in docs:
            for line in doc.raw_lines:
                path = search_index.rel_path(line.source_path or doc.path, self.root)
                indexed_lines[(path, line.lineno)] = (doc, line)
                expected_counts[path] = expected_counts.get(path, 0) + 1
        hits: list[SearchHit] = []
        for cand in found:
            entry = indexed_lines.get((cand.doc_path, cand.line_no))
            # 색인에 있으나 지금 권한으로는 안 보이는 문서 — 조용히 건너뛴다.
            # 색인이 낡아 문서가 사라진 경우도 같은 자리로 떨어진다.
            if entry is None:
                continue
            doc, line = entry
            score = search_index.score_line(tokens, query, line.speaker, line.text)
            if score:
                hits.append(SearchHit(doc=doc, line=line, score=score))
        if not hits:
            # 색인이 아직 안 돌았을 수 있다. 0건으로 답하기 전에 파일을 한 번 본다 —
            # 「색인 없음」이 「자료 없음」으로 보이는 것이 이 기능의 가장 나쁜 실패다.
            return self._scan(query, tokens, docs, limit)

        # 색인에 **일부** 결과가 있으면 예전에는 여기서 끝났다. 그래서 방금 들어온
        # 줄(예: 재변환한 첨부)이 조용히 검색에서 빠졌다 — 오류도 0건도 아니라
        # 「예전 것만 나오는」 답이 된다(설계 §2.2·§7.2).
        #
        # 낡은 문서만 파일에서 보완한다. 전체 아카이브를 매번 훑지 않는다.
        hits += self._stale_hits(query, tokens, docs, expected_counts)
        return self._rank(hits, limit)

    def _stale_hits(self, query, tokens, docs, expected_counts) -> list[SearchHit]:
        """색인이 뒤처진 문서만 파일에서 찾아 보탠다.

        색인 경로와 **같은 점수 함수**를 쓴다(`_scan`). 다른 점수를 쓰면 같은 질문에
        어느 경로로 갔느냐에 따라 순서가 달라진다.
        """
        from .. import search_index

        counts = search_index.indexed_counts(list(expected_counts))
        if counts is None:
            # DB 를 못 봤다. 이미 색인 결과를 받은 뒤이므로 여기서 전체 스캔으로
            # 되돌아가지 않는다 — 두 번 부담을 지우는 대신 있는 것으로 답한다.
            return []
        stale_paths = {
            path
            for path, expected in expected_counts.items()
            # 적어도 같은 것이 아니라 **정확히 같아야** 최신이다. revision reader
            # 이전의 숨김 행이 남으면 DB 쪽 수가 더 많아지는 경우도 있다.
            if counts.get(path, 0) != expected
        }
        stale = [
            doc
            for doc in docs
            if any(
                search_index.rel_path(line.source_path or doc.path, self.root)
                in stale_paths
                for line in doc.raw_lines
            )
        ]
        if not stale:
            return []
        logger.info("색인이 뒤처진 문서 %d개를 파일에서 보완한다", len(stale))
        # 상한은 호출부의 `_rank` 가 다시 적용한다. 여기서 자르면 관련도 높은 줄이
        # 파일 순서 때문에 잘린다.
        return self._scan(query, tokens, stale, len(stale) * 20)

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
        # 같은 줄이 두 번 들어올 수 있다 — 색인 결과와 낡은 문서 파일 스캔이
        # 겹칠 때다. 그대로 두면 같은 사실이 두 번 인용되고, 근거 줄 수도 부풀려진다.
        #
        # 키는 **줄이 실제로 있는 파일**이어야 한다. `doc.path` 를 쓰면 안 된다 —
        # `_merge()` 가 일자 파일을 합치면서 모든 줄에 같은 `path`(가장 최근 파일)를
        # 달아 주는데, `lineno` 는 파일마다 1부터 다시 세므로 **다른 날의 다른 줄이
        # 같은 키가 된다.** 그러면 둘 중 하나가 검색 결과에서 조용히 빠진다.
        # 실측(PF 자료 43채널 2061줄): 2061줄 중 **1750줄**이 다른 줄과 키가 겹쳤다.
        # 오류도 0건도 아니고 「그 말은 없었다」 로만 보이는 종류의 실패다.
        unique: dict[tuple, SearchHit] = {}
        for hit in hits:
            key = (str(hit.line.source_path or hit.doc.path), hit.line.lineno)
            kept = unique.get(key)
            if kept is None or hit.score > kept.score:
                unique[key] = hit
        hits = list(unique.values())

        # 파이썬 정렬은 안정적이라, **덜 중요한 것부터 차례로** 정렬하면 된다.
        # 문자열을 음수화할 수 없으니 이 방식이 보수(complement) 트릭보다 읽기 쉽다.
        hits.sort(key=lambda h: (h.doc.path.name, h.line.lineno))
        hits.sort(key=lambda h: h.line.ts or "", reverse=True)   # 최근순
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]
