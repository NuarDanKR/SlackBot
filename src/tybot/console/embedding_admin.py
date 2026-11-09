"""B-12 임베딩 색인 **운영** 경계 — 모델 등록, 워크스페이스 승인, 현황.

## 왜 콘솔인가

색인의 판정·발행 코드는 다 있었지만 켜는 수단이 없었다. 모델 레지스트리와
워크스페이스 정책 표는 운영 DB 에 있는데 0행이었고, 그 표를 채우는 길은
psql 로 INSERT 를 치는 것뿐이었다. CLAUDE.md 「운영 기능은 콘솔에 둔다」 —
서버 명령을 칠 수 있는 사람이 한 명이면 기능은 없는 것과 같다.

## 저장은 실행이 아니다

여기서 하는 일은 행을 고쳐 쓰는 것뿐이다. 모델을 부르지 않고 색인을 시작하지
않는다. 실제 호출은 다음 타이머 회차(`tybot.embedding_index_cli`)가 문서마다
`authorize_index_request` 로 **다시** 판정한 뒤에 한다. 화면은 저장 성공을
「색인됨」 으로 표시하지 않는다.

## 켜는 것은 까다롭게, 끄는 것은 언제나

워크스페이스를 켤 때만 차단 사유를 본다. 끄기·외부 승인 철회·모델 퇴역은 막지
않는다(사용 중인 모델의 퇴역만 예외 — 켜진 정책이 전부 거절로 떨어진다).

## transport 칸은 사실과 맞아야 한다

`authorize_index_request` 는 `transport='local'` 이면 외부 전송 승인을 보지 않는다.
그 칸이 사람이 적는 값이라, OpenAI 모델을 `local` 로 적으면 승인 없이 원문이
나간다. 그래서 외부 프로바이더 모델은 `external` 이 아니면 저장하지 않는다.
실행기(`embedding_index_cli.misdeclared_policies`)도 같은 대조를 한 번 더 한다.

## 본문은 다루지 않는다

이 모듈이 읽는 것은 정책·레지스트리·개수뿐이다. 분량 추정도 `raw_line` 의 글자
수 합계만 읽는다. 감사 레코드에는 무엇이 바뀌었는지와 사유만 들어간다.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

from ..embedding_index_job import EXTERNAL
from ..embedding_index_policy import PROVIDER_TRANSPORTS

#: 임베딩 어댑터가 있는 프로바이더. `build_embedding_providers()` 의 키와 같아야
#: 한다 — 시험이 둘을 대조한다. 여기서 그 함수를 부르지 않는 이유: 부르면
#: 어댑터가 생성되며 API 키 조회가 일어난다. 목록을 보는 데 키가 필요하지 않다.
SUPPORTED_PROVIDERS = ("openai",)

MAX_REASON_CHARS = 500
MAX_MODEL_CHARS = 200
MAX_DIMENSIONS = 16000
MODEL_STATES = ("draft", "active", "retired")
TRANSPORTS = ("local", "external")

_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKSPACE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class EmbeddingAdminError(RuntimeError):
    """콘솔에 그대로 내보내도 되는 고정 코드 거절."""

    def __init__(self, code: str, *, blockers: tuple[str, ...] = ()) -> None:
        self.code = code
        self.blockers = blockers
        super().__init__(code)


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise EmbeddingAdminError("database_unconfigured")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception:  # noqa: BLE001 - DSN 이 예외 본문에 들어 있다
        raise EmbeddingAdminError("database_unavailable") from None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_actor_reason(actor: str, reason: str) -> None:
    if not _text(actor):
        raise EmbeddingAdminError("actor_required")
    if not _text(reason):
        raise EmbeddingAdminError("reason_required")
    if len(reason) > MAX_REASON_CHARS:
        raise EmbeddingAdminError("reason_too_long")


def _audit(actor: str, action: str, target_type: str, target_id: str, *,
           workspace: str | None, metadata: dict) -> dict:
    """`audit_store.record(**audit)` 에 그대로 넣을 수 있는 모양."""
    return {
        "actor": actor,
        "category": "search",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "workspace": workspace,
        "outcome": "succeeded",
        "metadata": metadata,
    }


# --- 모델 레지스트리 ----------------------------------------------------------


@dataclass(frozen=True)
class ModelRegistration:
    revision: str
    provider: str
    model: str
    dimensions: int
    normalized: bool
    price_per_mtok: float | None
    reason: str
    actor: str


@dataclass(frozen=True)
class ModelChange:
    revision: str
    state: str
    external_approved: bool
    reason: str
    actor: str


@dataclass(frozen=True)
class Saved:
    """저장 결과와 라우트가 기록할 감사 레코드."""

    view: dict
    audit: dict = field(default_factory=dict)


def _validate_registration(req: ModelRegistration) -> None:
    _check_actor_reason(req.actor, req.reason)
    if not isinstance(req.revision, str) or not _REVISION.fullmatch(req.revision):
        raise EmbeddingAdminError("revision_invalid")
    if req.provider not in SUPPORTED_PROVIDERS:
        # 어댑터가 없는 프로바이더를 등록해 두면 켜는 순간 전부 실패한다.
        raise EmbeddingAdminError("provider_unsupported")
    if not isinstance(req.model, str) or not _MODEL_NAME.fullmatch(req.model):
        raise EmbeddingAdminError("model_invalid")
    if type(req.dimensions) is not int or not 1 <= req.dimensions <= MAX_DIMENSIONS:
        raise EmbeddingAdminError("dimensions_invalid")
    if type(req.normalized) is not bool:
        raise EmbeddingAdminError("normalized_invalid")
    price = req.price_per_mtok
    if price is not None and (
        isinstance(price, bool) or not isinstance(price, int | float)
        or not math.isfinite(price) or price < 0
    ):
        raise EmbeddingAdminError("price_invalid")


def register_model(req: ModelRegistration, *, connect=None) -> Saved:
    """모델 revision 을 `draft` 로 등록한다. 외부 승인은 **꺼진 채로** 시작한다.

    등록과 승인을 한 번에 하지 않는 이유: 같은 화면 한 번의 저장으로 「새 모델 +
    외부 전송 허가」 가 되면 승인이 등록의 부속물이 된다. 승인은 따로 사유를 적는다.
    """
    _validate_registration(req)
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM search_embedding_model_revision WHERE revision=%s",
                (req.revision,),
            )
            if cur.fetchone() is not None:
                raise EmbeddingAdminError("model_exists")
            cur.execute(
                """INSERT INTO search_embedding_model_revision
                       (revision, provider, model, dimensions, normalized,
                        input_price_per_mtok, state, external_approved, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, 'draft', false, %s)""",
                (req.revision, req.provider, req.model, req.dimensions,
                 req.normalized, req.price_per_mtok, req.actor),
            )
    except EmbeddingAdminError:
        raise
    except Exception:  # noqa: BLE001 - DB 예외 본문에는 행 값이 들어 있다
        raise EmbeddingAdminError("registry_unavailable") from None
    return Saved(
        view={"revision": req.revision, "state": "draft", "externalApproved": False},
        audit=_audit(req.actor, "embedding_model_register", "embedding_model", req.revision,
                     workspace=None, metadata={
                         "reason": req.reason, "provider": req.provider, "model": req.model,
                         "dimensions": req.dimensions,
                     }),
    )


def change_model(req: ModelChange, *, connect=None) -> Saved:
    """모델 상태와 외부 전송 승인을 바꾼다. 차원·모델명은 바꾸지 않는다.

    차원이나 모델이 바뀌면 그것은 **다른 revision** 이다. 같은 revision 아래에서
    바꾸면 이미 쌓인 벡터와 새 벡터가 한 색인에 섞이고, 표에는 그 흔적이 없다.
    """
    _check_actor_reason(req.actor, req.reason)
    if not isinstance(req.revision, str) or not _REVISION.fullmatch(req.revision):
        raise EmbeddingAdminError("revision_invalid")
    if req.state not in MODEL_STATES:
        raise EmbeddingAdminError("state_invalid")
    if type(req.external_approved) is not bool:
        raise EmbeddingAdminError("external_approved_invalid")
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT state, external_approved FROM search_embedding_model_revision
                    WHERE revision=%s FOR UPDATE""",
                (req.revision,),
            )
            row = cur.fetchone()
            if row is None:
                raise EmbeddingAdminError("model_unknown")
            before_state = str(row["state"])
            if before_state == "retired" and req.state != "retired":
                # 퇴역은 되돌리지 않는다. 다시 쓰려면 새 revision 으로 등록한다 —
                # 퇴역 사이에 바뀐 청커·정책과 옛 벡터가 섞이지 않게.
                raise EmbeddingAdminError("model_retired")
            if req.state != "active" and before_state == "active":
                cur.execute(
                    """SELECT workspace FROM search_embedding_workspace_policy
                        WHERE enabled AND model_revision=%s ORDER BY 1""",
                    (req.revision,),
                )
                in_use = [str(r["workspace"]) for r in cur.fetchall()]
                if in_use:
                    raise EmbeddingAdminError(
                        "model_in_use", blockers=tuple(f"workspace_{w}" for w in in_use)
                    )
            cur.execute(
                """UPDATE search_embedding_model_revision
                      SET state=%s, external_approved=%s
                    WHERE revision=%s""",
                (req.state, req.external_approved, req.revision),
            )
    except EmbeddingAdminError:
        raise
    except Exception:  # noqa: BLE001
        raise EmbeddingAdminError("registry_unavailable") from None
    return Saved(
        view={"revision": req.revision, "state": req.state,
              "externalApproved": req.external_approved},
        audit=_audit(req.actor, "embedding_model_change", "embedding_model", req.revision,
                     workspace=None, metadata={
                         "reason": req.reason, "previous_state": before_state,
                         "state": req.state,
                         "previous_external_approved": bool(row["external_approved"]),
                         "external_approved": req.external_approved,
                     }),
    )


