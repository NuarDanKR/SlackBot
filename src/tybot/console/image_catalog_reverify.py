"""B-101 Vision 무료 카탈로그 **재검증** — 비용 가드가 끈 모델을 콘솔에서 다시 켠다.

## 왜 있나

Vision 호출 한 건이 「비용 확인 불가(`cost_unverified`)」 나 「과금(`unexpected_charge`)」
으로 끝나면 저장소(`ImageDescriptionStore.finish_and_publish`)가 그 모델의 카탈로그를
`zero_cost_verified=false` 로 끈다(`updated_by='runtime-cost-guard'`). 무료라서 켠 기능이
계속 과금되지 않게 하는 의도된 fail-closed 다. **사람이 다시 확인하기 전까지** 그 모델을
쓰는 모든 이미지가 `zero_cost_not_verified` 로 막힌다.

2026-11-24 Gemini 타임아웃 한 건으로 tyit Vision 이 사흘 꺼져 있었다. 다시 켤 자리가
서버 SQL 뿐이어서다 — 「운영 기능은 콘솔에 둔다」(CLAUDE.md).

## 무엇을 하고 무엇을 안 하나

- **조회(`inspect`)** — 다시 켜기 전에 볼 것: 지금 행, 누가 껐는지, 비용 가드가 막은
  최근 작업, 이 모델로 켜진 워크스페이스. 다시 켜면 막혀 있던 이미지가 다음 배치에서
  한꺼번에 나가므로 **범위를 먼저 보인다.**
- **재검증(`reverify`)** — 무료 확인·한도·검증 시각만 새로 적는다. Vision 지원·자격·
  민감도·지역은 건드리지 않는다. 그 칸이 막고 있으면 다시 켜도 안 돌므로 **거절한다**
  (켰다고 믿고 아무것도 안 도는 상태가 가장 나쁘다).
- 모델을 부르지 않고 worker 를 깨우지 않는다. 다음 15분 배치가 현재 사실로 다시 판정한다.
- 사유·과금 확인은 필수다. 실행자는 로그인 사용자로만 정한다.

카탈로그는 **전역**이다 — 한 모델 행이 모든 워크스페이스에 걸린다. 그래서 전체
워크스페이스 관리자만 고칠 수 있다(원칙 3).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..archive.image_description_store import ImageDescriptionDenied, ImageDescriptionStore
from ..archive.image_model_catalog import FreeQuotaState
from . import image_description_admin
from .auth import ConsoleUser

MAX_REASON_CHARS = 500
#: 비용 가드가 카탈로그를 끌 때 남기는 실행자(`image_description_store`).
GUARD_ACTOR = "runtime-cost-guard"
#: 다시 켤 때 고를 수 있는 한도 방식. `unknown`·`exhausted` 로는 켜지지 않는다.
QUOTA_STATES = frozenset({FreeQuotaState.UNLIMITED.value, FreeQuotaState.AVAILABLE.value})
MAX_QUOTA = 10_000_000
RECENT_COST_JOBS = 5

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CODE = re.compile(r"^[a-z0-9_]+$")

#: 다른 사람이 먼저 바꿨다 — 다시 읽어야 한다.
CONFLICT_CODES = frozenset({"catalog_conflict"})
NOT_FOUND_CODES = frozenset({"catalog_not_found"})
UNAVAILABLE_CODES = frozenset(
    {
        "catalog_unavailable",
        "policy_unavailable",
        "connection_not_idle_autocommit",
    }
)
UNAVAILABLE_PREFIX = "vision_database_"


class CatalogReverifyError(RuntimeError):
    """콘솔에 그대로 내보내도 되는 고정 코드 거절."""

    def __init__(self, code: str, *, blockers: tuple[str, ...] = ()) -> None:
        self.code = code
        self.blockers = blockers
        super().__init__(code)


@dataclass(frozen=True)
class ReverifyRequest:
    provider: str
    model: str
    expected_revision: str
    quota_state: str
    quota_remaining: int | None
    confirm_no_charge: bool
    reason: str
    actor: str


@dataclass(frozen=True)
class ReverifyOutcome:
    provider: str
    model: str
    revision: str
    previous_revision: str
    previous_updated_by: str
    quota_state: str
    audit: dict = field(default_factory=dict)


def _safe(value: object) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else "catalog_unavailable"


def _name(value: str) -> str:
    text = (value or "").strip()
    if not _NAME.fullmatch(text):
        raise CatalogReverifyError("invalid_request")
    return text


def _connector(connect):
    return connect or image_description_admin._connect


def _catalog_row(cur, provider: str, model: str) -> dict:
    cur.execute(
        """SELECT provider,model,supports_vision,zero_cost_verified,verified_at,
                  verification_revision,max_sensitivity,allowed_regions,credential_ready,
                  quota_state,quota_remaining,quota_resets_at,updated_by,updated_at
             FROM archiver_image_model_catalog WHERE provider=%s AND model=%s""",
        (provider, model),
    )
    row = cur.fetchone()
    if not row:
        raise CatalogReverifyError("catalog_not_found")
    return dict(row)


def _affected(rows: list[dict], provider: str, model: str) -> list[dict]:
    """이 모델로 **실제로 켜진** 워크스페이스. 워크스페이스 행이 전역 행을 덮는다."""
    by_ws = {str(r["workspace"]): r for r in rows}
    glob = by_ws.get("", {})
    gconf = glob.get("config") or {}
    out = []
    for workspace, row in sorted(by_ws.items()):
        if not workspace:
            continue
        conf = gconf | (row.get("config") or {})
        if conf.get("provider") != provider or conf.get("model") != model:
            continue
        out.append(
            {
                "workspace": workspace,
                "enabled": bool(glob.get("enabled")) and bool(row.get("enabled")),
            }
        )
    return out


def _wrap(fn):
    try:
        return fn()
    except CatalogReverifyError:
        raise
    except image_description_admin.ImageDescriptionAdminError as exc:
        # 자격 쪽 코드는 그대로 — DSN 없음과 DB 장애가 같은 글자가 되면 고칠 자리를 못 찾는다.
        raise CatalogReverifyError(_safe(str(exc))) from None
    except ImageDescriptionDenied as exc:
        raise CatalogReverifyError(_safe(exc.args[0] if exc.args else "")) from None
    except Exception:  # noqa: BLE001 - DB 예외 본문에는 행 값이 들어 있다
        raise CatalogReverifyError("catalog_unavailable") from None


def inspect(provider: str, model: str, *, connect=None) -> dict:
    """다시 켜기 전에 볼 것. **아무것도 쓰지 않는다.** 원본·설명 본문은 읽지 않는다."""
    provider, model = _name(provider), _name(model)

    def run() -> dict:
        with _connector(connect)() as conn, conn.cursor() as cur:
            row = _catalog_row(cur, provider, model)
            cur.execute(
                """SELECT created_at, workspace, error_code,
                          CASE WHEN actual_usd IS NULL THEN NULL
                               ELSE actual_usd::text END AS actual_usd
                     FROM archiver_image_description_job
                    WHERE catalog_snapshot->>'provider'=%s
                      AND catalog_snapshot->>'model'=%s
                      AND error_code ~ '(cost_unverified|unexpected_charge)'
                    ORDER BY created_at DESC LIMIT %s""",
                (provider, model, RECENT_COST_JOBS),
            )
            jobs = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT workspace, enabled, config FROM archiver_image_description_policy")
            policies = [dict(r) for r in cur.fetchall()]
        return {
            "provider": provider,
            "model": model,
            "zeroCostVerified": bool(row["zero_cost_verified"]),
            "quotaState": row["quota_state"],
            "quotaRemaining": row["quota_remaining"],
            "verificationRevision": row["verification_revision"],
            "verifiedAt": row["verified_at"],
            "updatedBy": row["updated_by"],
            "updatedAt": row["updated_at"],
            "disabledByGuard": row["updated_by"] == GUARD_ACTOR,
            "supportsVision": bool(row["supports_vision"]),
            "credentialReady": bool(row["credential_ready"]),
            "costJobs": [
                {
                    "createdAt": j["created_at"],
                    "workspace": j["workspace"],
                    "errorCode": ",".join(
                        c for c in str(j["error_code"] or "").split(",") if _CODE.fullmatch(c)
                    ),
                    "actualUsd": j["actual_usd"],
                }
                for j in jobs
            ],
            # 실제 과금 기록이 있으면 화면이 더 세게 경고한다.
            "chargeSeen": any(
                j["actual_usd"] is not None and float(j["actual_usd"]) > 0 for j in jobs
            ),
            "affectedWorkspaces": _affected(policies, provider, model),
        }

    return _wrap(run)


def _validate(request: ReverifyRequest) -> None:
    reason = request.reason.strip()
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise CatalogReverifyError("reason_required")
    if not request.actor:
        raise CatalogReverifyError("actor_required")
    if request.confirm_no_charge is not True:
        raise CatalogReverifyError("confirmation_required")
    if request.quota_state not in QUOTA_STATES:
        raise CatalogReverifyError("quota_state_invalid")
    remaining = request.quota_remaining
    if request.quota_state == FreeQuotaState.AVAILABLE.value:
        if type(remaining) is not int or not 1 <= remaining <= MAX_QUOTA:
            raise CatalogReverifyError("quota_remaining_invalid")
    elif remaining is not None:
        raise CatalogReverifyError("quota_remaining_invalid")


def reverify(request: ReverifyRequest, *, now: datetime, connect=None) -> ReverifyOutcome:
    """무료 확인·한도·검증 시각만 새로 적는다. 모델도 worker 도 부르지 않는다."""
    provider, model = _name(request.provider), _name(request.model)
    _validate(request)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    revision = f"console-reverified-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}"

    def run() -> tuple[dict, str]:
        with _connector(connect)() as conn:
            with conn.cursor() as cur:
                row = _catalog_row(cur, provider, model)
            if row["verification_revision"] != request.expected_revision:
                raise CatalogReverifyError("catalog_conflict")
            blockers = tuple(
                code
                for code, ok in (
                    ("vision_not_verified", row["supports_vision"]),
                    ("credential_not_ready", row["credential_ready"]),
                )
                if not ok
            )
            if blockers:
                raise CatalogReverifyError("reverify_blocked", blockers=blockers)
            entry = ImageDescriptionStore._catalog_entry(row)
            quota = FreeQuotaState(request.quota_state)
            fresh = replace(
                entry,
                zero_cost_verified=True,
                verified_at=now,
                verification_revision=revision,
                quota_state=quota,
                quota_remaining=(
                    request.quota_remaining if quota is FreeQuotaState.AVAILABLE else None
                ),
                quota_resets_at=(
                    entry.quota_resets_at if quota is FreeQuotaState.AVAILABLE else None
                ),
            )
            # 저장소가 전역 정책 행을 잠그고 revision 을 다시 비교한다 — 여기서 읽은 값이
            # 낡았어도 commit 되지 않는다.
            ImageDescriptionStore(conn).save_catalog(
                fresh, expected_revision=request.expected_revision, actor=request.actor
            )
        return row, revision

    row, revision = _wrap(run)
    previous_by = str(row["updated_by"] or "")
    return ReverifyOutcome(
        provider=provider,
        model=model,
        revision=revision,
        previous_revision=request.expected_revision,
        previous_updated_by=previous_by,
        quota_state=request.quota_state,
        audit={
            "actor": request.actor,
            "category": "archiver",
            "action": "image_model_catalog_reverify",
            "target_type": "image_model_catalog",
            "target_id": f"{provider}/{model}",
            "workspace": None,
            "outcome": "succeeded",
            "metadata": {
                "reason": request.reason.strip(),
                "previous_revision": request.expected_revision,
                "previous_updated_by": previous_by,
                "previous_zero_cost_verified": bool(row["zero_cost_verified"]),
                "previous_quota_state": str(row["quota_state"] or ""),
                "revision": revision,
                "quota_state": request.quota_state,
                "quota_remaining": request.quota_remaining,
                "confirm_no_charge": True,
            },
        },
    )


# --- 라우트 ------------------------------------------------------------------

PATH = "/api/archiver/image-description/catalog/reverify"


class ReverifyBody(BaseModel):
    """`extra="forbid"` — 모르는 칸을 조용히 버리면 저장됐다고 믿는다. `actor` 는 받지 않는다."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    expectedRevision: str = Field(max_length=200)
    quotaState: str = Field(min_length=1, max_length=20)
    quotaRemaining: int | None = None
    confirmNoCharge: bool
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


