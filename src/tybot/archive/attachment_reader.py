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
from pathlib import Path

from .attachment_doc import (
    CONVERTED,
    NO_BODY_PREFIX,
    PARTIAL_PREFIX,
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
        error_code=str(front.get("error_code") or ""),
    )


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

    `attachment_doc.pick_current` 는 file ID 만 본다. 채널까지 넣는 이유는
    좌표를 온전히 쓰기 위해서다 — 같은 ID 가 두 채널에 보이는 상황을 우리가 만든
    적은 없지만, 권한이 다른 두 채널이 하나로 접히는 실수는 조용하고 크다.

    최신 판정은 `staged_at` 이다. 없으면 바꾸지 않는다 — 모르는 것으로 아는 것을
    덮으면 최신이 옛것으로 밀린다.
    """
    newest: dict[tuple[str, str], AttachmentDoc] = {}
    for doc in docs:
        key = (doc.channel_id, doc.file_id)
        current = newest.get(key)
        if current is None or _newer(doc, current):
            newest[key] = doc
    return sorted(newest.values(), key=lambda d: (d.channel_id, d.file_id, d.revision))


def _newer(candidate: AttachmentDoc, current: AttachmentDoc) -> bool:
    if not candidate.staged_at:
        return False
    if not current.staged_at:
        return True
    return candidate.staged_at > current.staged_at


def evidence(root: Path | str, channel_docs) -> list[AttachmentDoc]:
    """일반 근거로 쓸 첨부. 상태·revision·중복 셋 다 거른다."""
    loaded = [doc for doc in (load(path) for path in source_files(root)) if doc]
    legacy = legacy_index(channel_docs)
    current = current_by_file(
        [doc for doc in loaded if doc.conversion_state in GENERAL_EVIDENCE and doc.text.strip()]
    )
    return [doc for doc in current if not legacy.has(doc.channel_id, doc.name)]


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
    stamp = (doc.staged_at or "")[:16].replace("T", " ")
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
        last_ingested=doc.staged_at or None,
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
