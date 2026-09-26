"""첨부 정본을 아카이브에 쓴다 — 본문을 raw 에서 떼어 낸다.

설계: `docs/design/archiving-bot-separation-2026-09-23.md` §3
회의(2026-09-23): 「Archiving Bot은 첨부파일을 별도로 저장하겠음」

## 왜 스위치를 두나
바꾸는 것은 **수집의 저장 모양**이다. 켜는 순간부터 새 첨부 본문이 raw 에 안 들어가고,
읽는 쪽이 준비되지 않았으면 그 본문은 **답변에서 사라진다.** 오류는 안 난다 —
「그 파일 내용이 검색에 안 잡힘」 으로만 보인다.

분리 결정 §6 이 그걸 중지조건으로 못박았다.

> 새 첨부 reader 가 없어 별도 본문이 답변에서 사라진다

그래서 기본은 **꺼짐**이다. 읽는 쪽이 붙고 실측으로 확인한 뒤 켠다. 스위치가 있는
것과 켜는 것은 다른 결정이고, 후자는 사람이 한다.

## 옛 것을 지우지 않는다
이미 raw 에 박힌 `[첨부추출:…]` 줄은 소급 삭제하지 않는다(§3). raw 는 편집 금지
대상이고, 지우면 그 줄을 가리키던 옛 출처가 끊긴다. 새 쓰기부터 달라질 뿐이다.

그래서 한동안 두 모양이 공존한다. 그 사이 같은 첨부를 두 번 세지 않는 일은
`attachment_doc.usable_evidence()` 가 맡는다.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .attachment_doc import AttachmentDoc, from_staged, render

logger = logging.getLogger("tybot.archive.attachment_writer")


def separate_attachments() -> bool:
    """첨부 본문을 raw 에서 떼어 낼 것인가.

    **기본은 꺼짐.** 읽는 쪽이 붙기 전에 켜면 그 본문이 조용히 답변에서 빠진다.
    """
    return os.getenv("ARCHIVE_SEPARATE_ATTACHMENTS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def raw_lines_for(result, *, separate: bool | None = None) -> list[str]:
    """이 첨부가 **원문에 남길** 줄.

    분리 전에는 참조 + 본문(지금까지 하던 대로), 분리 뒤에는 참조만.

    `StagedAttachmentResult` 를 그대로 받는 이유: 호출부가 `reference_lines` 와
    `lines` 중 무엇을 쓸지 매번 고르면 **한 군데가 빠진다.** 그 빠진 경로만 옛
    모양으로 쓰이고, 그건 「어떤 채널은 첨부가 두 번 잡힌다」 로 나타난다.
    """
    enabled = separate_attachments() if separate is None else separate
    if not enabled:
        return list(getattr(result, "lines", []) or [])
    reference = list(getattr(result, "reference_lines", []) or [])
    # 옛 결과 객체에는 `reference_lines` 가 없다. 그때 빈 목록을 주면 파일이
    # 있었다는 사실 자체가 사라지므로 첫 줄로 되돌아간다.
    if reference:
        return reference
    lines = list(getattr(result, "lines", []) or [])
    return lines[:1]


#: 같은 판인지 볼 때 **세지 않는 칸.** 시각은 변환 결과를 바꾸지 않는다.
#:
#: 같은 원본을 같은 변환기·같은 설정으로 다시 읽으면 본문은 같고 시각만 다르다.
#: 그걸 충돌로 보면 재변환 때마다 error 가 쌓이고, 진짜 충돌이 그 안에 묻힌다.
#: 반대로 본문이나 **권한·출처 칸**이 다르면 그건 다른 문서다 — 거기서 막는다.
VOLATILE_FIELDS = ("staged_at", "converted_at", "reprocessed_at")


def same_evidence(existing: str, incoming: str) -> bool:
    """두 정본이 **같은 근거**인가. 시각만 다른 것은 같은 근거다."""
    return _comparable(existing) == _comparable(incoming)


def _comparable(text: str) -> tuple:
    """시각을 뺀 프론트매터와 본문. 비교에만 쓴다."""
    from .store import parse_frontmatter

    front = {
        key: str(value)
        for key, value in (parse_frontmatter(text) or {}).items()
        if key not in VOLATILE_FIELDS
    }
    _, _, body = text.partition("\n---\n")
    return (tuple(sorted(front.items())), body.strip())


def write_docs(
    archive_root: Path | str,
    results,
    *,
    workspace: str,
    channel_id: str,
    channel: str,
    visibility: str,
    acl: frozenset[str],
) -> list[AttachmentDoc]:
    """staged 첨부들을 정본 문서로 쓴다. **쓴 것만 돌려준다.**

    하나가 실패해도 나머지를 쓴다 — 첨부 하나 때문에 그 메시지의 다른 첨부까지
    잃으면, 사람은 「일부만 들어왔다」 를 오류 없이 겪는다.

    수집을 막지 않는다. 정본을 못 써도 원본과 staging 은 이미 남아 있으므로
    나중에 다시 만들 수 있다. 반대로 수집이 멈추면 놓친 원본은 되돌릴 수 없다.
    """
    root = Path(archive_root)
    written: list[AttachmentDoc] = []
    for result in results:
        staged_meta = getattr(result, "metadata_path", None)
        if staged_meta is None:
            continue
        doc = from_staged(
            Path(staged_meta).parent,
            workspace=workspace,
            channel_id=channel_id,
            channel=channel,
            visibility=visibility,
            acl=acl,
        )
        if doc is None:
            continue
        path = root / doc.relative_path()
        body = render(doc)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # **같은 revision 경로를 다른 내용으로 덮지 않는다.**
            #
            # revision 은 원본·변환기·판·설정에서 결정적으로 나온다. 그러니 같은
            # 경로에 다른 내용이 나왔다면 그 넷 중 무언가가 revision 에 안 들어갔다는
            # 뜻이다 — 덮으면 그 변환본을 인용한 답변의 출처가 조용히 달라진다.
            #
            # 같은 내용이면 아무 일도 안 한다(멱등). 다르면 **쓰지 않고 알린다.**
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                if same_evidence(existing, body):
                    written.append(doc)
                    continue
                logger.error(
                    "같은 revision 경로에 다른 내용이 나왔다 file=%s rev=%s — "
                    "덮지 않는다. 변환기 신원이 revision 에 빠졌을 수 있다",
                    doc.file_id, doc.revision,
                )
                continue
            # 원자적으로 바꾼다. 반쯤 쓰인 문서를 검색이 읽으면 프론트매터가
            # 잘려 스키마 오류가 되고, 그 채널 문서가 통째로 빠진다.
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning(
                "첨부 정본을 쓰지 못했다 file=%s: %s", doc.file_id, type(exc).__name__
            )
            continue
        written.append(doc)
    return written