def _detail(code: str, blockers: tuple[str, ...] = ()) -> dict:
    return {"code": code, "blockers": list(blockers)}


def _refused(exc: CatalogReverifyError) -> HTTPException:
    code = exc.code
    if code in CONFLICT_CODES:
        status = 409
    elif code in NOT_FOUND_CODES:
        status = 404
    elif code in UNAVAILABLE_CODES or code.startswith(UNAVAILABLE_PREFIX):
        status = 503
    else:
        status = 422
    return HTTPException(status_code=status, detail=_detail(code, exc.blockers))


def _require_global(user: ConsoleUser) -> None:
    # 카탈로그는 모든 워크스페이스에 걸린다. 일부만 보는 사람이 켜면 자기가 못 보는 곳의
    # 호출을 여는 것이 된다.
    if not user.all_workspaces:
        raise HTTPException(status_code=403, detail=_detail("workspace_forbidden"))


def build_router(
    *,
    current_user: Callable,
    require_admin: Callable[[ConsoleUser], None],
    check_write_request: Callable[[Request], None],
    audit_event: Callable[..., None],
    now: Callable[[], datetime] | None = None,
    connect=None,
) -> APIRouter:
    """보안 의존성을 받아 라우트를 만든다 — `app` 과의 순환 import 를 피한다."""
    router = APIRouter()
    user_dependency = Depends(current_user)
    clock = now or (lambda: datetime.now(UTC))

    @router.get(PATH)
    def get_catalog_reverify(
        provider: str = Query(min_length=1, max_length=128),
        model: str = Query(min_length=1, max_length=128),
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """다시 켜기 전에 볼 것. 쓰지 않는다."""
        require_admin(user)
        _require_global(user)
        try:
            return inspect(provider, model, connect=connect)
        except CatalogReverifyError as exc:
            raise _refused(exc) from None

    @router.post(PATH)
    def post_catalog_reverify(
        body: ReverifyBody,
        request: Request,
        user: ConsoleUser = user_dependency,
    ) -> dict:
        """무료 확인을 다시 적는다. 모델·worker 는 부르지 않는다."""
        require_admin(user)
        check_write_request(request)
        _require_global(user)
        try:
            said = reverify(
                ReverifyRequest(
                    provider=body.provider,
                    model=body.model,
                    expected_revision=body.expectedRevision,
                    quota_state=body.quotaState,
                    quota_remaining=body.quotaRemaining,
                    confirm_no_charge=body.confirmNoCharge,
                    reason=body.reason,
                    actor=user.email,
                ),
                now=clock(),
                connect=connect,
            )
        except CatalogReverifyError as exc:
            raise _refused(exc) from None
        # 저장은 끝났다. 감사 실패로 500 을 내면 다시 누르고 같은 변경이 두 번 남는다.
        audited = True
        try:
            audit_event(**said.audit)
        except Exception:  # noqa: BLE001 - 감사 실패가 저장을 되돌리지는 못한다
            audited = False
        return {
            "provider": said.provider,
            "model": said.model,
            "revision": said.revision,
            "previousRevision": said.previous_revision,
            "previousUpdatedBy": said.previous_updated_by,
            "quotaState": said.quota_state,
            "audited": audited,
        }

    return router


__all__ = [
    "GUARD_ACTOR",
    "MAX_REASON_CHARS",
    "PATH",
    "QUOTA_STATES",
    "CatalogReverifyError",
    "ReverifyBody",
    "ReverifyOutcome",
    "ReverifyRequest",
    "build_router",
    "inspect",
    "reverify",
]
