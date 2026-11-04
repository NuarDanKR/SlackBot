"""콘솔 API — 라이선스 현황 (`GET`·`PUT /api/licenses`).

표에는 Slack 연동 워크스페이스만 나온다. 활성은 Slack 이 정하고, 할당은 Slack 에서
가져올 수 없어 사람이 적는다. 계산·저장은 `license_store` 가 한다.

## 왜 `app.py` 가 아니라 여기 있나
라이선스 현황을 붙이면서 **기존 파일은 가능한 한 건드리지 않기로 했다.** 그래서 라우트를
이 모듈에 두고 `app.py` 에는 등록 두 줄만 남긴다. 로그인·권한·CSRF·감사 기록은
`app.py` 의 것을 그대로 빌려 쓴다 — 여기서 새로 만들면 규칙이 두 벌이 되고, 한쪽만
고치는 날 이 화면만 느슨해진다.

`app.py` 가 파일 **끝에서** 이 모듈을 불러오므로, 아래 `from .app import …` 시점에는
필요한 함수가 이미 정의돼 있다.

## 이 기능 때문에 바뀐 기존 파일
| 파일 | 무엇을 | 왜 피할 수 없나 |
|---|---|---|
| `src/tybot/console/app.py` | `include_router` 두 줄 | 콘솔 API 는 이 `app` 하나로만 뜬다. 정적 화면 마운트보다 앞에 등록해야 한다 |
| `console-web/src/App.tsx` | import·메뉴·화면 연결 각 1줄 | 메뉴 목록과 경로→화면 연결이 이 파일에만 있다 |
| `deploy/apply-schema.sh` | 스키마 목록 1줄 | 목록에 없으면 서버에 표가 생기지 않는다(`test_deploy_scripts` 가 막는다) |
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import license_people, license_store
from .app import _audit_event, _check_write_request, _require_admin, current_user
from .auth import ConsoleUser

logger = logging.getLogger("tybot.console.api")
router = APIRouter()
User = Annotated[ConsoleUser, Depends(current_user)]


class LicenseRowBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=24)
    allocated: int = Field(ge=0, le=license_store.MAX_ALLOCATED)


class ManualRowBody(BaseModel):
    """직접 추가한 워크스페이스. `id` 가 없으면 새로 추가한다."""

    id: int | None = Field(default=None, gt=0, strict=True)
    label: str = Field(min_length=1, max_length=license_store.MAX_LABEL)
    allocated: int = Field(ge=0, le=license_store.MAX_ALLOCATED)
    active: int = Field(ge=0, le=license_store.MAX_ALLOCATED)


class LicensesBody(BaseModel):
    rows: list[LicenseRowBody] = Field(default_factory=list, max_length=500)
    manual: list[ManualRowBody] = Field(default_factory=list, max_length=500)
    # 직접 추가한 워크스페이스만 지울 수 있다. 연동 워크스페이스는 받을 칸이 없다.
    removed: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        default_factory=list, max_length=500
    )


class MembersAddBody(BaseModel):
    """`target` 은 `ws:<워크스페이스 키>`(봇 토큰 없는 연동) 또는 `id:<번호>`(직접 추가)."""

    target: str = Field(min_length=4, max_length=40)
    empNos: list[Annotated[str, Field(max_length=40)]] = Field(
        min_length=1, max_length=license_people.MAX_ADD)


class ReleaseBody(BaseModel):
    """회수 처리. Slack 명단은 `slackUser`, 직접 입력 명단은 `memberId` 로 사람을 고른다."""

    target: str = Field(min_length=4, max_length=40)
    slackUser: str | None = Field(default=None, max_length=40)
    memberId: int | None = Field(default=None, gt=0, strict=True)
    reason: str = Field(min_length=1, max_length=500)


def _report(*, refresh: bool = False) -> dict:
    linked = license_store.linked_workspaces()
    try:
        stored = license_store.list_stored()
        manual = license_store.list_manual()
    except license_store.LicenseStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    tokens = {key: token for key, (_label, token) in linked.items() if token}
    counts = license_store.active_counts(tokens, refresh=refresh)
    return license_store.build_report(linked, stored, counts, manual,
                                      license_people.listed_counts())


@router.get("/api/licenses")
def get_licenses(user: User, refresh: bool = False) -> dict:
    """라이선스 표. 관리자만 본다 — 조직별 인원이 드러난다."""
    _require_admin(user)
    return _report(refresh=refresh)


@router.put("/api/licenses")
def put_licenses(body: LicensesBody, request: Request, user: User) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        result = license_store.save(
            actor=user.email,
            rows=[row.model_dump() for row in body.rows],
            linked=license_store.linked_workspaces(),
            manual=[row.model_dump() for row in body.manual],
            removed=body.removed,
        )
    except license_store.LicenseStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.warning("라이선스 설정 변경 — actor=%s result=%s", user.email, result)
    _audit_event(
        actor=user.email,
        category="license",
        action="save",
        target_type="settings",
        target_id="slack_license",
        outcome="succeeded",
        metadata=result,
    )
    return {**_report(), "result": result}


@router.get("/api/licenses/people")
def get_license_people(user: User, refresh: bool = False) -> dict:
    """워크스페이스별 라이선스 보유자와 회수 기록. 이름·사번·조직이 나오므로 관리자만 본다.

    Slack 조회는 표(`GET /api/licenses`)와 같은 캐시를 쓴다 — 화면을 열 때 두 번 읽지 않는다.
    """
    _require_admin(user)
    return _people(refresh)


@router.post("/api/licenses/members")
def post_license_members(body: MembersAddBody, request: Request, user: User) -> dict:
    """봇 토큰이 없거나 직접 추가한 워크스페이스의 명단에 사번을 넣는다."""
    _require_admin(user)
    _check_write_request(request)
    try:
        result = license_people.add(body.target, body.empNos, actor=user.email,
                                    linked=license_store.linked_workspaces())
    except license_people.LicensePeopleError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _audit_event(actor=user.email, category="license", action="member-add",
                 target_type="license_member", target_id=body.target, outcome="succeeded",
                 metadata={"added": result["added"], "already": len(result["already"]),
                           "notFound": len(result["notFound"]), "inactive": result["inactive"]})
    return result


def _people(refresh: bool = False) -> dict:
    linked = license_store.linked_workspaces()
    try:
        manual = license_store.list_manual()
    except license_store.LicenseStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    tokens = {key: token for key, (_label, token) in linked.items() if token}
    counts = license_store.active_counts(tokens, refresh=refresh)
    try:
        return license_people.holders(linked, counts, manual)
    except license_people.LicensePeopleError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/api/licenses/release")
def post_license_release(body: ReleaseBody, request: Request, user: User) -> dict:
    """라이선스 회수 처리 — 사유와 함께 기록한다.

    사람은 **서버가 지금 읽은 명단에서** 찾는다. 화면이 보낸 이름·사번을 그대로 적으면
    기록이 실제 명단과 어긋날 수 있다.
    """
    _require_admin(user)
    _check_write_request(request)
    if (body.slackUser is None) == (body.memberId is None):
        raise HTTPException(status_code=422, detail="회수할 사람을 하나 골라 주세요.")
    holder = next((h for h in _people()["holders"] if h["uid"] == body.target and (
        (body.memberId is not None and h["memberId"] == body.memberId)
        or (body.slackUser is not None and h["slackUser"] == body.slackUser))), None)
    if holder is None:
        raise HTTPException(status_code=404, detail="명단에서 찾지 못했습니다. 화면을 새로 고쳐 주세요.")
    try:
        result = license_people.release(holder, reason=body.reason, actor=user.email)
    except license_people.LicensePeopleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _audit_event(actor=user.email, category="license", action="release",
                 target_type="license_release", target_id=str(result["id"]), outcome="succeeded",
                 metadata={"uid": body.target, "source": holder["source"], "emp_no": holder["empNo"],
                           "slack_user": holder["slackUser"]})
    return result


@router.post("/api/licenses/release/{release_id}/cancel")
def post_license_release_cancel(release_id: int, request: Request, user: User) -> dict:
    _require_admin(user)
    _check_write_request(request)
    try:
        result = license_people.cancel(release_id, actor=user.email)
    except license_people.LicensePeopleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _audit_event(actor=user.email, category="license", action="release-cancel",
                 target_type="license_release", target_id=str(result["id"]), outcome="succeeded",
                 metadata=result)
    return result
