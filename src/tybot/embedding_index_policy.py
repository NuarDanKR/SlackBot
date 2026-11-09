"""Fail-closed DB authorization for B-12 embedding index calls.

The model registry and workspace policy are separate approvals.  In
particular, an externally hosted model is callable only when both rows approve
external transfer.  This module reads policy metadata only; it never receives
source text or calls a provider.
"""
from __future__ import annotations

from tybot.embedding_index_job import EXTERNAL, LOCAL, IndexAuthorization, IndexRequest

_KINDS = {"channel": 5, "attachment": 6, "dm": 7}

#: 실제로 구현된 provider의 전송 경계. 정책 행의 ``transport``는 승인 내용일 뿐
#: provider가 어디서 도는지를 바꾸지 못한다. 새 provider를 추가할 때 이 표에 그
#: 경계를 명시하지 않으면 색인은 닫힌다.
PROVIDER_TRANSPORTS = {"openai": EXTERNAL}


def authorize_index_request(connection, request: IndexRequest) -> IndexAuthorization:
    """Return one checked decision from the current workspace/model policy."""
    if not isinstance(request, IndexRequest):
        raise TypeError("request must be IndexRequest")
    with connection.cursor() as cur:
        cur.execute(
            """SELECT p.enabled, p.model_revision, p.transport, p.revision,
                      m.state, p.allow_channel, p.allow_attachment, p.allow_dm,
                      p.external_transfer_approved, m.external_approved, m.provider
                 FROM search_embedding_workspace_policy p
            LEFT JOIN search_embedding_model_revision m
                   ON m.revision = p.model_revision
                WHERE p.workspace = %s""",
            (request.workspace,),
        )
        row = cur.fetchone()
    if row is None:
        return _denied(request.model_revision, "unconfigured")
    if len(row) != 11:
        raise ValueError("embedding_policy_row_invalid")

    enabled, model_revision, transport, revision, model_state = row[:5]
    if transport not in {LOCAL, EXTERNAL}:
        raise ValueError("embedding_policy_transport_invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("embedding_policy_revision_invalid")
    policy_revision = f"workspace:{request.workspace}:r{revision}"
    model = str(model_revision or "")
    provider_transport = PROVIDER_TRANSPORTS.get(str(row[10] or ""))
    if provider_transport is None:
        raise ValueError("embedding_model_provider_invalid")
    kind_index = _KINDS.get(request.kind)
    kind_allowed = kind_index is not None and row[kind_index] is True
    external_approved = (
        provider_transport == EXTERNAL and row[8] is True and row[9] is True
    )
    allowed = (
        enabled is True
        and model == request.model_revision
        and model_state == "active"
        and kind_allowed
        and transport == provider_transport
        and (provider_transport == LOCAL or external_approved)
    )
    return IndexAuthorization(
        allowed=allowed,
        model_revision=request.model_revision,
        policy_revision=policy_revision,
        transport=provider_transport,
        external_transfer_approved=external_approved,
    ).checked(request.model_revision)


def _denied(model_revision: str, policy_revision: str) -> IndexAuthorization:
    return IndexAuthorization(
        allowed=False,
        model_revision=model_revision,
        policy_revision=policy_revision,
        transport=LOCAL,
    ).checked(model_revision)


__all__ = ["PROVIDER_TRANSPORTS", "authorize_index_request"]
