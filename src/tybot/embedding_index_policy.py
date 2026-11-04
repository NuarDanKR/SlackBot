"""Fail-closed DB authorization for B-12 embedding index calls.

The model registry and workspace policy are separate approvals.  In
particular, an externally hosted model is callable only when both rows approve
external transfer.  This module reads policy metadata only; it never receives
source text or calls a provider.
"""
from __future__ import annotations

from tybot.embedding_index_job import EXTERNAL, LOCAL, IndexAuthorization, IndexRequest

_KINDS = {"channel": 5, "attachment": 6, "dm": 7}


def authorize_index_request(connection, request: IndexRequest) -> IndexAuthorization:
    """Return one checked decision from the current workspace/model policy."""
    if not isinstance(request, IndexRequest):
        raise TypeError("request must be IndexRequest")
    with connection.cursor() as cur:
        cur.execute(
            """SELECT p.enabled, p.model_revision, p.transport, p.revision,
                      m.state, p.allow_channel, p.allow_attachment, p.allow_dm,
                      p.external_transfer_approved, m.external_approved
                 FROM search_embedding_workspace_policy p
            LEFT JOIN search_embedding_model_revision m
                   ON m.revision = p.model_revision
                WHERE p.workspace = %s""",
            (request.workspace,),
        )
        row = cur.fetchone()
    if row is None:
        return _denied(request.model_revision, "unconfigured")
    if len(row) != 10:
        raise ValueError("embedding_policy_row_invalid")

    enabled, model_revision, transport, revision, model_state = row[:5]
    if transport not in {LOCAL, EXTERNAL}:
        raise ValueError("embedding_policy_transport_invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("embedding_policy_revision_invalid")
    policy_revision = f"workspace:{request.workspace}:r{revision}"
    model = str(model_revision or "")
    kind_index = _KINDS.get(request.kind)
    kind_allowed = kind_index is not None and row[kind_index] is True
    external_approved = (
        transport == EXTERNAL and row[8] is True and row[9] is True
    )
    allowed = (
        enabled is True
        and model == request.model_revision
        and model_state == "active"
        and kind_allowed
        and (transport == LOCAL or external_approved)
    )
    return IndexAuthorization(
        allowed=allowed,
        model_revision=request.model_revision,
        policy_revision=policy_revision,
        transport=transport,
        external_transfer_approved=external_approved,
    ).checked(request.model_revision)


def _denied(model_revision: str, policy_revision: str) -> IndexAuthorization:
    return IndexAuthorization(
        allowed=False,
        model_revision=model_revision,
        policy_revision=policy_revision,
        transport=LOCAL,
    ).checked(model_revision)


__all__ = ["authorize_index_request"]
