"""후속 질문의 참조 범위를 **결정론으로** 정한다.

설계: [`docs/design/thread-follow-up-evidence.md`](../../docs/design/thread-follow-up-evidence.md) §9

## 이 파일이 하는 일 하나

"방금 그 문서 다시 확인해줘" 가 **무엇을 가리키는지**를 코드가 정한다. LLM 은
지칭이 있다는 것과 주제가 무엇인지까지만 제안하고(`intent.followup_hint`), 실제
QA 레코드와 원문 좌표는 여기서 고른다. 모델이 고르게 두면 모델이 지어낸 파일명·
경로·source ID 가 그대로 조회 대상이 된다.

## 넓히지 않는다

복원에 실패해도 채널 전체 검색으로 되돌아가지 않는다. 사용자는 좁게 물었는데
넓은 답을 받으면, 그 답이 자기 질문의 답이 아니라는 사실을 알 방법이 없다.
근거를 못 찾았으면 **못 찾았다고 답하는 것**이 맞다.

권한도 넓히지 않는다. 지난번에 보였다는 사실은 지금 보여도 된다는 뜻이 아니므로,
좌표는 전부 `ArchiveStore.resolve_refs()` 를 통과해야 근거가 된다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .access import RequestContext
from .attachment_review import Attachment
from .attachment_review import scan as scan_attachments
from .evidence_refs import AttachmentRef, EvidenceRef
from .intent import SINGULAR_FOLLOW_UP_RE, Intent

logger = logging.getLogger("tybot.thread_followup")

# 주제어가 걸린 줄 주변 몇 줄까지 함께 남길 것인가.
#
# 표와 목록은 **머리줄에만 주제어가 적힌다.** "미수금" 으로 거르면 `원기성금액`,
# `회수금액` 줄이 통째로 빠져, 주제는 맞는데 숫자가 없는 근거가 남는다.
TOPIC_CONTEXT_LINES = 3
# 지칭이 모호할 때 사용자에게 보일 후보 수.
MAX_CLARIFY_CHOICES = 5


@dataclass
class ResolvedFollowup:
    """후속 질문이 실제로 쓸 범위. 비어 있을 수 있고, 그때는 그렇게 답한다."""

    evidence_hits: list = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    parent_record_ids: list[str] = field(default_factory=list)
    topic_terms: list[str] = field(default_factory=list)
    resolution: str = "none"
    dropped_codes: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    # 사용자에게 물을 후보. 파일명은 담당자가 봐야 하므로 남기지만 본문은 열지 않는다.
    choices: list[str] = field(default_factory=list)
    refs_requested: int = 0

    @property
    def applied(self) -> bool:
        """이 범위를 답변에 적용해야 하는가."""
        return self.resolution != "none"

    @property
    def empty(self) -> bool:
        return not self.evidence_hits and not self.attachments

    def log_line(self) -> str:
        """업무 본문 없이 코드와 개수만(설계 §14)."""
        return (
            f"followup_resolution={self.resolution} "
            f"parent_records={len(self.parent_record_ids)} "
            f"refs_requested={self.refs_requested} "
            f"refs_resolved={len(self.evidence_hits)} "
            f"refs_dropped={max(0, self.refs_requested - len(self.evidence_hits))} "
            f"attachments_resolved={len(self.attachments)} "
            f"dropped_codes={'|'.join(self.dropped_codes) or '-'}"
        )


def _turn_refs(turn: dict) -> list[EvidenceRef]:
    return [r for r in (turn.get("evidence_refs") or []) if isinstance(r, EvidenceRef)]


def _turn_attachments(turn: dict) -> list[AttachmentRef]:
    return [r for r in (turn.get("attachment_refs") or []) if isinstance(r, AttachmentRef)]


def _matches_topic(turn: dict, topics: list[str]) -> bool:
    hay = " ".join(
        [str(turn.get("question") or "")] + [str(t) for t in (turn.get("subject_terms") or [])]
    ).lower()
    return any(t.lower() in hay for t in topics if t)


def _with_context(hits: list, kept: list) -> list:
    """주제어가 걸린 줄 주변을 함께 남긴다. 순서는 원래 순서를 지킨다."""
    if not kept:
        return []
    anchors: dict[str, set[int]] = {}
    for hit in kept:
        key = str(hit.line.source_path or hit.doc.path)
        anchors.setdefault(key, set()).add(hit.line.lineno)
    out = []
    for hit in hits:
        key = str(hit.line.source_path or hit.doc.path)
        near = anchors.get(key)
        if near and any(abs(hit.line.lineno - n) <= TOPIC_CONTEXT_LINES for n in near):
            out.append(hit)
    return out


class ThreadFollowupResolver:
    """같은 스레드의 이전 결과를 현재 권한으로 되살린다."""

    def __init__(self, store, *, archive_dir=None, scan=None) -> None:
        self._store = store
        self._archive_dir = archive_dir if archive_dir is not None else getattr(store, "root", None)
        # 첨부 메타데이터 읽기. 테스트에서 갈아 끼울 수 있게 주입받는다.
        self._scan = scan or scan_attachments

    def resolve(
        self,
        intent: Intent,
        ctx: RequestContext,
        *,
        turns: list[dict],
        channel_id: str = "",
    ) -> ResolvedFollowup:
        mode = intent.reference_mode
        if mode == "none" or not turns:
            return ResolvedFollowup()

        scope_channel = channel_id or ctx.channel_id or ""
        topics = [t for t in (intent.topic_terms or []) if t]

        # 참조를 남긴 turn 만 후보다. 구형 레코드(좌표 없음)는 이 경로로 오지
        # 않는다 — 호출자가 `thread_has_refs` 로 가른다.
        candidates = [t for t in turns if _turn_refs(t) or _turn_attachments(t)]
        if not candidates:
            return ResolvedFollowup(resolution=mode, dropped_codes=["no_prior_refs"])

        if mode == "prior_attachments":
            with_files = [t for t in candidates if _turn_attachments(t)]
            selected = with_files[-1:] or candidates[-1:]
        elif mode == "prior_topic" and topics:
            matched = [t for t in candidates if _matches_topic(t, topics)]
            selected = matched[-1:] if matched else candidates[-1:]
        else:
            selected = candidates[-1:]

        refs: list[EvidenceRef] = []
        file_refs: list[AttachmentRef] = []
        parents: list[str] = []
        dropped: list[str] = []
        for turn in selected:
            rid = str(turn.get("record_id") or "")
            if rid and rid not in parents:
                parents.append(rid)
            for ref in _turn_refs(turn):
                # 채널 질문은 **항상 현재 채널로 제한한다.** 이전 레코드에 다른
                # 채널 참조가 잘못 섞여 있어도 여기서 끊는다(설계 §16 권한).
                if scope_channel and ref.channel_id != scope_channel:
                    if "channel_scope" not in dropped:
                        dropped.append("channel_scope")
                    continue
                if ref not in refs:
                    refs.append(ref)
            for fref in _turn_attachments(turn):
                if scope_channel and fref.channel_id != scope_channel:
                    if "channel_scope" not in dropped:
                        dropped.append("channel_scope")
                    continue
                if fref not in file_refs:
                    file_refs.append(fref)

        hits, codes = self._store.resolve_refs(refs, ctx)
        for code in codes:
            if code not in dropped:
                dropped.append(code)

        if topics and hits:
            from . import search_index

            tokens = search_index.tokens_of(" ".join(topics))
            kept = [
                h
                for h in hits
                if tokens and search_index.score_line(tokens, "", h.line.speaker, h.line.text)
            ]
            if kept:
                hits = _with_context(hits, kept)
            else:
                # 주제에 맞는 줄이 없다. **다른 주제 문서를 끌어오지 않는다** —
                # 근거를 다시 찾지 못했다고 답하는 편이 맞다(설계 §10).
                hits = []
                dropped.append("topic_no_match")

        attachments = self._attachments(file_refs, hits, dropped, scope_channel)

        result = ResolvedFollowup(
            evidence_hits=hits,
            attachments=attachments,
            parent_record_ids=parents,
            topic_terms=topics,
            resolution=mode,
            dropped_codes=dropped,
            refs_requested=len(refs),
        )
        if (
            mode == "prior_attachments"
            and len(attachments) > 1
            and SINGULAR_FOLLOW_UP_RE.search(intent.question or "")
        ):
            # 어느 하나를 가리키는지 확정할 수 없다. **채널 전체 실패 목록으로
            # 넓히지 않고** 짧게 되묻는다(설계 §9-8).
            result.needs_clarification = True
            result.choices = [a.name for a in attachments[:MAX_CLARIFY_CHOICES] if a.name]
        logger.info("%s", result.log_line())
        return result

    def _attachments(
        self,
        refs: list[AttachmentRef],
        hits: list,
        dropped: list[str],
        scope_channel: str,
    ) -> list[Attachment]:
        """참조된 첨부의 **현재** 상태. 과거 문장이 아니라 메타데이터가 기준이다."""
        if self._archive_dir is None:
            return []
        try:
            staged = self._scan(self._archive_dir)
        except Exception as exc:  # noqa: BLE001 - 첨부 조회 실패가 답변을 막지 않는다
            logger.warning("첨부 메타데이터 조회 실패: %s", exc)
            if "attachment_scan_failed" not in dropped:
                dropped.append("attachment_scan_failed")
            return []
        by_id = {(a.workspace, a.channel_id, a.file_id): a for a in staged}

        out: list[Attachment] = []
        for ref in refs:
            item = by_id.get((ref.workspace, ref.channel_id, ref.file_id))
            if item is None:
                if "attachment_missing" not in dropped:
                    dropped.append("attachment_missing")
                continue
            out.append(item)
        if out or not hits:
            return out

        # 구형 레코드에는 첨부 좌표가 없다. 근거 줄에 적힌 이름으로 좁게 한 번만
        # 찾는다 — **같은 이름이 여럿이면 찾지 못한 것으로 본다.** 어느 파일인지
        # 모르는 채로 남의 파일 상태를 그 파일의 상태로 답하면 안 된다.
        from .answer import _attachment_names

        for workspace, channel, name in _attachment_names(hits):
            if scope_channel and channel != scope_channel:
                continue
            matches = [
                a
                for a in staged
                if a.workspace == workspace and a.channel_id == channel and a.name == name
            ]
            if len(matches) != 1:
                if matches and "attachment_ambiguous" not in dropped:
                    dropped.append("attachment_ambiguous")
                continue
            if matches[0] not in out:
                out.append(matches[0])
        return out
