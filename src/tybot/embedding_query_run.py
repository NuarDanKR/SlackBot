"""Shadow execution of one question through the B-12 vector path.

This is the first place the whole query chain is wired end to end:

    policy  ->  one embedding call  ->  candidates in an approved scope
            ->  resolve against the current ACL  ->  evidence coordinates

and it is deliberately wired **beside** the answer, not into it.  The result
object carries no ``SearchHit``, no score and no text, so there is nothing in
it that could be merged into a ranking by accident.  Mixing vector hits with
keyword hits is a separate decision that has to be measured (recall, latency,
cost) before it is made; a shadow run is how that measurement is collected.

What the boundary owns
----------------------

* **Nothing is sent before the policy says so.**  The policy is read first and
  :func:`~tybot.embedding_query_policy.require_query_authorization` has to pass.
  If the policy cannot be read at all, the question is not embedded -- an
  unreadable approval is not an approval.
* **Exactly one call.**  No retry, no fallback, no second provider.  A retry on
  a question doubles both the bill and the number of copies of that sentence
  sitting in someone else's logs, for a run whose output nobody is waiting on.
* **The provider is injected.**  This module does not pick a provider, a model
  or a transport, and does not read the model registry to find one.  The caller
  passes the embedder it already built and names what it is about to use; the
  policy decides whether that is allowed.
* **No bodies anywhere.**  The question, the vector and the provider's error
  text do not reach the result object, the log line, any ``repr``, or the
  database.  What is kept is counts and fixed words.
* **The ACL is re-checked after the vector lookup, twice.**  The index is a
  rebuildable cache that was written under yesterday's permissions, so every
  candidate is reopened through the caller's own window
  (``resolve_candidates``) and then checked again against the canonical text
  that window holds (``evidence_for``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tybot.archive.embedding_evidence import evidence_for
from tybot.embedding_index_job import EMBEDDING_FAILURE_OUTCOMES, EmbeddingFailure
from tybot.embedding_index_plan import EmbeddingInput
from tybot.embedding_query_policy import (
    QUERY_DECISIONS,
    QueryNotAuthorized,
    QueryRequest,
    authorize_query_request,
    require_query_authorization,
)
from tybot.embedding_resolver import resolve_candidates
from tybot.embedding_search import MAX_CANDIDATES, SearchScope, candidates
from tybot.evidence_refs import MAX_REFS, EvidenceRef

log = logging.getLogger("tybot.embedding_query_run")

#: Revision reported to the embedder for a question.  Questions are not
#: chunked, so there is no chunker revision to name.
QUERY_CHUNKER_REVISION = "query:whole"

DEFAULT_LIMIT = 50

#: Why a shadow run ended.  Fixed words: they are counted and shown to
#: operators, and an outcome that varied with the question would be the
#: question leaking out one bit at a time.
QUERY_OUTCOMES = frozenset(
    {
        "ok",
        "policy_denied",
        "embedding_failed",
        "db_unavailable",
        "no_candidates",
        "snapshot_changed",
    }
)

#: Drop codes that mean the archive moved under the index.  Everything else a
#: resolver can report -- above all ``source_not_visible`` -- is folded into
#: ``no_candidates`` on purpose: telling the caller apart "it changed" from
#: "you may not see it" would make the vector cache an oracle for documents
#: the requester cannot open.
SNAPSHOT_DROPS = frozenset(
    {
        "source_revision_changed",
        "chunk_hash_changed",
        "chunk_bounds_changed",
        "source_changed",
    }
)

_REFUSALS = frozenset({"request_invalid", "scope_mismatch"})
_UNREADABLE = frozenset({"policy_unreadable", "vector_index_unreadable"})

#: Every value ``detail`` may take.  A detail outside this set would mean a
#: provider message or a path had found its way in.
DETAIL_WORDS = frozenset(
    {""}
    | (QUERY_DECISIONS - {"allowed"})
    | EMBEDDING_FAILURE_OUTCOMES
    | SNAPSHOT_DROPS
    | _REFUSALS
    | _UNREADABLE
)


class QueryEmbedder(Protocol):
    """The one verb this module needs.  ``GatewayEmbedder`` satisfies it."""

    def embed(
        self,
        inputs: tuple[EmbeddingInput, ...],
        *,
        model_revision: str,
        policy_revision: str,
    ): ...


class _Once:
    """One call, enforced rather than promised.

    The no-retry rule is the kind that survives review and dies in a later
    edit.  Wrapping the injected embedder means a second call raises here
    instead of quietly costing money and shipping the question again.
    """

    def __init__(self, embedder: QueryEmbedder) -> None:
        self._embedder = embedder
        self.calls = 0

    def embed(self, inputs, *, model_revision: str, policy_revision: str):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("query_embedder_called_twice")
        return self._embedder.embed(
            inputs, model_revision=model_revision, policy_revision=policy_revision
        )


@dataclass(frozen=True)
class ShadowQueryResult:
    """What a shadow run is allowed to remember.

    Counts, fixed words, and reopenable coordinates.  No question, no vector,
    no text, no score, and no ``SearchHit`` -- there is deliberately nothing
    here that a ranking could consume.
    """

    outcome: str
    detail: str = ""
    model_revision: str = ""
    policy_revision: str = ""
    transport: str = ""
    embed_calls: int = 0
    candidate_count: int = 0
    resolved_count: int = 0
    refs: tuple[EvidenceRef, ...] = ()
    dropped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in QUERY_OUTCOMES:
            raise ValueError("unknown query outcome")
        if self.detail not in DETAIL_WORDS:
            raise ValueError("unknown query detail")
        if (self.outcome == "ok") != bool(self.refs):
            raise ValueError("ok requires evidence coordinates")
        for count in (self.candidate_count, self.resolved_count, self.embed_calls):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("counts must be non-negative integers")
        if self.embed_calls > 1:
            # One question, one call. A result that admits to two is a bug
            # report, not a measurement.
            raise ValueError("a question may be embedded at most once")
        if len(self.refs) > MAX_REFS:
            raise ValueError("too many evidence coordinates")


def run_shadow_query(
    question: str,
    /,
    *,
    workspace: str,
    model_revision: str,
    transport: str,
    policy_connection,
    embedder: QueryEmbedder,
    scope: SearchScope,
    sources,
    root: Path | str,
    limit: int = DEFAULT_LIMIT,
    connect: Callable[[], object | None] | None = None,
) -> ShadowQueryResult:
    """Run one question through the vector path without touching the answer.

    ``question`` is positional-only and is used in exactly one place: building
    the single :class:`EmbeddingInput` handed to the injected embedder.  It is
    not stored, logged, or returned.

    ``scope`` must already be the approved set of channels for this requester,
    and ``sources`` must be the window built from that same request --
    ``VisibleSources`` for the live path.  Both the resolver and
    ``evidence_for`` go through it, so the ACL decision is made once, in the
    place that owns it, and is never recomputed here.
    """
    if not isinstance(scope, SearchScope):
        raise TypeError("scope must be a checked SearchScope")
    if not callable(getattr(embedder, "embed", None)):
        raise TypeError("embedder must provide embed()")
    if not callable(getattr(sources, "open", None)) or not callable(
        getattr(sources, "entry", None)
    ):
        raise TypeError("sources must provide open() and entry()")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")

    try:
        request = QueryRequest(workspace, model_revision, transport)
    except ValueError:
        # An unusable request is refused, not guessed at. Nothing was sent.
        return _end("policy_denied", "request_invalid", model_revision, "", transport)
    if scope.workspace != request.workspace:
        # The approved channel set belongs to a different workspace than the
        # policy about to be read. Running anyway would search one workspace
        # under another's approval.
        return _end("policy_denied", "scope_mismatch", model_revision, "", transport)

    try:
        authorization = authorize_query_request(policy_connection, request)
        require_query_authorization(
            authorization, model_revision=model_revision, transport=transport
        )
    except QueryNotAuthorized as refusal:
        return _end(
            "policy_denied", refusal.decision, model_revision, "", transport
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable policy is not an approval
        log.warning("질의 임베딩 정책을 읽지 못했다: %s", type(exc).__name__)
        return _end("db_unavailable", "policy_unreadable", model_revision, "", transport)

    policy_revision = authorization.policy_revision
    once = _Once(embedder)
    try:
        batch = once.embed(
            (_input(question),),
            model_revision=model_revision,
            policy_revision=policy_revision,
        )
        vectors = tuple(getattr(batch, "vectors", ()) or ())
        if len(vectors) != 1:
            raise EmbeddingFailure("embedding_result_incomplete")
    except EmbeddingFailure as failure:
        return _end(
            "embedding_failed",
            failure.code,
            model_revision,
            policy_revision,
            transport,
            embed_calls=once.calls,
        )
    except Exception as exc:  # noqa: BLE001 - a provider body must not escape
        # ``from None`` is not enough on its own: the traceback is dropped here
        # because the message of a 4xx from an embedding endpoint routinely
        # contains the text it refused, and that text is the question.
        log.warning("질의 임베딩 실패: %s", type(exc).__name__)
        return _end(
            "embedding_failed",
            "embedding_call_failed",
            model_revision,
            policy_revision,
            transport,
            embed_calls=once.calls,
        )

    found = candidates(
        vectors[0],
        scope=scope,
        model_revision=model_revision,
        limit=limit,
        connect=connect,
    )
    del vectors, batch
    if found is None:
        return _end(
            "db_unavailable",
            "vector_index_unreadable",
            model_revision,
            policy_revision,
            transport,
            embed_calls=once.calls,
        )
    if not found:
        return _end(
            "no_candidates",
            "",
            model_revision,
            policy_revision,
            transport,
            embed_calls=once.calls,
        )

    resolution = resolve_candidates(found, open_visible=sources.open)
    evidence = evidence_for(
        resolution.resolved, sources, root, limit=min(limit, MAX_REFS)
    )
    dropped = _ordered(tuple(resolution.dropped) + tuple(evidence.dropped))
    refs = tuple(evidence.refs)
    if refs:
        outcome, detail = "ok", ""
    else:
        changed = next((code for code in dropped if code in SNAPSHOT_DROPS), "")
        outcome = "snapshot_changed" if changed else "no_candidates"
        detail = changed
    return _end(
        outcome,
        detail,
        model_revision,
        policy_revision,
        transport,
        embed_calls=once.calls,
        candidate_count=len(found),
        resolved_count=len(resolution.resolved),
        refs=refs,
        dropped=dropped,
    )


def _input(question: str) -> EmbeddingInput:
    """The one and only copy of the question this module makes."""
    import hashlib

    text = str(question or "")
    if not text.strip():
        raise EmbeddingFailure("embedding_request_invalid")
    return EmbeddingInput(
        chunk_no=0,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def _ordered(codes: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for code in codes:
        if code not in seen:
            seen.append(code)
    return tuple(seen)


def _end(
    outcome: str,
    detail: str,
    model_revision: str,
    policy_revision: str,
    transport: str,
    *,
    embed_calls: int = 0,
    candidate_count: int = 0,
    resolved_count: int = 0,
    refs: tuple[EvidenceRef, ...] = (),
    dropped: tuple[str, ...] = (),
) -> ShadowQueryResult:
    result = ShadowQueryResult(
        outcome=outcome,
        detail=detail,
        model_revision=str(model_revision or ""),
        policy_revision=str(policy_revision or ""),
        transport=str(transport or ""),
        embed_calls=embed_calls,
        candidate_count=candidate_count,
        resolved_count=resolved_count,
        refs=refs,
        dropped=dropped,
    )
    # Counts and fixed words only. No question, no vector, no path.
    log.info(
        "벡터 섀도 질의 결과=%s 사유=%s 호출=%d 후보=%d 복원=%d 좌표=%d 제외=%s",
        result.outcome,
        result.detail or "-",
        result.embed_calls,
        result.candidate_count,
        result.resolved_count,
        len(result.refs),
        "|".join(result.dropped) or "-",
    )
    return result


__all__ = [
    "DEFAULT_LIMIT",
    "DETAIL_WORDS",
    "QUERY_OUTCOMES",
    "SNAPSHOT_DROPS",
    "QueryEmbedder",
    "ShadowQueryResult",
    "run_shadow_query",
]
