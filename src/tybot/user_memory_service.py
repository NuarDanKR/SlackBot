"""Fail-soft runtime boundary for user-scoped memory.

The Slack layer supplies the preceding QA record. This module verifies that
lineage before writing and keeps database failures from blocking an answer.
It deliberately does not resolve evidence: callers must use the same current
ACL/revision reader as follow-up questions when building a context.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from .user_memory import (
    CLAIM,
    CORRECTION,
    MemoryContext,
    MemoryDecision,
    Store,
    UserMemory,
    build_context,
    classify_feedback,
)

log = logging.getLogger("tybot.user_memory")


@contextlib.contextmanager
def connect() -> Iterator[object | None]:
    """Open a short-lived writable connection, or disable memory safely."""
    dsn = str(os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        yield None
        return
    try:
        import psycopg

        with psycopg.connect(
            dsn,
            autocommit=False,
            connect_timeout=3,
            row_factory=psycopg.rows.dict_row,
        ) as connection:
            yield connection
    except Exception as exc:  # noqa: BLE001 - optional memory must not stop answers
        log.warning("개인 기억 DB를 사용할 수 없습니다: %s", exc)
        yield None


@dataclass(frozen=True)
class RememberResult:
    decision: MemoryDecision
    memory_id: int = 0
    saved: bool = False
    unavailable: bool = False


@dataclass(frozen=True)
class MemoryListResult:
    memories: tuple[UserMemory, ...] = ()
    unavailable: bool = False


@dataclass(frozen=True)
class RevokeResult:
    revoked: bool = False
    unavailable: bool = False


class UserMemoryService:
    def __init__(self, connector=connect) -> None:
        self._connect = connector

    @staticmethod
    def _owns_prior(prior: dict[str, Any], workspace: str, user_id: str) -> bool:
        return bool(
            prior
            and str(prior.get("record_id") or "").strip()
            and str(prior.get("workspace") or "") == workspace
            and str(prior.get("user") or "") == user_id
        )

    def remember(
        self,
        utterance: str,
        *,
        workspace: str,
        user_id: str,
        prior: dict[str, Any] | None,
    ) -> RememberResult:
        """Classify and save feedback only when the preceding turn is owned."""
        owned = prior if prior and self._owns_prior(prior, workspace, user_id) else {}
        decision = classify_feedback(
            utterance,
            prior_record_id=str(owned.get("record_id") or ""),
        )
        # DM 은 그 사람 한 명의 기록이다(dm-workspace). DM 에서 한 사실 정정은 공유
        # 정정 원장에 가지 않고 본인 claim 으로만 남는다.
        if decision.kind == CORRECTION and str(owned.get("channel_id") or "").startswith("D"):
            decision = replace(decision, kind=CLAIM)
        if not decision.stores:
            return RememberResult(decision)
        try:
            with self._connect() as connection:
                if connection is None:
                    return RememberResult(decision, unavailable=True)
                memory_id = Store(connection).save(
                    decision,
                    workspace=workspace,
                    user_id=user_id,
                    source_record_id=str(owned["record_id"]),
                    source_channel_id=str(owned.get("channel_id") or ""),
                    source_thread_ts=str(owned.get("thread_ts") or ""),
                    evidence_refs=owned.get("evidence_refs") or (),
                )
            return RememberResult(decision, memory_id=memory_id, saved=True)
        except Exception as exc:  # noqa: BLE001 - memory is not required to answer
            log.warning("개인 기억 저장 실패: %s", exc)
            return RememberResult(decision, unavailable=True)

    def context(
        self,
        *,
        workspace: str,
        user_id: str,
        question: str,
        claim_is_current: Callable[[UserMemory], bool],
        include_search_hints: bool = True,
    ) -> MemoryContext:
        """Load exact-user memory and apply the caller's current evidence check."""
        if not workspace.strip() or not user_id.strip():
            return MemoryContext()
        try:
            with self._connect() as connection:
                if connection is None:
                    return MemoryContext()
                memories = Store(connection).active(workspace, user_id)
                result = build_context(
                    memories,
                    question=question,
                    claim_is_current=claim_is_current,
                    include_search_hints=include_search_hints,
                )
                Store(connection).mark_used(
                    result.memory_ids,
                    workspace=workspace,
                    user_id=user_id,
                )
                return result
        except Exception as exc:  # noqa: BLE001 - memory is not required to answer
            log.warning("개인 기억 조회 실패: %s", exc)
            return MemoryContext()

    def revoke(
        self,
        memory_id: int,
        *,
        workspace: str,
        user_id: str,
        actor: str,
    ) -> bool:
        """Revoke only an exact-user row; unavailable storage changes nothing."""
        return self.revoke_result(
            memory_id,
            workspace=workspace,
            user_id=user_id,
            actor=actor,
        ).revoked

    def list_active(self, *, workspace: str, user_id: str) -> MemoryListResult:
        """List only the caller's active rows without treating DB failure as empty."""
        if not workspace.strip() or not user_id.strip():
            return MemoryListResult()
        try:
            with self._connect() as connection:
                if connection is None:
                    return MemoryListResult(unavailable=True)
                memories = Store(connection).active(workspace, user_id)
                return MemoryListResult(tuple(memories))
        except Exception as exc:  # noqa: BLE001 - memory is not required to answer
            log.warning("개인 기억 목록 조회 실패: %s", exc)
            return MemoryListResult(unavailable=True)

    def revoke_result(
        self,
        memory_id: int,
        *,
        workspace: str,
        user_id: str,
        actor: str,
    ) -> RevokeResult:
        """Revoke an owned row and distinguish absence from unavailable storage."""
        try:
            with self._connect() as connection:
                if connection is None:
                    return RevokeResult(unavailable=True)
                revoked = Store(connection).revoke(
                    memory_id,
                    workspace=workspace,
                    user_id=user_id,
                    actor=actor,
                )
                return RevokeResult(revoked=revoked)
        except Exception as exc:  # noqa: BLE001 - memory is not required to answer
            log.warning("개인 기억 철회 실패: %s", exc)
            return RevokeResult(unavailable=True)



