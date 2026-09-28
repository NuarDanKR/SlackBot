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

from . import license_store
from .app import _audit_event, _check_write_request, _require_admin, current_user
from .auth import ConsoleUser

logger = logging.getLogger("tybot.console.api")
router = APIRouter()
User = Annotated[ConsoleUser, Depends(current_user)]


class LicenseRowBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=24)
    allocated: int = Field(ge=0, le=license_store.MAX_ALLOCATED)


class LicensesBody(BaseModel):
    rows: list[LicenseRowBody] = Field(default_factory=list, max_length=500)


def _report(*, refresh: bool = False) -> dict:
    linked = license_store.linked_workspaces()
    try:
        stored = license_store.list_stored()
    except license_store.LicenseStoreError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    tokens = {key: token for key, (_label, token) in linked.items() if token}
    counts = license_store.active_counts(tokens, refresh=refresh)
    return license_store.build_report(linked, stored, counts)


@router.get("/api/licenses")
def get_licenses(user: User, refresh: bool = False) -> dict:
    """라이선스 표. 관리자만 본다 — 조직별 인원이 드러난다."""
    _require_admin(user)
    return _report(refresh=refresh)


@router.put("/api/licenses")
def put_licenses(body: LicensesBody, request: Request, user: User) -> dict:
    _require_admin(user)
    _check_write_request(request)
    linked = set(license_store.linked_workspaces())
    try:
        result = license_store.save(
            actor=user.email,
            rows=[row.model_dump() for row in body.rows],
            linked=linked,
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
