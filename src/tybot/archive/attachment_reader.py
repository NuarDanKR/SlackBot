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
| revision | 최신 하나 | 전부 |
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
    legacy_index,
)
from .store import ArchiveDoc, RawLine, parse_frontmatter

log = logging.getLogger("tybot.archive.attachment_reader")

#: 정본이 사는 자리. `AttachmentDoc.relative_path()` 와 **같은 모양**이어야 한다.
ATTACHMENT_GLOB = "*/channels/*/attachments/*/*.md"

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


def source_files(root: Path | str) -> list[Path]:
    """정본 파일 목록. 없으면 빈 목록 — 아직 아무것도 안 쓴 상태다."""
    base = Path(root) / "workspaces"
    return sorted(base.glob(ATTACHMENT_GLOB)) if base.is_dir() else []


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
    )


def load_checked(path: Path, root: Path | str) -> AttachmentDoc | None:
    """검증까지 통과한 문서만. 깨졌으면 `None` 이고 **로그가 남는다.**

    조용히 건너뛰지 않는 이유: 정본이 안 읽히면 그 자료는 답변에서 사라지는데,
    사라진 것과 없는 것은 화면에서 구분되지 않는다.
    """
    doc = load(path)
    if doc is None:
        return None
    problem = validate(doc, path, root)
    if problem is not None:
        log.warning("첨부 정본을 근거로 쓰지 않는다 (%s): %s", problem.message, path)
        return None
    return doc


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


def evidence(root: Path | str, channel_docs) -> list[AttachmentDoc]:
    """일반 근거로 쓸 첨부. 상태·revision·중복 셋 다 거른다."""
    loaded = [
        doc for doc in (load_checked(path, root) for path in source_files(root)) if doc
    ]
    legacy = legacy_index(channel_docs)
    current = current_by_file(
        [doc for doc in loaded if doc.conversion_state in GENERAL_EVIDENCE and doc.text.strip()]
    )
    return [
        doc for doc in current
        if not legacy.has(doc.workspace, doc.channel_id, doc.name)
    ]


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
    path = Path(root) / doc.relative_path()
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


def archive_docs(root: Path | str, channel_docs) -> list[ArchiveDoc]:
    """`ArchiveStore.docs()` 가 채널 문서 뒤에 붙이는 것."""
    return [as_archive_doc(doc, root) for doc in evidence(root, channel_docs)]


def audit_archive_docs(root: Path | str) -> list[ArchiveDoc]:
    """감사 조회용. 과거 revision 과 실패·부분 변환본까지 전부."""
    return [as_archive_doc(doc, root) for doc in all_revisions(root)]
