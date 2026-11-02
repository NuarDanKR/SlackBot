"""B-81 이미지 설명 **정책 변경** 콘솔 라우트.

읽기 전용 상태 라우트(`GET …/policy`)와 분리한다. 그쪽은 보기만 하고 이쪽은
행을 고쳐 쓴다. 같은 파일에 두면 쓰기 보호(관리자·CSRF)를 한 군데 빠뜨렸을 때
읽기 경로를 보며 「검사한다」 고 믿게 된다.

## `app` 을 import 하지 않는다

보안 의존성(`current_user`·`_require_admin`·`_check_write_request`·`_audit_event`)은
`app.py` 에 있고, `app.py` 는 이 라우터를 등록한다. 모듈 수준에서 서로를 부르면
**순환 import** 다 — 실제로 그렇게 터진 적이 있다(2026-10-28, `router` 속성이
아직 없다는 `AttributeError`). 그래서 `build_router` 가 의존성을 받아서 그때
라우트를 만든다.

## 저장은 허가가 아니다

이 라우트는 정책 행 하나를 고쳐 쓰고 끝난다. worker 를 깨우지 않고 모델을
부르지 않으며 이미 쌓인 파일을 소급해 돌리지 않는다. 호출 권한은 전송 직전에
다시 판정된다. 화면에서 저장 성공을 **적용·호출 성공으로 표시하지 않는다.**
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import image_description_policy_admin as admin
from .auth import ConsoleUser

#: 다른 사람이 먼저 저장했다. 다시 읽어야 한다.
CONFLICT_CODES = frozenset({"policy_conflict"})
#: 고칠 자리가 설정·DB 쪽이다. 요청을 고쳐도 안 된다.
UNAVAILABLE_CODES = frozenset({
    "policy_unavailable",
    "connection_not_idle_autocommit",
})
#: 전용 Vision 자격 문제. `vision_database_` 로 시작하는 코드 전부.
UNAVAILABLE_PREFIX = "vision_database_"

#: 설정 칸 수 상한. 경계가 모르는 키를 거절하므로 통과하는 것은 많아야 열 개지만,
#: 거대한 본문을 파싱한 뒤에 거절하면 그 파싱이 공짜가 아니다.
MAX_CONFIG_KEYS = 50


class PolicyBody(BaseModel):
    """`extra="forbid"` 다. 모르는 칸을 조용히 버리면 운영자는 저장됐다고 믿는다.

    `actor` 를 여기서 받지 않는 이유도 같다 — 본문에 적어 보내면 **그 칸이
    무시된다는 사실을 아무도 모른 채** 다른 사람 이름으로 감사가 남는 줄 안다.
    실행자는 로그인한 사용자로만 정한다. `actor` 를 넣으면 422 다.
    """

    model_config = ConfigDict(extra="forbid")

    #: 빈 문자열이 전역 행이다. `min_length` 를 두면 전역을 못 고친다.
    workspace: str = Field(default="", max_length=40)
    expectedRevision: str | None = Field(default=None, max_length=200)
    enabled: bool
    config: dict = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=admin.MAX_REASON_CHARS)


def _request(body: PolicyBody, *, workspace: str, actor: str):
    if len(body.config) > MAX_CONFIG_KEYS:
        raise HTTPException(status_code=422, detail=_detail("config_key_unknown"))
    return admin.PolicyChangeRequest(
        workspace=workspace,
        expected_revision=body.expectedRevision,
        enabled=body.enabled,
        config=dict(body.config),
        reason=body.reason.strip(),
        actor=actor,
    )


def _detail(code: str, blockers: tuple[str, ...] = ()) -> dict:
    """언제나 같은 모양으로 내보낸다. 화면이 차단 사유를 목록으로 그린다."""
    return {"code": code, "blockers": list(blockers)}


def _workspace(user: ConsoleUser, value: str) -> str:
    """서버가 범위를 정한다. 본문이 말하는 워크스페이스를 믿지 않는다."""
    workspace = (value or "").strip().lower()
    if workspace == admin.GLOBAL:
        # 전역 기본값은 **모든 워크스페이스**에 걸린다. 일부만 보는 사람이
        # 고치면 자기가 못 보는 곳의 설정을 바꾸는 것이 된다(원칙 3).
        if not user.all_workspaces:
            raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))
        return admin.GLOBAL
    if not user.may_see(workspace):
        raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))
    return workspace


def _refused(exc: admin.ImageDescriptionPolicyAdminError) -> HTTPException:
    code = exc.code
    if code in CONFLICT_CODES:
        status = 409
    elif code in UNAVAILABLE_CODES or code.startswith(UNAVAILABLE_PREFIX):
        status = 503
    else:
        # 잘못된 요청·사유 누락·활성화 차단. 요청을 고치면 된다.
        status = 422
    return HTTPException(status_code=status, detail=_detail(code, exc.blockers))


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    """보안 의존성이 선 뒤에 라우트를 만든다 — 순환 import 를 피한다."""
    router = APIRouter()
    user_dependency = Depends(current_user)
    clock = now or (lambda: datetime.now(UTC))

    @router.post("/api/archiver/image-description/policy/preview")
    def post_policy_preview(
        body: PolicyBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """저장하면 어떻게 되는지만 본다. **아무것도 쓰지 않는다.**

        화면이 저장 단추를 막고 이유를 보여 주려면 저장을 시도해 보기 전에
        알아야 한다. 저장해 보고 실패로 배우게 하면 그 실패가 감사에 남는다.
        """
        require_admin(user)
        # 미리보기도 쓰기와 같은 CSRF 검사를 지난다. 쓰지는 않지만 **설정과
        # 차단 사유를 읽는** 경로이고, 그것은 로그인 세션으로만 열려야 한다.
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            said = admin.preview_policy_change(
                _request(body, workspace=workspace, actor=user.email), now=clock()
            )
        except admin.ImageDescriptionPolicyAdminError as exc:
            raise _refused(exc) from None
        return {
            "workspace": said.workspace,
            "enabled": said.enabled,
            "rowRevision": said.row_revision,
            "changedKeys": list(said.changed_keys),
            "savable": said.savable,
            "blockers": list(said.blockers),
        }

    @router.put("/api/archiver/image-description/policy")
    def put_policy(
        body: PolicyBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """정책 행 하나를 고쳐 쓴다. worker 도 모델도 부르지 않는다."""
        require_admin(user)
        check_write_request(request)
        workspace = _workspace(user, body.workspace)
        try:
            said = admin.save_policy_change(
                _request(body, workspace=workspace, actor=user.email), now=clock()
            )
        except admin.ImageDescriptionPolicyAdminError as exc:
            raise _refused(exc) from None

        # 저장은 끝났다. 감사 실패로 500 을 내면 운영자가 다시 누르고, 같은
        # 변경이 revision 두 개로 남는다. 실패는 숨기지 말고 **응답에 적는다.**
        audited = True
        try:
            audit_event(**said.audit)
        except Exception:  # noqa: BLE001 - 감사 실패가 저장을 되돌리지는 못한다
            audited = False
        return {
            "workspace": said.workspace,
            "revision": said.revision,
            "previousRevision": said.previous_revision,
            "enabled": said.enabled,
            "changedKeys": list(said.changed_keys),
            "audited": audited,
        }

    return router


__all__ = [
    "CONFLICT_CODES",
    "MAX_CONFIG_KEYS",
    "UNAVAILABLE_CODES",
    "UNAVAILABLE_PREFIX",
    "PolicyBody",
    "build_router",
]