# --- 워크스페이스 정책 --------------------------------------------------------


@dataclass(frozen=True)
class PolicyChange:
    workspace: str
    #: 지금 행의 revision. 행이 없으면 None — 그때는 새로 만든다.
    expected_revision: int | None
    enabled: bool
    model_revision: str | None
    transport: str
    allow_channel: bool
    allow_attachment: bool
    external_transfer_approved: bool
    reason: str
    actor: str


def _validate_policy(req: PolicyChange) -> None:
    _check_actor_reason(req.actor, req.reason)
    if not isinstance(req.workspace, str) or not _WORKSPACE.fullmatch(req.workspace):
        raise EmbeddingAdminError("workspace_invalid")
    if req.expected_revision is not None and (
        type(req.expected_revision) is not int or req.expected_revision <= 0
    ):
        raise EmbeddingAdminError("revision_invalid")
    for flag in (req.enabled, req.allow_channel, req.allow_attachment,
                 req.external_transfer_approved):
        if type(flag) is not bool:
            raise EmbeddingAdminError("policy_invalid")
    if req.transport not in TRANSPORTS:
        raise EmbeddingAdminError("transport_invalid")
    if req.model_revision is not None and (
        not isinstance(req.model_revision, str) or not _REVISION.fullmatch(req.model_revision)
    ):
        raise EmbeddingAdminError("model_revision_invalid")
    if req.external_transfer_approved and req.transport != "external":
        # 스키마 CHECK 와 같다. DB 에서 터지면 사용자는 503 만 본다.
        raise EmbeddingAdminError("external_approval_requires_external_transport")


