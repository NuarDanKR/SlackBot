"""첨부 정본을 **읽는 쪽.** 검색·답변이 쓰는 것은 여기서 고른 문서뿐이다.

결정: 2026-09-26 오너 지시(첨부 정본 reader) · 분리 설계 §2·§3.
쓰는 쪽: `attachment_writer.write_docs` · 형식: `attachment_doc.render`.

## 왜 따로 읽나

정본 경로는 `channels/<id>/attachments/<file-id>/<revision>.md` 라
`ArchiveStore._files()` 의 원문 글롭(`…/raw/*.md`)에 **한 장도 안 걸린다.**
그래서 지금까지는 파일이 있어도 검색이 못 봤다 — 오류 없이, 그냥 없는 자료였다.

그리고 형식도 다르다. 정본에는 `## 원문` 절이 없으므로 `load_doc` 이 스키마
위반으로 거절한다. 여기서 따로 판다.

## 무엇을 고르나

| | 일반 근거 | 감사 |
|---|---|---|
| 상태 | **`succeeded` 만** | 전부 |
| revision | 최신 판 하나를 고른 뒤 상태 확인 | 전부 |
| raw 중복 | 뺀다 | 그대로 |

**`partial` 을 뺀다.** 「본문이 나왔다」 와 「다 읽었다」 는 다르다. 절반만 읽힌
변환본을 근거로 쓰면, 뒤쪽에 있던 내용을 **「없다」 고 답하게 된다.** 없는 것과
못 읽은 것은 사람이 할 일이 다르다.

`failed`·`blocked`·`unsupported`·`pending` 도 같다. 문서는 남기되 근거가 아니다 —
「PII 로 막혔다」 는 사실이고 화면이 보여 주지만, 답변의 근거는 아니다.

## 같은 첨부를 두 번 세지 않는다

분리 스위치가 꺼져 있는 동안은 같은 본문이 **raw 에도** 있다
(`[첨부추출:이름]`). 둘 다 근거로 세면 답이 「두 군데서 확인됨」 처럼 보이는데
사실은 한 군데다. `attachment_doc.legacy_index` 가 채널+파일명으로 다리를 놓아
그런 정본을 뺀다. 스위치를 켜면 raw 에 본문이 없어지므로 다리가 스스로 걷힌다.

## 거르는 자리가 하나다

`ArchiveStore.docs()` 가 이걸 부르고, 색인 경로도 그 결과에서 줄을 찾는다
(`_scan`·`candidates`). `revision_reader` 와 같은 구조다 — 두 경로가 갈릴 수 없다.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .attachment_doc import (
    BLOCKED,
    CONVERTED,
    FAILED,
    NO_BODY_PREFIX,
    PARTIAL,
    PARTIAL_PREFIX,
    PENDING,
    UNSUPPORTED,
    AttachmentDoc,
    _safe,
    legacy_index,
)
from .store import ArchiveDoc, RawLine, parse_frontmatter

log = logging.getLogger("tybot.archive.attachment_reader")

#: 정본이 사는 자리. `AttachmentDoc.relative_path()` 와 **같은 모양**이어야 한다.
ATTACHMENT_GLOB = "*/channels/*/attachments/*/*.md"
SHADOW_ATTACHMENT_GLOB = "*/*__*/archive/attachments/*/*.md"

#: 일반 근거로 쓰는 상태. **`partial` 은 빠진다**(모듈 머리말).
#:
#: `attachment_doc.USABLE` 과 일부러 다르다. 그쪽은 「본문이 있나」 이고 여기는
#: 「근거로 써도 되나」 다. 한 이름으로 합치면 화면이 부분 변환본을 못 보게 되거나,
#: 답변이 부분 변환본을 전체로 읽거나 — 둘 중 하나가 된다.
GENERAL_EVIDENCE = (CONVERTED,)


def is_attachment_doc(doc) -> bool:
    """이 문서가 첨부 정본인가. **경로로 판정한다.**

    호출부마다 경로 문자열을 뒤지면 한 군데가 다른 규칙을 쓰고, 그때 첨부가
    채널 문서로 세어지거나 그 반대가 된다.
    """
    parts = Path(str(getattr(doc, "path", "") or "")).parts
    return "attachments" in parts


#: 첨부 캐시가 들고 있을 **원본 파일 바이트** 상한.
#:
#: 메모리에 올라간 모양은 파일보다 두 배 남짓 크다 — 본문 한 줄이 `RawLine`
#: 객체 하나가 되기 때문이다. 그래서 상한을 「파일 바이트」로 세고 배수는 여기
#: 적어 둔다. 바꿀 일이 생기면 `TYBOT_ATTACHMENT_CACHE_MB` 로 올린다.
DEFAULT_CACHE_MB = 192

#: 상한 초과 경고의 중복 억제도 캐시다. 본문을 안 들고 있어도 경로를 무한히
#: 기억하면 오래 도는 프로세스에서 다른 형태의 누수가 된다.
OVERSIZE_NOTICE_LIMIT = 1024


def _cache_bytes() -> int:
    raw = (os.getenv("TYBOT_ATTACHMENT_CACHE_MB") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_CACHE_MB
    except ValueError:
        value = DEFAULT_CACHE_MB
    return max(0, value) * 1024 * 1024


@dataclass(frozen=True)
class DocCacheStats:
    """첨부 캐시 누적 계수. 파일 좌표와 내용은 담지 않는다."""

    lookups: int
    hits: int
    misses: int
    invalidations: int
    evictions: int
    bypasses: int


class DocCache:
    """첨부 정본 한 장의 파싱 결과를 **파일 지문**으로 기억한다.

    ## 왜 필요했나 (2026-11-02 OOM)

    `evidence()` 는 부를 때마다 정본을 **전부 다시 읽었다.** 한 질문이
    `visible_docs()` 를 2~7 번 부르고(`_channels_matching` · `search` · 0건일 때의
    낱말별 재검색), 그 질문은 검색을 42 번 했다. 첨부 본문은 한 줄이 `RawLine`
    객체 하나가 되므로 파일의 두 배 남짓으로 부풀고, 그것이 **한 질문 안에서
    수십 번** 새로 만들어졌다 버려지기를 반복했다. 합성 86MB 아카이브로 재니
    `visible_docs()` 한 번이 195MB 를 찍었고, 첨부를 빼면 2.5MB 였다.

    원문 MD 는 이미 `ArchiveStore._load` 가 같은 방식으로 캐시한다. 첨부만
    빠져 있었다.

    ## 낡은 내용을 답하지 않는다

    열쇠는 경로이고 값에는 `(mtime_ns, st_size)` 지문이 붙는다. 변환 워커가
    정본을 다시 쓰면 지문이 달라져 그 자리에서 다시 읽는다. **캐시 때문에
    옛 변환본이 근거가 되는 일은 없다.**

    ## 상한은 바이트로 둔다 — 한 장도 예외가 아니다

    개수로 막으면 큰 PDF 변환본 몇 장이 상한을 통과하면서 메모리를 다 쓴다.
    넘치면 **오래 안 쓴 것부터 버린다.**

    **상한보다 큰 파일 한 장은 아예 들고 있지 않는다.** 「마지막 한 장은
    남긴다」 로 두면 그 한 장이 상한을 무시하고 상주한다 — 상한을 둔 이유가
    바로 큰 변환본이었는데, 그 경우에만 상한이 꺼지는 꼴이 된다. 버려도
    근거가 사라지지는 않는다. 다음 호출이 그 파일만 다시 읽을 뿐이고, 그건
    고치기 전과 같은 비용이지 더 나쁜 비용이 아니다.

    ## 같은 파일을 둘이 동시에 파싱하지 않는다

    `visible_docs()` 는 워크스페이스 봇들이 공유하는 인스턴스에서 불린다.
    두 요청이 같은 큰 정본을 동시에 만나면, 캐시가 채워지기 **전**이라 둘 다
    헛걸음하고 **같은 본문이 메모리에 두 벌** 올라간다. 한 벌이 200MB 면 그
    순간 400MB 다 — 고치려던 것과 같은 모양이다.

    그래서 경로마다 문을 하나 둔다. 먼저 들어간 쪽이 만들고, 뒤에 온 쪽은
    기다렸다가 **만들어진 것을 받는다.** 문은 경로별이라 서로 다른 파일은
    그대로 나란히 간다.
    """

    def __init__(self, max_bytes: int | None = None) -> None:
        self._max = _cache_bytes() if max_bytes is None else max(0, max_bytes)
        self._lock = threading.Lock()
        # path -> (지문, 파일 바이트, AttachmentDoc | None, ArchiveDoc | None)
        self._items: dict[Path, tuple[tuple[int, int], int, object, object]] = {}
        self._bytes = 0
        # 만드는 중인 경로의 문. `(Lock, 기다리는 수)` — 아무도 안 쓰면 치운다.
        self._gates: dict[Path, list] = {}
        #: 상한보다 커서 캐시에 못 넣은 경로. **지문만** 둔다(본문은 안 들고 있다).
        self.oversize: dict[Path, tuple[int, int]] = {}
        self._lookups = 0
        self._hits = 0
        self._misses = 0
        self._invalidations = 0
        self._evictions = 0
        self._bypasses = 0

    @property
    def held_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def stats(self) -> DocCacheStats:
        with self._lock:
            return DocCacheStats(
                lookups=self._lookups,
                hits=self._hits,
                misses=self._misses,
                invalidations=self._invalidations,
                evictions=self._evictions,
                bypasses=self._bypasses,
            )

    # -- 상한 ------------------------------------------------------------
    def _put(self, path: Path, fingerprint, size: int, doc, archive) -> None:
        with self._lock:
            old = self._items.pop(path, None)
            if old is not None:
                self._bytes -= old[1]
            if self._max <= 0 or size > self._max:
                self._bypasses += 1
                if self._max > 0 and self.oversize.get(path) != fingerprint:
                    self.oversize[path] = fingerprint
                    while len(self.oversize) > OVERSIZE_NOTICE_LIMIT:
                        self.oversize.pop(next(iter(self.oversize)))
                    log.warning(
                        "첨부 정본이 캐시 상한보다 커서 들고 있지 않는다 "
                        "(%d bytes > %d): %s — 상한은 TYBOT_ATTACHMENT_CACHE_MB",
                        size, self._max, path,
                    )
                return
            self.oversize.pop(path, None)
            self._items[path] = (fingerprint, size, doc, archive)
            self._bytes += size
            # dict 는 삽입 순서를 지키므로 맨 앞이 가장 오래 안 쓴 것이다.
            # 방금 넣은 것 자체가 상한 안이므로 이 고리는 반드시 멈춘다.
            while self._bytes > self._max and self._items:
                oldest = next(iter(self._items))
                if oldest == path:
                    break
                self._bytes -= self._items.pop(oldest)[1]
                self._evictions += 1

    def _peek(self, path: Path, fingerprint: tuple[int, int]):
        with self._lock:
            entry = self._items.get(path)
            if entry is None:
                return None
            if entry[0] != fingerprint:
                self._bytes -= self._items.pop(path)[1]
                self._invalidations += 1
                return None
            # 가장 최근에 쓴 것을 뒤로 보낸다(삽입 순서 = 사용 순서).
            self._items[path] = self._items.pop(path)
            return entry

    # -- 경로별 문 --------------------------------------------------------
    @contextmanager
    def _gate(self, path: Path):
        with self._lock:
            gate = self._gates.get(path)
            if gate is None:
                gate = self._gates[path] = [threading.Lock(), 0]
            gate[1] += 1
        gate[0].acquire()
        try:
            yield
        finally:
            gate[0].release()
            with self._lock:
                gate[1] -= 1
                if gate[1] <= 0:
                    self._gates.pop(path, None)

    @property
    def gates_open(self) -> int:
        """아직 치우지 않은 문의 수. 샌다면 여기가 자란다."""
        with self._lock:
            return len(self._gates)

    # -- 읽기 -------------------------------------------------------------
    def loaded(self, path: Path, build):
        """검증까지 끝난 `AttachmentDoc` (또는 `None`). 캐시에 없으면 `build()`."""
        with self._lock:
            self._lookups += 1
        fingerprint = _fingerprint(path)
        if fingerprint is None:
            with self._lock:
                self._misses += 1
                self._bypasses += 1
            return build()
        entry = self._peek(path, fingerprint)
        if entry is not None:
            with self._lock:
                self._hits += 1
            return entry[2]
        with self._gate(path):
            # 문 앞에서 기다리는 동안 앞사람이 채웠을 수 있다. 다시 본다 —
            # 이 재확인이 없으면 문은 순서만 세울 뿐 중복 파싱을 못 막는다.
            entry = self._peek(path, fingerprint)
            if entry is not None:
                with self._lock:
                    self._hits += 1
                return entry[2]
            with self._lock:
                self._misses += 1
            doc = build()
            self._put(path, fingerprint, fingerprint[1], doc, None)
            return doc

    def as_archive(self, path: Path, doc: AttachmentDoc, build):
        """검색이 쓰는 `ArchiveDoc`. 줄 객체를 질문마다 다시 만들지 않는다."""
        with self._lock:
            self._lookups += 1
        fingerprint = _fingerprint(path)
        if fingerprint is None:
            with self._lock:
                self._misses += 1
                self._bypasses += 1
            return build()
        entry = self._peek(path, fingerprint)
        if entry is not None and entry[3] is not None:
            with self._lock:
                self._hits += 1
            return entry[3]
        with self._gate(path):
            entry = self._peek(path, fingerprint)
            if entry is not None and entry[3] is not None:
                with self._lock:
                    self._hits += 1
                return entry[3]
            with self._lock:
                self._misses += 1
            archive = build()
            kept = entry[2] if entry is not None else doc
            self._put(path, fingerprint, fingerprint[1], kept, archive)
            return archive


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def source_files(root: Path | str) -> list[Path]:
    """정본 파일 목록. 없으면 빈 목록 — 아직 아무것도 안 쓴 상태다."""
    base = Path(root) / "workspaces"
    legacy = list(base.glob(ATTACHMENT_GLOB)) if base.is_dir() else []
    shadow = list(Path(root).glob(SHADOW_ATTACHMENT_GLOB))
    return sorted(legacy + shadow)


def _family_key(path: Path, root: Path | str) -> tuple[str, str, str] | tuple[str]:
    """Identity available from canonical and transition paths without opening files."""
    try:
        parts = path.resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        return (str(path.resolve().parent),)
    if (
        len(parts) == 7
        and parts[0] == "workspaces"
        and parts[2] == "channels"
        and parts[4] == "attachments"
    ):
        return (parts[1], parts[3].split("__", 1)[0], parts[5])
    if (
        len(parts) == 6
        and parts[2] == "archive"
        and parts[3] == "attachments"
    ):
        return (parts[0], parts[1].split("__", 1)[0], parts[4])
    return (str(path.resolve().parent),)


#: 없으면 문서로 세지 않는 칸. 하나라도 비면 그 문서는 **좌표가 없는 본문**이다 —
#: 출처를 붙일 수도, 권한을 판정할 수도, 중복을 막을 수도 없다.
REQUIRED_FIELDS = ("workspace", "channel_id", "file_id", "revision", "conversion_state")

#: 프론트매터가 말할 수 있는 공개 범위. 다른 값은 **모르는 값**이고, 모르면 막는다.
VALID_VISIBILITY = ("public", "private")

#: 우리가 쓰는 변환 상태. 모르는 값이면 본문을 어떻게 다룰지 알 수 없다 —
#: 「다 읽었다」 인지 「절반만」 인지 「막혔다」 인지가 답을 바꾼다.
KNOWN_STATES = (CONVERTED, PARTIAL, BLOCKED, FAILED, UNSUPPORTED, PENDING)

#: 채널 권한으로 **되돌릴 수 있는** 문제. 나머지는 사람이 봐야 한다.
#: `attachment_repair` 가 이 목록으로 고칠 것과 막을 것을 가른다.
RIGHTS_CODES = ("bad_visibility", "empty_acl")


@dataclass(frozen=True)
class Problem:
    """정본 한 장이 근거가 못 되는 이유. **코드와 문장을 함께 든다.**

    문장만 돌려주면 읽는 쪽이 문자열을 뒤져 분류하게 되고, 그날 문구를 고치면
    분류가 조용히 무너진다.
    """

    code: str
    message: str

    @property
    def rights(self) -> bool:
        """채널 권한으로 되돌릴 수 있나."""
        return self.code in RIGHTS_CODES


class BrokenDoc(ValueError):
    """정본으로 읽을 수 없다. **사유를 들고 있다.**"""


def validate(doc: AttachmentDoc, path: Path, root: Path | str) -> Problem | None:
    """이 문서를 근거로 써도 되나. 안 되면 **사유**를 돌려준다.

    ## 왜 검증하나

    정본은 우리가 쓴 파일이지만, 손으로 고쳐지거나 반쯤 쓰이거나 옛 형식으로
    남아 있을 수 있다. 그때 조용히 읽으면 세 가지가 난다.

    1. **권한이 없는 문서가 열린다** — ACL 이 비면 「제한 없음」 으로 읽힌다
       (2026-09-26 실제로 그랬다: `render` 가 `#` 로 시작하는 채널명을 맨 값으로
       적어 파서가 주석으로 읽었다)
    2. **엉뚱한 채널의 근거가 된다** — 경로와 프론트매터 좌표가 어긋난 경우
    3. **상태를 모른 채 본문을 쓴다** — 알 수 없는 `conversion_state`

    셋 다 오류가 안 난다. 그래서 여기서 막는다.
    """
    for field_name in REQUIRED_FIELDS:
        if not str(getattr(doc, field_name, "") or "").strip():
            return Problem("missing_field", f"필수 칸이 비었다: {field_name}")
    if doc.visibility not in VALID_VISIBILITY:
        return Problem("bad_visibility", f"알 수 없는 visibility: {doc.visibility!r}")
    if doc.visibility == "private" and not doc.acl:
        # 비공개인데 열쇠가 없다. 「아무도 못 본다」 가 아니라 판정 기준이 없는
        # 것이고, `can_access` 는 빈 ACL 을 제한 없음으로 읽을 수 있다.
        return Problem("empty_acl", "비공개 문서인데 ACL 이 비었다")
    if doc.conversion_state not in KNOWN_STATES:
        # 「다 읽었다」 인지 「절반만」 인지 모른 채 본문을 쓰면, 뒤쪽에 있던
        # 내용을 「없다」 고 답할 수 있다.
        return Problem(
            "unknown_state", f"알 수 없는 conversion_state: {doc.conversion_state!r}"
        )

    # 경로와 프론트매터가 **같은 좌표**를 말해야 한다. 어긋나면 어느 쪽이 참인지
    # 고를 근거가 없다 — 한쪽을 믿으면 다른 쪽 채널의 근거가 된다.
    expected = Path(root) / doc.relative_path()
    try:
        parts = Path(path).resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        parts = ()
    if len(parts) == 6 and parts[2] == "archive" and parts[3] == "attachments":
        workspace, channel_folder, _, _, file_id, revision = parts
        if (
            workspace == doc.workspace
            and channel_folder.startswith(f"{doc.channel_id}__")
            and bool(channel_folder.removeprefix(f"{doc.channel_id}__"))
            and file_id == _safe(doc.file_id)
            and revision == f"{_safe(doc.revision)}.md"
        ):
            return None
    try:
        same = expected.resolve() == Path(path).resolve()
    except OSError:
        same = str(expected) == str(path)
    if not same:
        return Problem("path_mismatch", f"경로와 좌표가 어긋난다: {path}")
    return None


def load(path: Path) -> AttachmentDoc | None:
    """정본 한 장. 못 읽으면 `None` — **조용히 건너뛰지는 않는다**(로그를 남긴다).

    `load_doc` 을 안 쓰는 이유: 정본에는 `## 원문` 절이 없어 그쪽 검사에서 거절된다.
    형식이 다른 것이지 잘못된 것이 아니다.
    """
    try:
        text = path.read_text(encoding="utf-8")
        front = parse_frontmatter(text)
    except Exception:  # noqa: BLE001 - 한 장이 깨져도 나머지는 읽는다
        log.warning("첨부 정본을 읽지 못했다: %s", path)
        return None
    if str(front.get("kind") or "") != "attachment":
        return None

    acl = front.get("acl") or ""
    if isinstance(acl, str):
        acl = [part.strip() for part in acl.split(",") if part.strip()]
    return AttachmentDoc(
        workspace=str(front.get("workspace") or ""),
        channel_id=str(front.get("channel_id") or ""),
        channel=str(front.get("channel") or ""),
        file_id=str(front.get("file_id") or ""),
        name=str(front.get("file_name") or ""),
        revision=str(front.get("revision") or ""),
        visibility=str(front.get("visibility") or "private"),
        acl=frozenset(acl),
        text=_body(text),
        conversion_state=str(front.get("conversion_state") or ""),
        message_ts=str(front.get("message_ts") or ""),
        permalink=str(front.get("permalink") or ""),
        filetype=str(front.get("filetype") or ""),
        sha256=str(front.get("sha256") or ""),
        staged_at=str(front.get("staged_at") or ""),
        converted_at=str(front.get("converted_at") or ""),
        converter_name=str(front.get("converter_name") or ""),
        converter_version=str(front.get("converter_version") or ""),
        error_code=str(front.get("error_code") or ""),
        human_upload_confirmed=str(front.get("human_upload_confirmed") or "").lower()
        == "true",
        source_path=path,
    )


def load_checked(
    path: Path, root: Path | str, *, cache: DocCache | None = None
) -> AttachmentDoc | None:
    """검증까지 통과한 문서만. 깨졌으면 `None` 이고 **로그가 남는다.**

    조용히 건너뛰지 않는 이유: 정본이 안 읽히면 그 자료는 답변에서 사라지는데,
    사라진 것과 없는 것은 화면에서 구분되지 않는다.

    `cache` 를 주면 파일 지문이 같은 동안 다시 읽지 않는다(`DocCache`).
    거절도 함께 기억한다 — 안 그러면 깨진 정본이 호출마다 경고를 다시 찍는다.
    """

    def build() -> AttachmentDoc | None:
        doc = load(path)
        if doc is None:
            return None
        problem = validate(doc, path, root)
        if problem is not None:
            log.warning("첨부 정본을 근거로 쓰지 않는다 (%s): %s", problem.message, path)
            return None
        return doc

    return build() if cache is None else cache.loaded(path, build)


def _body(text: str) -> str:
    """프론트매터 뒤의 본문. **안내 줄은 본문이 아니다.**

    제목(`# 이름`)은 메타다. 「본문이 없습니다」 안내도 마찬가지인데, 그쪽은 더
    위험하다 — 그대로 두면 「PII 로 막혔다」 라는 문장이 검색 결과로 나오고,
    그 자체가 근거처럼 보인다.
    """
    _, _, rest = text.partition("\n---\n")
    if not rest:
        return ""
    lines = rest.splitlines()
    while lines and (not lines[0].strip() or lines[0].startswith("# ")):
        lines.pop(0)
    kept = [
        line for line in lines
        if not line.startswith(NO_BODY_PREFIX) and not line.startswith(PARTIAL_PREFIX)
    ]
    return "\n".join(kept).strip()


def current_by_file(docs: list[AttachmentDoc]) -> list[AttachmentDoc]:
    """`(채널, file ID)` 마다 **최신 revision 하나.**

    `attachment_doc.pick_current` 는 file ID 만 본다. 워크스페이스와 채널까지
    넣는 이유는 좌표를 온전히 쓰기 위해서다 — 권한이 다른 둘이 하나로 접히는
    실수는 조용하고 크다. 워크스페이스가 다르면 회사 경계를 넘는다(원칙 4).

    최신 판정은 **`converted_at`** 이다. `staged_at` 은 metadata 를 쓴 시각이라
    재변환에서 둘이 갈리고, 그때 옛 변환본이 최신으로 올라올 수 있다. 없으면
    `staged_at` 으로 내려간다. 둘 다 없으면 바꾸지 않는다 — 모르는 것으로 아는
    것을 덮으면 최신이 옛것으로 밀린다.
    """
    newest: dict[tuple[str, str, str], AttachmentDoc] = {}
    for doc in docs:
        key = (doc.workspace, doc.channel_id, doc.file_id)
        current = newest.get(key)
        if current is None or _newer(doc, current):
            newest[key] = doc
    return sorted(
        newest.values(),
        key=lambda d: (d.workspace, d.channel_id, d.file_id, d.revision),
    )


def _stamp(doc: AttachmentDoc) -> str:
    """최신 판정에 쓰는 시각. 변환이 끝난 때가 먼저다."""
    return doc.converted_at or doc.staged_at or ""


def _newer(candidate: AttachmentDoc, current: AttachmentDoc) -> bool:
    mine, theirs = _stamp(candidate), _stamp(current)
    if not mine:
        return False
    if not theirs:
        return True
    return mine > theirs


def evidence(
    root: Path | str, channel_docs, *, cache: DocCache | None = None,
    paths: set[str] | None = None,
) -> list[AttachmentDoc]:
    """일반 근거로 쓸 첨부. 상태·revision·중복 셋 다 거른다."""
    return list(iter_evidence(root, channel_docs, cache=cache, paths=paths))


def iter_evidence(
    root: Path | str, channel_docs, *, cache: DocCache | None = None,
    paths: set[str] | None = None,
) -> Iterator[AttachmentDoc]:
    """Yield current evidence one file-id family at a time."""
    files = source_files(root)
    requested: set[Path] | None = None
    if paths is not None:
        base = Path(root).resolve()
        requested = set()
        families: set[tuple[str, ...]] = set()
        for value in paths:
            relative = Path(str(value or ""))
            if relative.is_absolute():
                continue
            candidate = (base / relative).resolve()
            if not candidate.is_relative_to(base):
                continue
            requested.add(candidate)
            families.add(_family_key(candidate, root))
        # converted_at, not the filename, selects the current revision. Read only
        # siblings of DB candidates so an old indexed revision cannot revive.
        files = [path for path in files if _family_key(path, root) in families]
    legacy = legacy_index(channel_docs)
    grouped: dict[tuple[str, ...], list[Path]] = {}
    for path in files:
        grouped.setdefault(_family_key(path, root), []).append(path)
    for siblings in grouped.values():
        loaded = [
            doc
            for doc in (
                load_checked(path, root, cache=cache) for path in siblings
            )
            if doc
        ]
        # A newer partial/failed revision must not expose an older successful body.
        for doc in current_by_file(loaded):
            if (
                doc.conversion_state not in GENERAL_EVIDENCE
                or not doc.text.strip()
                or legacy.has(doc.workspace, doc.channel_id, doc.name)
            ):
                continue
            if requested is not None and (
                doc.source_path is None or doc.source_path.resolve() not in requested
            ):
                continue
            yield doc


def all_revisions(root: Path | str) -> list[AttachmentDoc]:
    """**전부** — 실패·부분·대기·옛 revision 포함. 감사 조회 전용.

    일반 조회와 함수를 나누는 이유: 같은 함수에 플래그를 두면 호출부 하나가
    기본값을 잘못 줘서 부분 변환본이 답변 근거가 된다.
    """
    return [doc for doc in (load(path) for path in source_files(root)) if doc]


# ---------------------------------------------------------------------------
# 검색이 쓰는 모양으로
# ---------------------------------------------------------------------------


def as_archive_doc(doc: AttachmentDoc, root: Path | str) -> ArchiveDoc:
    """검색이 다루는 문서 모양. **채널 문서와 합치지 않는다.**

    합치면 사람 발언과 문서 본문이 한 줄기가 되고, 문서의 8월 금액과 사람이
    9월에 정정한 금액이 같은 목록에 오른다(절대 원칙 7 이 막으려던 것).

    줄마다 `message_ts` 를 실어 둔다 — 그 첨부가 붙어 있던 메시지 좌표다.
    출처를 누르면 파일이 올라온 자리로 간다(요구 6).
    """
    path = doc.source_path or Path(root) / doc.relative_path()
    stamp = _stamp(doc)[:16].replace("T", " ")
    speaker = doc.name or doc.file_id
    lines = [
        RawLine(
            ts=stamp, speaker=speaker, text=text, lineno=index,
            source_path=path, message_ts=doc.message_ts,
        )
        for index, text in enumerate(doc.text.splitlines(), start=1)
        if text.strip()
    ]
    return ArchiveDoc(
        path=path,
        workspace=doc.workspace,
        channel=doc.channel,
        visibility=doc.visibility,
        # **채널 ACL 을 그대로 쓴다.** 첨부는 그 채널에 올라온 것이므로 채널보다
        # 넓게 열릴 수 없다.
        acl=doc.acl,
        share_with=frozenset(),
        last_ingested=_stamp(doc) or None,
        channel_id=doc.channel_id,
        schema_version=1,
        raw_lines=lines,
    )


def archive_docs(
    root: Path | str, channel_docs, *, cache: DocCache | None = None,
    paths: set[str] | None = None,
) -> list[ArchiveDoc]:
    """`ArchiveStore.docs()` 가 채널 문서 뒤에 붙이는 것.

    `cache` 가 있으면 **줄 객체까지** 재사용한다. 첨부 본문은 한 줄이 `RawLine`
    하나가 되므로, 여기서 다시 만들면 한 질문이 같은 것을 수십 번 만든다.
    """
    docs = evidence(root, channel_docs, cache=cache, paths=paths)
    if cache is None:
        return [as_archive_doc(doc, root) for doc in docs]
    return [
        cache.as_archive(
            doc.source_path or Path(root) / doc.relative_path(),
            doc,
            lambda doc=doc: as_archive_doc(doc, root),
        )
        for doc in docs
    ]


def iter_archive_docs(
    root: Path | str, channel_docs, *, cache: DocCache | None = None,
    paths: set[str] | None = None,
) -> Iterator[ArchiveDoc]:
    """Yield searchable attachments without retaining the full archive list."""
    for doc in iter_evidence(root, channel_docs, cache=cache, paths=paths):
        if cache is None:
            yield as_archive_doc(doc, root)
            continue
        yield cache.as_archive(
            doc.source_path or Path(root) / doc.relative_path(),
            doc,
            lambda doc=doc: as_archive_doc(doc, root),
        )


def audit_archive_docs(root: Path | str) -> list[ArchiveDoc]:
    """감사 조회용. 과거 revision 과 실패·부분 변환본까지 전부."""
    return [as_archive_doc(doc, root) for doc in all_revisions(root)]
