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
from dataclasses import dataclass
from typing import Any

from .user_memory import (
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

