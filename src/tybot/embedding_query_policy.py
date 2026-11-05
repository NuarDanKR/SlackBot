"""Fail-closed DB authorization for embedding a *question* (B-12).

Indexing and querying are two different transfers and this module keeps them
that way.

* Indexing embeds archived source text.  That text was collected under the
  archive rules, screened for PII, and is already sitting in a file a reviewer
  can open.
* A query is what a person typed into Slack a second ago.  It has passed none
  of that, and it is frequently the most sensitive sentence in the exchange --
  people put the thing they do not know into the question.

So ``allow_query`` and ``query_external_transfer_approved`` live in their own
table (``search_embedding_query_policy``), default to false, and this module
never reads the index policy row.  Approving the index must not, by any path,
start sending questions to an external provider.

This module receives **metadata only** -- workspace, model revision, intended
transport.  The question body is not a parameter here and never will be: a
policy decision does not need it, and a function that accepts it ends up
logging it.  :class:`QueryRequest` bounds and pattern-checks every field, so a
sentence cannot be smuggled through one of them.

It does not call a provider, embed anything, or widen a search.  It returns a
decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields

LOCAL = "local"
EXTERNAL = "external"
TRANSPORTS = frozenset({LOCAL, EXTERNAL})

#: Identifier shape shared with the model registry.  Applied to every request
#: field: a question has spaces, punctuation and newlines, so it cannot pass as
#: a workspace or a revision even if a caller wires the arguments up wrongly.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: Every decision this module can return.  Fixed words: they are counted and
#: shown to operators, so they must not vary with the request.
QUERY_DECISIONS = frozenset(
    {
        "allowed",
        "unconfigured",
        "query_disabled",
        "model_mismatch",
        "model_inactive",
        "transport_mismatch",
        "query_external_not_approved",
    }
)


class QueryNotAuthorized(RuntimeError):
    """A refused question embedding.  ``str()`` is one fixed decision word."""

    def __init__(self, decision: str) -> None:
        if decision not in QUERY_DECISIONS or decision == "allowed":
            raise ValueError("unknown query decision")
        super().__init__(decision)

    @property
    def decision(self) -> str:
        return str(self)


@dataclass(frozen=True)
class QueryRequest:
    """Metadata about a question embedding.  Never the question.

    ``transport`` is what the caller is *about to use* -- taken from the model
    binding it holds -- not a preference.  The decision refuses when it differs
    from the policy row, so a registry that has moved to a hosted model cannot
    ride on a policy row that still says ``local``.
    """

    workspace: str
    model_revision: str
    transport: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("embedding_query_request_invalid")
        if self.transport not in TRANSPORTS:
            raise ValueError("embedding_query_request_invalid")


@dataclass(frozen=True)
class QueryAuthorization:
    """One checked decision.  Deliberately not an ``IndexAuthorization``.

    The two are separate types so that a query path handed an index decision
    fails at the boundary instead of quietly honouring an approval that was
    given for something else.
    """

    allowed: bool
    model_revision: str
    policy_revision: str
    transport: str
    decision: str
    external_transfer_approved: bool = False

    def checked(self, expected_model: str) -> QueryAuthorization:
        if self.model_revision != expected_model:
            raise ValueError("query_authorization_model_mismatch")
        if not self.policy_revision or self.policy_revision != self.policy_revision.strip():
            raise ValueError("query_authorization_policy_invalid")
        if self.transport not in TRANSPORTS:
            raise ValueError("query_authorization_transport_invalid")
        if self.decision not in QUERY_DECISIONS:
            raise ValueError("query_authorization_decision_invalid")
        if self.allowed != (self.decision == "allowed"):
            raise ValueError("query_authorization_decision_invalid")
        if (
            self.allowed
            and self.transport == EXTERNAL
            and not self.external_transfer_approved
        ):
            raise ValueError("query_external_transfer_not_approved")
        return self


def authorize_query_request(connection, request: QueryRequest) -> QueryAuthorization:
    """Read the workspace's question-embedding policy and decide.

    Only ``search_embedding_query_policy`` and the model registry are read.
    ``search_embedding_workspace_policy`` -- the index approval -- is not in
    this statement, and that absence is what the separation is made of.
    """
    if not isinstance(request, QueryRequest):
        raise TypeError("request must be QueryRequest")
    with connection.cursor() as cur:
        cur.execute(
            """SELECT q.allow_query, q.model_revision, q.transport, q.revision,
                      q.query_external_transfer_approved,
                      m.state, m.external_approved
                 FROM search_embedding_query_policy q
            LEFT JOIN search_embedding_model_revision m
                   ON m.revision = q.model_revision
                WHERE q.workspace = %s""",
            (request.workspace,),
        )
        row = cur.fetchone()
    if row is None:
        return _denied(request, "unconfigured", "query:unconfigured")
    if len(row) != 7:
        raise ValueError("embedding_query_policy_row_invalid")

    allow_query, model_revision, transport, revision = row[:4]
    query_approved, model_state, model_approved = row[4:]
    if transport not in TRANSPORTS:
        raise ValueError("embedding_query_policy_transport_invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("embedding_query_policy_revision_invalid")

    # Its own namespace. An index policy revision reads ``workspace:<ws>:r<n>``;
    # if the two ever print the same string, an audit cannot tell which
    # approval a call was made under.
    policy_revision = f"query:{request.workspace}:r{revision}"
    external_approved = (
        transport == EXTERNAL and query_approved is True and model_approved is True
    )

    if allow_query is not True:
        decision = "query_disabled"
    elif str(model_revision or "") != request.model_revision:
        decision = "model_mismatch"
    elif model_state != "active":
        decision = "model_inactive"
    elif transport != request.transport:
        decision = "transport_mismatch"
    elif transport == EXTERNAL and not external_approved:
        decision = "query_external_not_approved"
    else:
        decision = "allowed"

    return QueryAuthorization(
        allowed=decision == "allowed",
        model_revision=request.model_revision,
        policy_revision=policy_revision,
        transport=transport,
        decision=decision,
        external_transfer_approved=external_approved,
    ).checked(request.model_revision)


def require_query_authorization(
    authorization: QueryAuthorization,
    *,
    model_revision: str,
    transport: str,
) -> QueryAuthorization:
    """The gate a future query path calls before a vector is asked for.

    Rejects anything that is not a :class:`QueryAuthorization` -- an index
    decision has the wrong type, and that is the point -- and re-checks the
    model and transport the caller is actually about to use against the ones
    the decision was made for.
    """
    if not isinstance(authorization, QueryAuthorization):
        raise TypeError("authorization must be QueryAuthorization")
    authorization.checked(authorization.model_revision)
    if not authorization.allowed:
        raise QueryNotAuthorized(authorization.decision)
    if authorization.model_revision != model_revision:
        raise QueryNotAuthorized("model_mismatch")
    if authorization.transport != transport:
        raise QueryNotAuthorized("transport_mismatch")
    if transport == EXTERNAL and not authorization.external_transfer_approved:
        raise QueryNotAuthorized("query_external_not_approved")
    return authorization


def _denied(
    request: QueryRequest, decision: str, policy_revision: str
) -> QueryAuthorization:
    return QueryAuthorization(
        allowed=False,
        model_revision=request.model_revision,
        policy_revision=policy_revision,
        transport=LOCAL,
        decision=decision,
    ).checked(request.model_revision)


__all__ = [
    "EXTERNAL",
    "LOCAL",
    "QUERY_DECISIONS",
    "QueryAuthorization",
    "QueryNotAuthorized",
    "QueryRequest",
    "authorize_query_request",
    "require_query_authorization",
]