# --- 사람이 남긴 사실 정정(2026-11-20 오너 결정) ---------------------------------


@dataclass(frozen=True)
class CorrectionResult:
    saved: bool = False
    correction_id: int = 0
    #: 저장하지 않은 이유. 고정 낱말: not_correction · no_evidence · dm_private ·
    #: private_source · corrector_cannot_open · unavailable
    reason: str = ""
    before: str = ""
    after: str = ""


@dataclass(frozen=True)
class CorrectionContext:
    prompt: str = ""
    #: 표시 후보. 답변이 정해진 뒤 같은 문서를 읽은 답에만 붙인다(`fact_correction.overlaps`).
    items: tuple = ()
    ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CorrectionListResult:
    corrections: tuple = ()
    unavailable: bool = False


class FactCorrectionService:
    """정정 원장의 fail-soft 경계. 권한 판정은 호출부가 넘긴 함수(현재 ACL)로만 한다."""

    def __init__(self, connector=connect) -> None:
        self._connect = connector

    def record(
        self,
        utterance: str,
        *,
        workspace: str,
        corrector_id: str,
        corrector_name: str,
        prior: dict[str, Any] | None,
        can_open: Callable[[list[dict[str, Any]]], bool],
    ) -> CorrectionResult:
        """정정 문장을 원장에 남긴다. 정정자가 **지금** 원문을 열 수 있어야 한다."""
        from .fact_correction import Store as CorrectionStore
        from .fact_correction import parse_change
        from .sensitive_mask import mask_sensitive

        change = parse_change(utterance)
        if change is None:
            return CorrectionResult(reason="not_correction")
        # 확인 문장과 LLM 에 넘기는 값도 가린 값이다(원칙 5).
        before = mask_sensitive(change.before).text
        after = mask_sensitive(change.after).text
        row = prior or {}
        if str(row.get("channel_id") or "").startswith("D"):
            return CorrectionResult(reason="dm_private", before=before, after=after)
        # 좌표로 다시 열 수 없는 근거(질문 첨부·실시간 Slack)를 읽은 답이면 공유하지
        # 않는다. 정정 값이 그 비공개 근거에서 나왔을 수 있는데, 공개 범위는 아카이브
        # 좌표로만 정해지므로 그 근거를 못 보는 사람에게도 보이게 된다.
        citations = " ".join(str(c) for c in (row.get("citations") or []))
        if row.get("attachment_refs") or "질문 첨부" in citations or "[실시간]" in citations:
            return CorrectionResult(reason="private_source", before=before, after=after)
        record_id = str(row.get("record_id") or "").strip()
        refs = [ref for ref in (row.get("evidence_refs") or []) if isinstance(ref, dict)]
        if (
            not record_id
            or not refs
            or str(row.get("workspace") or "") != workspace
        ):
            return CorrectionResult(reason="no_evidence", before=before, after=after)
        try:
            allowed = bool(can_open(refs))
        except Exception as exc:  # noqa: BLE001 - 확인 못 한 권한은 막는다
            log.warning("정정자 원문 권한 확인 실패: %s", type(exc).__name__)
            allowed = False
        if not allowed:
            return CorrectionResult(reason="corrector_cannot_open", before=before, after=after)
        try:
            with self._connect() as connection:
                if connection is None:
                    return CorrectionResult(reason="unavailable")
                correction_id = CorrectionStore(connection).save(
                    change,
                    workspace=workspace,
                    corrector_id=corrector_id,
                    corrector_name=corrector_name,
                    body=utterance,
                    subject=str(row.get("question") or ""),
                    source_record_id=record_id,
                    source_channel_id=str(row.get("channel_id") or ""),
                    evidence_refs=refs,
                )
            return CorrectionResult(
                saved=True, correction_id=correction_id, before=before, after=after,
            )
        except Exception as exc:  # noqa: BLE001 - 정정 저장 실패가 답을 막지 않는다
            log.warning("사실 정정 저장 실패: %s", exc)
            return CorrectionResult(reason="unavailable")

    def context(
        self,
        *,
        workspace: str,
        question: str,
        visible: Callable[[Any], bool],
    ) -> CorrectionContext:
        from .fact_correction import Store as CorrectionStore
        from .fact_correction import render_prompt, select

        if not workspace.strip() or not (question or "").strip():
            return CorrectionContext()
        try:
            with self._connect() as connection:
                if connection is None:
                    return CorrectionContext()
                chosen = select(
                    CorrectionStore(connection).active(workspace),
                    question=question,
                    visible=visible,
                )
                return CorrectionContext(
                    prompt=render_prompt(chosen),
                    items=chosen,
                    ids=tuple(item.id for item in chosen),
                )
        except Exception as exc:  # noqa: BLE001 - 정정은 답변의 필수 조건이 아니다
            log.warning("사실 정정 조회 실패: %s", exc)
            return CorrectionContext()

    def mark_shown(self, ids, *, workspace: str) -> None:
        from .fact_correction import Store as CorrectionStore

        if not ids:
            return
        try:
            with self._connect() as connection:
                if connection is not None:
                    CorrectionStore(connection).mark_shown(ids, workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            log.warning("사실 정정 표시 기록 실패: %s", exc)

    def list_own(self, *, workspace: str, corrector_id: str) -> CorrectionListResult:
        from .fact_correction import Store as CorrectionStore

        try:
            with self._connect() as connection:
                if connection is None:
                    return CorrectionListResult(unavailable=True)
                return CorrectionListResult(
                    tuple(CorrectionStore(connection).by_corrector(workspace, corrector_id))
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("사실 정정 목록 조회 실패: %s", exc)
            return CorrectionListResult(unavailable=True)

    def revoke_own(self, correction_id: int, *, workspace: str, corrector_id: str) -> RevokeResult:
        from .fact_correction import Store as CorrectionStore

        try:
            with self._connect() as connection:
                if connection is None:
                    return RevokeResult(unavailable=True)
                return RevokeResult(revoked=CorrectionStore(connection).revoke(
                    correction_id, workspace=workspace, actor=corrector_id,
                    reason="정정자 본인 철회", corrector_id=corrector_id,
                ))
        except Exception as exc:  # noqa: BLE001
            log.warning("사실 정정 철회 실패: %s", exc)
            return RevokeResult(unavailable=True)
