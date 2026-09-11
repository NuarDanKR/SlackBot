"""후속 질문이 이어 갈 **근거 참조** — 답변 문장이 아니라 원문 좌표.

설계: [`docs/design/thread-follow-up-evidence.md`](../../docs/design/thread-follow-up-evidence.md)

## 왜 참조인가

같은 스레드에서 "방금 그 문서 다시 확인해줘" 라고 물으면, 이어 갈 것은 **이전 봇
답변 문장이 아니라 그 답변이 읽은 원문**이다. 문장을 이어 가면 요약을 근거로 다시
요약하게 되고(원칙 1 위반), 한 번 잘못 읽은 숫자가 대화 내내 사실로 굳는다.

그래서 남기는 것은 좌표뿐이다 — 어느 워크스페이스, 어느 채널, 어느 파일, 몇 번째
줄, 그 줄의 지문. 다음 질문에서 **현재 권한으로 그 좌표를 다시 열어** 읽는다.
권한이 그 사이에 바뀌었으면 열리지 않는다. 그것이 이 구조의 핵심이다.

## 무엇을 담지 않나

원문 텍스트, OCR 본문, 절대 경로, 파일명(첨부는 `file_id` 로만 식별).
`content_hash` 는 **비교용 지문**이라 원문을 되살릴 수 없다. 참조가 감사 기록
(JSONL)에 실리므로, 참조 자체가 원문 사본이 되면 아카이브 밖에 원문이 한 벌 더
생긴다.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

logger = logging.getLogger("tybot.evidence_refs")

ARCHIVE_LINE = "archive_line"
LIVE_MESSAGE = "live_message"
KINDS = (ARCHIVE_LINE, LIVE_MESSAGE)

# 한 답변이 남길 참조 상한. 감사 기록 한 줄이 커지는 것을 막는다 —
# 참조가 수백 개면 후속 질문의 범위가 「채널 전체」와 다를 바 없다.
MAX_REFS = 40
MAX_ATTACHMENT_REFS = 20

NUL = "\x00"


def content_hash(ts: str, speaker: str, text: str) -> str:
    """원문 한 줄의 지문.

    구분자로 NUL 을 쓴다. 필드 경계가 없으면 `ts="a", text="b"` 와
    `ts="ab", text=""` 가 같은 지문이 되고, 그건 다른 줄을 같은 줄로 보게 만든다.
    """
    raw = f"{ts or ''}{NUL}{speaker or ''}{NUL}{text or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_relative_path(value: str) -> str:
    """아카이브 뿌리 기준 상대 경로로 쓸 수 있는 값만 통과시킨다.

    참조는 감사 기록(JSONL)을 거쳐 돌아온다. 즉 **파일에서 읽은 값을 경로로 쓰는
    것**이고, 검사하지 않으면 그 자리가 곧 경로 탈출이다. 되돌려 주는 값이 빈
    문자열이면 호출자는 그 참조를 버린다.
    """
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    p = PurePosixPath(raw)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        return ""
    # 드라이브 문자가 붙은 윈도우 절대 경로(`C:/...`)는 PurePosixPath 가
    # 상대 경로로 본다. 콜론이 든 경로는 아카이브에 없다.
    if ":" in raw:
        return ""
    return p.as_posix()


@dataclass(frozen=True)
class EvidenceRef:
    """원문 한 줄을 **다시 열 수 있는** 좌표. 원문 자체는 들지 않는다."""

    kind: Literal["archive_line", "live_message"]
    workspace: str
    channel_id: str
    document_path: str = ""  # ARCHIVE_DIR 기준 상대 경로
    line_no: int = 0  # 빠른 탐색용 힌트. 이것만 믿지 않는다.
    source_ts: str = ""
    content_hash: str = ""
    message_ts: str = ""  # live_message 일 때 사용

    @property
    def is_live(self) -> bool:
        return self.kind == LIVE_MESSAGE

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, row: Any) -> EvidenceRef | None:
        """감사 기록에서 읽은 한 건. **모양이 틀리면 버린다.**

        구형 레코드에는 이 필드가 아예 없고, 손으로 고친 줄이 섞일 수도 있다.
        여기서 예외를 내면 스레드 문맥 조회 하나가 답변 전체를 막는다.
        """
        if not isinstance(row, dict):
            return None
        kind = str(row.get("kind") or "")
        if kind not in KINDS:
            return None
        workspace = str(row.get("workspace") or "")
        channel_id = str(row.get("channel_id") or "")
        if not workspace or not channel_id:
            return None
        try:
            line_no = int(row.get("line_no") or 0)
        except (TypeError, ValueError):
            line_no = 0
        path = safe_relative_path(str(row.get("document_path") or ""))
        if kind == ARCHIVE_LINE and not path:
            return None
        message_ts = str(row.get("message_ts") or "")
        if kind == LIVE_MESSAGE and not message_ts:
            return None
        return cls(
            kind=kind,  # type: ignore[arg-type]
            workspace=workspace,
            channel_id=channel_id,
            document_path=path,
            line_no=max(0, line_no),
            source_ts=str(row.get("source_ts") or ""),
            content_hash=str(row.get("content_hash") or ""),
            message_ts=message_ts,
        )


@dataclass(frozen=True)
class AttachmentRef:
    """첨부 하나. **파일명이 아니라 file_id 로 식별한다.**

    같은 이름의 파일이 여러 개 올라오는 것은 드문 일이 아니다. 이름을 키로 쓰면
    후속 질문이 다른 파일의 상태를 그 파일의 상태로 답한다 — 틀렸다는 신호가
    어디에도 안 나온다.

    이름·변환 오류 문자열·추출 본문은 복제하지 않는다. 화면에 보일 때
    `attachment_review.scan()` 의 **현재** 메타데이터에서 읽는다. 참조에 복제해
    두면 이미 변환에 성공한 파일을 계속 「실패」로 답하게 된다.
    """

    workspace: str
    channel_id: str
    file_id: str

    def to_json(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_json(cls, row: Any) -> AttachmentRef | None:
        if not isinstance(row, dict):
            return None
        workspace = str(row.get("workspace") or "")
        channel_id = str(row.get("channel_id") or "")
        file_id = str(row.get("file_id") or "")
        if not workspace or not channel_id or not file_id:
            return None
        if "/" in file_id or "\\" in file_id or file_id in (".", ".."):
            return None
        return cls(workspace=workspace, channel_id=channel_id, file_id=file_id)


def ref_from_hit(hit, root: Path | str) -> EvidenceRef | None:
    """검색 결과 한 줄 -> 참조.

    경로는 `line.source_path` 를 우선한다. 합쳐진 문서(`ArchiveStore.docs()`)의
    `doc.path` 는 여러 파일 중 하나일 뿐이라, 그걸 적으면 다른 날짜 파일의 줄에
    엉뚱한 파일 경로가 붙는다.
    """
    doc = getattr(hit, "doc", None)
    line = getattr(hit, "line", None)
    if doc is None or line is None:
        return None
    channel_id = str(getattr(doc, "channel_id", "") or "")
    if not channel_id:
        # 채널 ID 가 없으면 다음 요청에서 채널 범위를 확인할 수 없다.
        # 확인할 수 없는 참조는 남기지 않는다(막는 쪽이 기본값).
        return None
    source = getattr(line, "source_path", None) or getattr(doc, "path", None)
    if source is None:
        return None
    from .search_index import rel_path

    path = safe_relative_path(rel_path(source, root))
    if not path:
        return None
    return EvidenceRef(
        kind=ARCHIVE_LINE,
        workspace=str(getattr(doc, "workspace", "") or ""),
        channel_id=channel_id,
        document_path=path,
        line_no=int(getattr(line, "lineno", 0) or 0),
        source_ts=str(getattr(line, "ts", "") or ""),
        content_hash=content_hash(
            str(getattr(line, "ts", "") or ""),
            str(getattr(line, "speaker", "") or ""),
            str(getattr(line, "text", "") or ""),
        ),
    )


def refs_from_hits(hits, root: Path | str, *, limit: int = MAX_REFS) -> list[EvidenceRef]:
    """검색 결과 -> 참조 목록. 중복은 합치고 상한을 건다."""
    out: list[EvidenceRef] = []
    seen: set[tuple[str, str, int]] = set()
    for hit in hits or ():
        ref = ref_from_hit(hit, root)
        if ref is None:
            continue
        key = (ref.workspace, ref.document_path, ref.line_no)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
        if len(out) >= limit:
            break
    return out


def live_ref(workspace: str, channel_id: str, message_ts: str) -> EvidenceRef | None:
    """실시간으로 읽은 Slack 메시지 한 건의 참조.

    다음 요청에서는 **현재 권한으로 다시 가져와야** 근거가 된다. 가져올 수
    없으면 근거에서 빠진다 — 지난번에 보였다는 사실은 지금 보여도 된다는 뜻이
    아니다.
    """
    if not workspace or not channel_id or not message_ts:
        return None
    return EvidenceRef(
        kind=LIVE_MESSAGE,
        workspace=workspace,
        channel_id=channel_id,
        message_ts=str(message_ts),
    )


def refs_to_json(refs, *, limit: int = MAX_REFS) -> list[dict[str, Any]]:
    return [r.to_json() for r in list(refs or ())[:limit]]


def refs_from_json(rows) -> list[EvidenceRef]:
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:MAX_REFS]:
        ref = EvidenceRef.from_json(row)
        if ref is not None:
            out.append(ref)
    return out


def attachment_refs_to_json(refs, *, limit: int = MAX_ATTACHMENT_REFS) -> list[dict[str, str]]:
    return [r.to_json() for r in list(refs or ())[:limit]]


def attachment_refs_from_json(rows) -> list[AttachmentRef]:
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:MAX_ATTACHMENT_REFS]:
        ref = AttachmentRef.from_json(row)
        if ref is not None:
            out.append(ref)
    return out