def _model_row(cur, revision: str | None) -> dict | None:
    if not revision:
        return None
    cur.execute(
        """SELECT revision, provider, state, external_approved, input_price_per_mtok
             FROM search_embedding_model_revision WHERE revision=%s""",
        (revision,),
    )
    return cur.fetchone()


def _blockers(req: PolicyChange, model: dict | None) -> tuple[str, ...]:
    blockers: list[str] = []
    if req.model_revision and model is None:
        blockers.append("model_unknown")
    if model is not None and PROVIDER_TRANSPORTS.get(str(model["provider"])) == EXTERNAL \
            and req.transport != "external":
        # 끌 때도 막는다 — 잘못 적힌 행은 나중에 켜는 순간 승인 없이 나간다.
        blockers.append("provider_requires_external_transport")
    if not req.enabled:
        return tuple(blockers)
    if not req.model_revision:
        blockers.append("model_required")
    elif model is not None and model["state"] != "active":
        blockers.append("model_not_active")
    if not (req.allow_channel or req.allow_attachment):
        blockers.append("nothing_allowed")
    if req.transport == "external":
        if model is not None and model["external_approved"] is not True:
            blockers.append("model_external_not_approved")
        if not req.external_transfer_approved:
            blockers.append("workspace_external_not_approved")
    return tuple(blockers)


def estimate(cur, workspace: str, price_per_mtok: float | None) -> dict:
    """켜면 처음에 얼마나 보내게 되는지. `raw_line` 의 개수와 글자 수만 센다.

    첨부 변환본과 채널 원문 중 아직 색인 캐시에 없는 것은 빠지므로 **하한에 가까운
    추정**이다. 비용은 글자 수를 토큰 수로 본 값이다. 단가가 없으면 null 이다.
    """
    cur.execute(
        """SELECT count(DISTINCT doc_path) AS docs, COALESCE(sum(length(body)), 0) AS chars
             FROM raw_line WHERE workspace=%s""",
        (workspace,),
    )
    row = cur.fetchone() or {"docs": 0, "chars": 0}
    chars = int(row["chars"] or 0)
    return {
        "documents": int(row["docs"] or 0),
        "chars": chars,
        "priceUsdPerMtok": price_per_mtok,
        "estimatedCostUsd": (
            None if price_per_mtok is None else round(chars / 1_000_000 * price_per_mtok, 2)
        ),
        "basis": "raw_line",
    }


