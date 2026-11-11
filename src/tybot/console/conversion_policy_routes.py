"""변환 런타임 정책(rhwp 앱 ID·야간 재점검 숫자) 콘솔 라우트 (B-89 후속).

정본은 DB 한 행이다(`tybot.conversion_policy`). 이 화면은 그 행을 보여 주고, 사람이
저장하면 한 트랜잭션에서 행을 바꾸고 감사 행을 붙인다.

## 시크릿은 없다

다루는 값은 Slack 앱 ID(공개 식별자)와 숫자 둘뿐이다. 토큰·키는 이 경로로 오가지
않는다 — 본문에 모르는 칸이 오면 422 다(`extra="forbid"`).

## 저장은 「켜기」 가 아니다

값을 저장해도 야간 타이머는 켜지지 않는다. 타이머는 readiness 점검
(`scripts/check_conversion_policy.py`)을 본 사람이 따로 켠다. 응답과 화면에도
그렇게 적는다.

## `app` 을 import 하지 않는다

보안 의존성은 `app.py` 에 있고 `app.py` 가 이 라우터를 등록한다. 순환 import 를
피하려고 `build_router` 가 의존성을 받는다(`image_description_policy_routes` 와 같다).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import conversion_policy as policy_store
from .auth import ConsoleUser

logger = logging.getLogger(__name__)


class ConversionPolicyBody(BaseModel):
    """실행자는 로그인한 사용자로만 정한다. 본문에 `actor` 를 넣으면 422 다."""

    model_config = ConfigDict(extra="forbid")

    rhwpAppIds: list[str] = Field(max_length=policy_store.MAX_APP_IDS)
    recheckLimit: int
    rhwpGraceHours: int
    #: 화면이 읽었던 판. 행이 없었으면 0.
    expectedRevision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


def snapshot(*, resolve=None, history=None, legacy=None) -> dict:
    """화면 한 장에 필요한 것. DB 를 못 읽어도 화면은 뜬다 — 「모름」 을 보여 준다."""
    resolve = resolve or policy_store.resolve
    history = history or policy_store.history
    legacy = legacy or policy_store.legacy_values
    policy = resolve()
    try:
        entries = history(20)
        history_error = ""
    except Exception as exc:  # noqa: BLE001 - 이력 조회 실패가 화면을 막으면 안 된다
        entries, history_error = [], type(exc).__name__
    return {
        "policy": policy.to_json(include_actor=True),
        "legacy": legacy(),
        "history": entries,
        "historyError": history_error,
        "limits": {
            "recheckLimit": list(policy_store.RECHECK_LIMIT_RANGE),
            "rhwpGraceHours": list(policy_store.RHWP_GRACE_RANGE),
            "maxAppIds": policy_store.MAX_APP_IDS,
        },
        "defaults": {
            "recheckLimit": policy_store.DEFAULT_RECHECK_LIMIT,
            "rhwpGraceHours": policy_store.DEFAULT_RHWP_GRACE_HOURS,
        },
        "timerNote": (
            "값을 저장해도 새벽 재점검 타이머는 켜지지 않습니다. "
            "readiness 점검(scripts/check_conversion_policy.py)을 확인한 뒤 사람이 켭니다."
        ),
    }


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    save=None,
    resolve=None,
    history=None,
    legacy=None,
) -> APIRouter:
    router = APIRouter()
    user_dependency = Depends(current_user)
    do_save = save or policy_store.save

    def _view() -> dict:
        return snapshot(resolve=resolve, history=history, legacy=legacy)

    @router.get("/api/conversion-policy")
    def get_conversion_policy(user: ConsoleUser = user_dependency) -> dict:
        require_admin(user)
        return _view()

    @router.put("/api/conversion-policy")
    def put_conversion_policy(
        body: ConversionPolicyBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        require_admin(user)
        check_write_request(request)
        # 이 값은 **모든 워크스페이스**의 HWP 처리를 바꾼다. 일부만 보는 관리자가
        # 바꾸면 자기가 못 보는 곳의 수집을 바꾸는 것이 된다(원칙 3).
        if not user.all_workspaces:
            raise HTTPException(
                status_code=403, detail="전체 워크스페이스 권한이 있는 관리자만 바꿀 수 있습니다."
            )
        try:
            saved = do_save(
                app_ids=body.rhwpAppIds,
                recheck_limit=body.recheckLimit,
                rhwp_grace_hours=body.rhwpGraceHours,
                actor=user.email,
                expected_revision=body.expectedRevision,
                reason=body.reason,
            )
        except policy_store.PolicyInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except policy_store.PolicyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except policy_store.PolicyUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail=f"변환 정책 DB 를 쓸 수 없습니다({exc.reason}). 스키마 적용과 DB 상태를 확인하세요.",
            ) from None
        logger.warning(
            "변환 정책 변경 — actor=%s revision=%s delegation=%s",
            user.email,
            saved.revision,
            saved.delegation,
        )
        # 저장은 끝났다. 감사 실패로 500 을 내면 다시 누르게 되고 같은 변경이 두 판이
        # 된다. DB 감사 행은 이미 같은 트랜잭션에 있다 — 통합 감사 실패는 응답에 적는다.
        audited = True
        try:
            audit_event(
                actor=user.email,
                category="environment",
                action="policy_change",
                target_type="conversion_policy",
                target_id=str(saved.revision),
                outcome="succeeded",
                metadata={
                    "revision": saved.revision,
                    "delegation": saved.delegation,
                    "appIdCount": len(saved.app_ids),
                    "recheckLimit": saved.recheck_limit,
                    "rhwpGraceHours": saved.rhwp_grace_hours,
                },
            )
        except Exception:  # noqa: BLE001 - 감사 실패가 저장을 되돌리지는 못한다
            audited = False
        return {**_view(), "saved": saved.to_json(include_actor=True), "audited": audited}

    return router


__all__ = ["ConversionPolicyBody", "build_router", "snapshot"]
