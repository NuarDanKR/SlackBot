"""B-12 임베딩 색인 콘솔 라우트 — 현황, 모델 등록, 워크스페이스 승인.

`app` 을 import 하지 않는다. 보안 의존성은 `app.py` 에 있고 `app.py` 가 이 라우터를
등록하므로, 모듈 수준에서 서로 부르면 순환 import 다(이미지 설명 정책 라우트와 같은
이유). `build_router` 가 의존성을 받아 그때 라우트를 만든다.

## 누가 무엇을

| 동작 | 조건 |
|---|---|
| 현황 보기 | 관리자. 정책·색인 수는 볼 수 있는 워크스페이스만 |
| 모델 등록·상태·외부 승인 | 관리자 + 전체 워크스페이스. 모델은 모든 워크스페이스에 걸린다 |
| 워크스페이스 정책 | 관리자 + 그 워크스페이스를 볼 수 있음 |

실행은 여기 없다. 「지금 한 회차」 는 배치 관리의 `tybot-embedding-index.timer`
실행 단추가 한다 — 실행 경로를 둘로 만들면 하나는 단일 실행 판정을 빼먹는다.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import embedding_admin as admin
from .auth import ConsoleUser

CONFLICT_CODES = frozenset({"policy_conflict", "model_exists"})
UNAVAILABLE_CODES = frozenset({
    "database_unconfigured", "database_unavailable", "registry_unavailable",
    "policy_unavailable", "status_unavailable",
})
NOT_FOUND_CODES = frozenset({"model_unknown"})


def _detail(code: str, blockers: tuple[str, ...] = ()) -> dict:
    return {"code": code, "blockers": list(blockers)}


def _refused(exc: admin.EmbeddingAdminError) -> HTTPException:
    if exc.code in CONFLICT_CODES:
        status = 409
    elif exc.code in UNAVAILABLE_CODES:
        status = 503
    elif exc.code in NOT_FOUND_CODES:
        status = 404
    else:
        status = 422
    return HTTPException(status_code=status, detail=_detail(exc.code, exc.blockers))


class ModelBody(BaseModel):
    """`extra="forbid"` — 모르는 칸을 조용히 버리면 운영자는 저장됐다고 믿는다.

    `actor` 는 받지 않는다. 실행자는 로그인한 사용자로만 정한다.
    """

    model_config = ConfigDict(extra="forbid")

    revision: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=admin.MAX_MODEL_CHARS)
    dimensions: int
    normalized: bool = True
    priceUsdPerMtok: float | None = None
    reason: str = Field(min_length=1, max_length=admin.MAX_REASON_CHARS)


class ModelChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=20)
    externalApproved: bool
    reason: str = Field(min_length=1, max_length=admin.MAX_REASON_CHARS)


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=40)
    expectedRevision: int | None = None
    enabled: bool
    modelRevision: str | None = Field(default=None, max_length=128)
    transport: str = Field(min_length=1, max_length=20)
    allowChannel: bool = True
    allowAttachment: bool = True
    externalTransferApproved: bool = False
    reason: str = Field(min_length=1, max_length=admin.MAX_REASON_CHARS)


def _policy(body: PolicyBody, *, workspace: str, actor: str) -> admin.PolicyChange:
    return admin.PolicyChange(
        workspace=workspace,
        expected_revision=body.expectedRevision,
        enabled=body.enabled,
        model_revision=(body.modelRevision or "").strip() or None,
        transport=body.transport,
        allow_channel=body.allowChannel,
        allow_attachment=body.allowAttachment,
        external_transfer_approved=body.externalTransferApproved,
        reason=body.reason.strip(),
        actor=actor,
    )


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    connect: Callable | None = None,
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)

    def _all_workspaces(user: ConsoleUser) -> None:
        # 모델 레지스트리는 모든 워크스페이스에 걸린다. 일부만 보는 사람이 바꾸면
        # 자기가 못 보는 곳의 전송 대상을 바꾸게 된다(원칙 3).
        if not user.all_workspaces:
            raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))

    def _workspace(user: ConsoleUser, value: str) -> str:
        workspace = (value or "").strip().lower()
        if not user.may_see(workspace):
            raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))
        return workspace

    def _record(saved: admin.Saved) -> bool:
        # 저장은 끝났다. 감사 실패로 500 을 내면 운영자가 다시 누르고 같은 변경이
        # 두 번 남는다. 실패는 숨기지 말고 응답에 적는다.
        try:
            audit_event(**saved.audit)
        except Exception:  # noqa: BLE001
            return False
        return True

    @router.get("/api/embedding/status")
    def get_status(user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        scope = None if user.all_workspaces else frozenset(user.workspaces)
        try:
            return admin.status(workspaces=scope, connect=connect)
        except admin.EmbeddingAdminError as exc:
            raise _refused(exc) from None

    @router.post("/api/embedding/models")
    def post_model(body: ModelBody, request: Request,
                   user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        check_write_request(request)
        _all_workspaces(user)
        try:
            saved = admin.register_model(admin.ModelRegistration(
                revision=body.revision.strip(), provider=body.provider.strip(),
                model=body.model.strip(), dimensions=body.dimensions,
                normalized=body.normalized, price_per_mtok=body.priceUsdPerMtok,
                reason=body.reason.strip(), actor=user.email,
            ), connect=connect)
        except admin.EmbeddingAdminError as exc:
            raise _refused(exc) from None
        return {**saved.view, "audited": _record(saved)}

    @router.put("/api/embedding/models/{revision}")
    def put_model(revision: str, body: ModelChangeBody, request: Request,
                  user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        check_write_request(request)
        _all_workspaces(user)
        try:
            saved = admin.change_model(admin.ModelChange(
                revision=revision, state=body.state,
                external_approved=body.externalApproved,
                reason=body.reason.strip(), actor=user.email,
            ), connect=connect)
        except admin.EmbeddingAdminError as exc:
            raise _refused(exc) from None
        return {**saved.view, "audited": _record(saved)}

    @router.post("/api/embedding/policy/preview")
    def post_policy_preview(body: PolicyBody, request: Request,
                            user: ConsoleUser = user_dependency) -> dict:
        """저장하면 어떻게 되는지와 분량 추정. **아무것도 쓰지 않는다.**"""
        require_admin(user)
        # 쓰지는 않지만 정책과 분량을 읽는 경로다. 로그인 세션으로만 열린다.
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            return admin.preview_policy(
                _policy(body, workspace=workspace, actor=user.email), connect=connect
            )
        except admin.EmbeddingAdminError as exc:
            raise _refused(exc) from None

    @router.put("/api/embedding/policy")
    def put_policy(body: PolicyBody, request: Request,
                   user: ConsoleUser = user_dependency) -> dict:
        """정책 행 하나를 고쳐 쓴다. 색인을 시작하지 않는다."""
        require_admin(user)
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            saved = admin.save_policy(
                _policy(body, workspace=workspace, actor=user.email), connect=connect
            )
        except admin.EmbeddingAdminError as exc:
            raise _refused(exc) from None
        return {**saved.view, "audited": _record(saved)}

    return router


__all__ = ["CONFLICT_CODES", "UNAVAILABLE_CODES", "build_router"]