def _current_policy(cur, workspace: str) -> dict | None:
    cur.execute(
        """SELECT workspace, enabled, model_revision, transport, allow_channel,
                  allow_attachment, allow_dm, external_transfer_approved, revision
             FROM search_embedding_workspace_policy WHERE workspace=%s""",
        (workspace,),
    )
    return cur.fetchone()


def preview_policy(req: PolicyChange, *, connect=None) -> dict:
    """저장하면 어떻게 되는지와 분량 추정. **아무것도 쓰지 않는다.**"""
    _validate_policy(req)
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            model = _model_row(cur, req.model_revision)
            current = _current_policy(cur, req.workspace)
            blockers = _blockers(req, model)
            price = None if model is None or model["input_price_per_mtok"] is None \
                else float(model["input_price_per_mtok"])
            sizing = estimate(cur, req.workspace, price) if req.enabled else None
    except EmbeddingAdminError:
        raise
    except Exception:  # noqa: BLE001
        raise EmbeddingAdminError("policy_unavailable") from None
    return {
        "workspace": req.workspace,
        "currentRevision": None if current is None else int(current["revision"]),
        "savable": not blockers,
        "blockers": list(blockers),
        "estimate": sizing,
    }


def save_policy(req: PolicyChange, *, connect=None) -> Saved:
    """검사한 뒤 정책 행 하나를 고쳐 쓴다. revision 이 다르면 거절한다."""
    _validate_policy(req)
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            model = _model_row(cur, req.model_revision)
            blockers = _blockers(req, model)
            if blockers:
                raise EmbeddingAdminError("activation_blocked", blockers=blockers)
            values = (req.enabled, req.model_revision, req.transport, req.allow_channel,
                      req.allow_attachment, req.external_transfer_approved,
                      req.reason, req.actor)
            if req.expected_revision is None:
                cur.execute(
                    """INSERT INTO search_embedding_workspace_policy
                           (workspace, enabled, model_revision, transport, allow_channel,
                            allow_attachment, allow_dm, external_transfer_approved,
                            reason, updated_by, revision, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, false, %s, %s, %s, 1, now())
                       ON CONFLICT (workspace) DO NOTHING
                       RETURNING revision""",
                    (req.workspace, *values),
                )
            else:
                # allow_dm 은 언제나 false 로 되돌린다. DM 색인은 kind 분리 스키마
                # 전까지 거절이고(정본 창구도 거절한다), 화면에 그 칸은 없다.
                cur.execute(
                    """UPDATE search_embedding_workspace_policy
                          SET enabled=%s, model_revision=%s, transport=%s,
                              allow_channel=%s, allow_attachment=%s, allow_dm=false,
                              external_transfer_approved=%s, reason=%s, updated_by=%s,
                              revision=revision + 1, updated_at=now()
                        WHERE workspace=%s AND revision=%s
                    RETURNING revision""",
                    (*values, req.workspace, req.expected_revision),
                )
            row = cur.fetchone()
            if row is None:
                # 다른 사람이 먼저 저장했거나, 없다고 본 행이 그새 생겼다.
                raise EmbeddingAdminError("policy_conflict")
            revision = int(row["revision"])
    except EmbeddingAdminError:
        raise
    except Exception:  # noqa: BLE001
        raise EmbeddingAdminError("policy_unavailable") from None
    return Saved(
        view={"workspace": req.workspace, "revision": revision, "enabled": req.enabled},
        audit=_audit(req.actor, "embedding_policy_save", "embedding_policy", req.workspace,
                     workspace=req.workspace, metadata={
                         "reason": req.reason,
                         "previous_revision": req.expected_revision,
                         "revision": revision,
                         "enabled": req.enabled,
                         "model_revision": req.model_revision,
                         "transport": req.transport,
                         "external_transfer_approved": req.external_transfer_approved,
                     }),
    )


# --- 현황 --------------------------------------------------------------------


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def status(*, workspaces: frozenset[str] | None, connect=None) -> dict:
    """화면 한 장에 필요한 것. `workspaces` 가 None 이면 전부 본다.

    본문·벡터·질문은 없다. 문서 수·청크 수·실행 이력의 숫자뿐이다.
    """
    try:
        with (connect or _connect)() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT revision, provider, model, dimensions, normalized, state,
                          external_approved, input_price_per_mtok, created_at, created_by
                     FROM search_embedding_model_revision ORDER BY created_at DESC"""
            )
            models = [
                {
                    "revision": r["revision"], "provider": r["provider"], "model": r["model"],
                    "dimensions": int(r["dimensions"]), "normalized": bool(r["normalized"]),
                    "state": r["state"], "externalApproved": bool(r["external_approved"]),
                    "priceUsdPerMtok": None if r["input_price_per_mtok"] is None
                    else float(r["input_price_per_mtok"]),
                    "createdAt": _iso(r["created_at"]), "createdBy": r["created_by"],
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                """SELECT workspace, enabled, model_revision, transport, allow_channel,
                          allow_attachment, external_transfer_approved, revision, reason,
                          updated_at, updated_by
                     FROM search_embedding_workspace_policy ORDER BY workspace"""
            )
            policies = [
                {
                    "workspace": r["workspace"], "enabled": bool(r["enabled"]),
                    "modelRevision": r["model_revision"], "transport": r["transport"],
                    "allowChannel": bool(r["allow_channel"]),
                    "allowAttachment": bool(r["allow_attachment"]),
                    "externalTransferApproved": bool(r["external_transfer_approved"]),
                    "revision": int(r["revision"]), "reason": r["reason"],
                    "updatedAt": _iso(r["updated_at"]), "updatedBy": r["updated_by"],
                }
                for r in cur.fetchall()
                if workspaces is None or r["workspace"] in workspaces
            ]
            cur.execute(
                """SELECT workspace, model_revision,
                          count(*) FILTER (WHERE is_current) AS current_docs,
                          COALESCE(sum(chunk_count) FILTER (WHERE is_current), 0) AS chunks,
                          count(*) FILTER (WHERE state = 'failed') AS failed_docs,
                          max(indexed_at) AS last_indexed
                     FROM archive_search_document_revision
                 GROUP BY workspace, model_revision ORDER BY 1, 2"""
            )
            coverage = [
                {
                    "workspace": r["workspace"], "modelRevision": r["model_revision"],
                    "currentDocuments": int(r["current_docs"]), "chunks": int(r["chunks"]),
                    "failedDocuments": int(r["failed_docs"]),
                    "lastIndexedAt": _iso(r["last_indexed"]),
                }
                for r in cur.fetchall()
                if workspaces is None or r["workspace"] in workspaces
            ]
            runs = _runs(cur) if workspaces is None else []
    except EmbeddingAdminError:
        raise
    except Exception:  # noqa: BLE001
        raise EmbeddingAdminError("status_unavailable") from None
    return {
        "providers": list(SUPPORTED_PROVIDERS),
        "models": models,
        "policies": policies,
        "coverage": coverage,
        # 실행 이력은 워크스페이스를 가리지 않는 전역 숫자다(회차가 여러 곳을 함께
        # 돈다). 일부 워크스페이스만 보는 사람에게 남의 분량이 보이지 않게 뺀다.
        "runs": runs,
    }


def _runs(cur, limit: int = 20) -> list[dict]:
    try:
        cur.execute(
            """SELECT id, model_revision, trigger, requested_by, state, error_code,
                      stopped_reason, requested_count, published_count, embed_calls,
                      embedded_chars, embedded_chunks, cost_usd, started_at, finished_at
                 FROM search_embedding_index_run ORDER BY started_at DESC LIMIT %s""",
            (limit,),
        )
    except Exception:  # noqa: BLE001 - 이력 표가 아직 없는 서버
        cur.connection.rollback()
        return []
    return [
        {
            "id": int(r["id"]), "modelRevision": r["model_revision"], "trigger": r["trigger"],
            "requestedBy": r["requested_by"], "state": r["state"],
            "errorCode": r["error_code"], "stoppedReason": r["stopped_reason"],
            "requested": r["requested_count"], "published": r["published_count"],
            "embedCalls": r["embed_calls"], "embeddedChars": r["embedded_chars"],
            "embeddedChunks": r["embedded_chunks"],
            "costUsd": None if r["cost_usd"] is None else float(r["cost_usd"]),
            "startedAt": _iso(r["started_at"]), "finishedAt": _iso(r["finished_at"]),
        }
        for r in cur.fetchall()
    ]


__all__ = [
    "MAX_REASON_CHARS",
    "SUPPORTED_PROVIDERS",
    "EmbeddingAdminError",
    "ModelChange",
    "ModelRegistration",
    "PolicyChange",
    "Saved",
    "change_model",
    "estimate",
    "preview_policy",
    "register_model",
    "save_policy",
    "status",
]
