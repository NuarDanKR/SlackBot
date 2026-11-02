"""B-81 이미지 설명 **정책 변경** 경계.

## 저장은 허가가 아니다

이 모듈이 하는 일은 정책 행을 하나 고쳐 쓰는 것뿐이다. 저장했다고 모델을 부르지
않고, 이미 쌓인 파일을 소급해 돌리지도 않는다. 호출 권한은 전송 직전에
`preflight_image_description` 과 `ImageDescriptionStore.reserve` 가 **현재 사실로
다시** 판정한다. 그래서 여기서 통과한 저장이 나중에 거절될 수 있고, 그게 맞다.

여기서 미리 막는 이유는 따로 있다. 화면에서 「켜짐」 으로 저장해 놓고 실제로는
아무것도 안 도는 상태가 가장 나쁘다 — 운영자는 켰다고 믿고, 증상은 몇 주 뒤
「왜 사진이 하나도 안 잡히나」 로 나타난다. 켤 수 없는 설정은 **켜는 순간**
거절한다.

## 전역과 워크스페이스

행이 둘이다. 전역 행(`workspace=''`)이 기본값이고 워크스페이스 행이 덮어쓴다.
덮어쓰기 규칙은 저장소(`_effective`)가 소유하므로 여기서 다시 쓰지 않고 같은
방식으로 합쳐 본다 — `global.config | workspace.config`.

상한 두 개는 **합치지 않는다.** `max_concurrency` 와 `daily_calls` 는
`reserve` 가 전역 행과 워크스페이스 행에서 **따로** 읽어 둘 다 만족해야
통과시킨다(전체 상한과 워크스페이스 상한이다). 그래서 활성화하려면 두 행이
각자 그 값을 들고 있어야 한다.

## 충돌

`expectedRevision` 이 지금 행의 revision 과 다르면 거절한다. 판정은
`ImageDescriptionStore.save_policy` 가 **전역 행을 잠근 뒤** 하므로, 이 모듈이
먼저 읽어 본 값이 낡았더라도 commit 되지 않는다.

읽기 API 가 내려 주는 `revision` 은 `전역:워크스페이스` **합성**이다. 화면이 그
값을 그대로 돌려주므로 여기서 갈라 쓴다. 안 갈라 쓰면 저장이 영원히
`policy_conflict` 로 떨어진다.

## 사유와 실행자

`save_policy` 는 실행자만 원장에 남긴다. 사유 칸이 스키마에 없다 — 스키마는
Codex 소유라 여기서 고치지 않는다. 대신 콘솔 감사(`console_audit_event`)에 넣을
레코드를 **만들어서 돌려준다.** 라우트가 그것을 기록한다. 사유 없는 변경은
저장 자체를 거절한다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tybot.gateway.base import Sensitivity

from ..archive.image_description_store import (
    CONFIG_KEYS,
    ImageDescriptionDenied,
    ImageDescriptionStore,
)
from ..archive.image_model_catalog import (
    FreeVisionModelUnavailable,
    select_free_vision_model,
)
from . import image_description_admin

#: 행 → 카탈로그 항목 변환은 저장소가 소유한다. **import 시점에 묶어 둔다** —
#: `ImageDescriptionStore` 를 통해 찾으면 시험이 저장소를 바꿔 끼울 때 같이
#: 바뀌고, 그러면 변환까지 가짜가 된다.
_catalog_entry = ImageDescriptionStore._catalog_entry

#: 사유는 사람이 적는다. 상한은 감사 칸(`metadata`)이 감당할 크기로만 둔다.
MAX_REASON_CHARS = 500

#: `reserve` 가 전역 행과 워크스페이스 행에서 **따로** 읽는 상한.
#: 합쳐서 보면 한쪽만 채워도 통과해 버린다.
PER_ROW_KEYS = frozenset({"max_concurrency", "daily_calls"})

#: 나머지는 합친 값으로 본다. 목록을 여기 베끼지 않는다 — 저장소가 키를 늘리면
#: 따라온다.
MERGED_KEYS = frozenset(CONFIG_KEYS) - PER_ROW_KEYS

GLOBAL = ""

#: 콘솔로 나가는 코드의 모양. `image_description_admin._safe_code` 와 같은 기준이다.
_CODE = re.compile(r"^[a-z0-9_]+$")


class ImageDescriptionPolicyAdminError(RuntimeError):
    """콘솔에 그대로 내보내도 되는 고정 코드 거절."""

    def __init__(self, code: str, *, blockers: tuple[str, ...] = ()) -> None:
        self.code = code
        self.blockers = blockers
        super().__init__(code)


@dataclass(frozen=True)
class PolicyChangeRequest:
    workspace: str
    expected_revision: str | None
    enabled: bool
    config: dict
    reason: str
    actor: str


@dataclass(frozen=True)
class PolicyChangePreview:
    """저장하면 어떻게 되는가. **쓰지 않는다.**"""

    workspace: str
    enabled: bool
    row_revision: str
    changed_keys: tuple[str, ...]
    blockers: tuple[str, ...] = ()

    @property
    def savable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class PolicyChangeOutcome:
    workspace: str
    revision: str
    previous_revision: str
    enabled: bool
    changed_keys: tuple[str, ...]
    audit: dict = field(default_factory=dict)


# --- 값 검사 -----------------------------------------------------------------


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(value: object) -> bool:
    """세는 값. `bool` 은 `int` 의 하위형이라 **먼저 걸러야** 1/0 이 통과한다."""
    return type(value) is int and value > 0


def _seconds(value: object) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and value > 0
    )


#: 키마다 무엇이 올바른가. `preflight_image_description` 이 전송 직전에 보는
#: 것과 같은 기준이다 — 시험이 둘을 대조한다.
VALIDATORS = {
    "model": _text,
    "provider": _text,
    "region": _text,
    "sensitivity": lambda value: isinstance(value, str)
    and value in {item.value for item in Sensitivity},
    "catalog_verification_ttl_seconds": _seconds,
    "timeout_seconds": _seconds,
    "max_image_bytes": _count,
    "max_output_tokens": _count,
    "max_concurrency": _count,
    "daily_calls": _count,
}


def _validate_request(request: PolicyChangeRequest) -> None:
    if not isinstance(request.workspace, str):
        raise ImageDescriptionPolicyAdminError("policy_invalid")
    if type(request.enabled) is not bool:
        raise ImageDescriptionPolicyAdminError("policy_invalid")
    if not isinstance(request.config, dict):
        raise ImageDescriptionPolicyAdminError("policy_invalid")
    if not _text(request.actor):
        raise ImageDescriptionPolicyAdminError("actor_required")
    if not _text(request.reason):
        raise ImageDescriptionPolicyAdminError("reason_required")
    if len(request.reason) > MAX_REASON_CHARS:
        raise ImageDescriptionPolicyAdminError("reason_too_long")
    if request.expected_revision is not None and not isinstance(
        request.expected_revision, str
    ):
        raise ImageDescriptionPolicyAdminError("policy_invalid")
    unknown = set(request.config) - set(CONFIG_KEYS)
    if unknown:
        # 모르는 키를 조용히 버리면 운영자는 저장됐다고 믿는다.
        raise ImageDescriptionPolicyAdminError("config_key_unknown")
    for key, value in sorted(request.config.items()):
        if not VALIDATORS[key](value):
            raise ImageDescriptionPolicyAdminError(f"config_invalid_{key}")


def _row_revision(expected: str | None, *, workspace: str) -> str | None:
    """읽기 API 의 합성 `revision` 을 **쓰는 행의** revision 으로 바꾼다.

    `effective_policy` 는 `전역:워크스페이스` 를 돌려준다. 화면이 그 값을 그대로
    돌려주는데 `save_policy` 는 행 하나의 revision 과 비교하므로, 안 갈라 쓰면
    저장이 영원히 `policy_conflict` 가 된다.
    """
    if expected is None or ":" not in expected:
        return expected
    head, _, tail = expected.partition(":")
    return head if workspace == GLOBAL else tail


# --- 지금 상태 읽기 ----------------------------------------------------------


def _rows(cur, workspace: str) -> dict:
    cur.execute(
        """SELECT workspace, revision, enabled, config
             FROM archiver_image_description_policy
            WHERE workspace = ANY(%s)""",
        ([GLOBAL, workspace],),
    )
    return {str(row["workspace"]): dict(row) for row in cur.fetchall()}


def _catalog(cur) -> tuple:
    cur.execute("SELECT * FROM archiver_image_model_catalog")
    # 변환 규칙을 여기 베껴 적으면 칸이 하나 늘어난 날 둘이 갈라지고,
    # 갈라진 쪽은 **없는 자격을 있다고** 읽는다.
    return tuple(_catalog_entry(row) for row in cur.fetchall())


# --- 활성화 판정 -------------------------------------------------------------


def _activation_blockers(
    *, workspace: str, merged: dict, rows: dict, proposed: dict, entries, now: datetime
) -> tuple[str, ...]:
    """켤 수 있는가. 켤 때만 본다 — 끄는 것은 언제나 할 수 있어야 한다."""
    blockers: list[str] = []
    for key in sorted(MERGED_KEYS):
        if key not in merged or not VALIDATORS[key](merged[key]):
            blockers.append(f"activation_requires_{key}")

    # 전역 행과 워크스페이스 행이 **각자** 들고 있어야 한다.
    per_row = {GLOBAL: dict(rows.get(GLOBAL, {}).get("config") or {})}
    if workspace != GLOBAL:
        per_row[workspace] = dict(rows.get(workspace, {}).get("config") or {})
    per_row[workspace] = per_row.get(workspace, {}) | proposed
    for name, config in sorted(per_row.items()):
        where = "global" if name == GLOBAL else "workspace"
        for key in sorted(PER_ROW_KEYS):
            if key not in config or not VALIDATORS[key](config[key]):
                blockers.append(f"activation_requires_{where}_{key}")

    if blockers:
        # 모델 판정은 상한이 선 뒤에 본다. 둘을 섞어 내보내면 무엇부터 고칠지
        # 흐려지고, 애초에 민감도·리전이 없으면 모델을 평가할 수 없다.
        return tuple(blockers)

    try:
        select_free_vision_model(
            entries,
            model=merged["model"],
            provider=merged["provider"],
            sensitivity=Sensitivity(merged["sensitivity"]),
            region=merged["region"],
            now=now,
            verification_ttl=timedelta(
                seconds=merged["catalog_verification_ttl_seconds"]
            ),
        )
    except FreeVisionModelUnavailable as exc:
        # 카탈로그가 확인한 무료 Vision 모델만 고를 수 있다. 막힌 모델을 다른
        # 모델로 바꿔 주지 않는다 — 유료로 조용히 넘어가는 길이 거기서 생긴다.
        return tuple(f"model_{code}" for code in exc.blockers)
    return ()


def _assess(request: PolicyChangeRequest, *, cur, now: datetime):
    rows = _rows(cur, request.workspace)
    current = rows.get(request.workspace, {})
    merged = dict(rows.get(GLOBAL, {}).get("config") or {})
    if request.workspace != GLOBAL:
        merged |= dict(current.get("config") or {})
    merged |= request.config

    blockers: tuple[str, ...] = ()
    if request.enabled:
        blockers = _activation_blockers(
            workspace=request.workspace,
            merged=merged,
            rows=rows,
            proposed=request.config,
            entries=_catalog(cur),
            now=now,
        )

    before = dict(current.get("config") or {})
    changed = tuple(
        sorted(key for key, value in request.config.items() if before.get(key) != value)
    )
    if current and current.get("enabled") != request.enabled:
        changed = (*changed, "enabled")
    return current, blockers, changed


# --- 공개 경계 ---------------------------------------------------------------


def preview_policy_change(
    request: PolicyChangeRequest, *, now: datetime, connect=None
) -> PolicyChangePreview:
    """저장하면 어떻게 되는지만 본다. **아무것도 쓰지 않는다.**

    화면이 저장 단추를 막고 이유를 보여 주려면 저장을 시도해 보기 전에 알아야
    한다. 저장해 보고 실패로 배우게 하면 그 실패가 감사에 남는다.
    """
    _validate_request(request)
    connector = connect or image_description_admin._connect
    try:
        with connector() as conn, conn.cursor() as cur:
            current, blockers, changed = _assess(request, cur=cur, now=now)
    except ImageDescriptionPolicyAdminError:
        raise
    except image_description_admin.ImageDescriptionAdminError as exc:
        # 자격 쪽 코드는 **그대로 올려 보낸다.** `policy_unavailable` 로 뭉개면
        # DSN 이 없는 것과 DB 가 죽은 것이 같은 글자가 되고, 운영자는 고칠
        # 자리를 못 찾는다.
        raise ImageDescriptionPolicyAdminError(_safe(str(exc))) from None
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionPolicyAdminError(_code(exc)) from None
    except Exception:  # noqa: BLE001 - DB 예외 본문에는 행 값이 들어 있다
        raise ImageDescriptionPolicyAdminError("policy_unavailable") from None
    return PolicyChangePreview(
        workspace=request.workspace,
        enabled=request.enabled,
        row_revision=str(current.get("revision") or ""),
        changed_keys=changed,
        blockers=blockers,
    )


def save_policy_change(
    request: PolicyChangeRequest, *, now: datetime, connect=None
) -> PolicyChangeOutcome:
    """검사한 뒤 행 하나를 고쳐 쓴다. 모델도 소급 작업도 시작하지 않는다."""
    _validate_request(request)
    connector = connect or image_description_admin._connect
    try:
        with connector() as conn, conn.cursor() as cur:
            current, blockers, changed = _assess(request, cur=cur, now=now)
            if blockers:
                raise ImageDescriptionPolicyAdminError(
                    "activation_blocked", blockers=blockers
                )
            revision = ImageDescriptionStore(conn).save_policy(
                request.workspace,
                expected_revision=_row_revision(
                    request.expected_revision, workspace=request.workspace
                ),
                enabled=request.enabled,
                config=request.config,
                actor=request.actor,
            )
    except ImageDescriptionPolicyAdminError:
        raise
    except image_description_admin.ImageDescriptionAdminError as exc:
        # 자격 쪽 코드는 **그대로 올려 보낸다.** `policy_unavailable` 로 뭉개면
        # DSN 이 없는 것과 DB 가 죽은 것이 같은 글자가 되고, 운영자는 고칠
        # 자리를 못 찾는다.
        raise ImageDescriptionPolicyAdminError(_safe(str(exc))) from None
    except ImageDescriptionDenied as exc:
        raise ImageDescriptionPolicyAdminError(_code(exc)) from None
    except Exception:  # noqa: BLE001 - DB 예외 본문에는 행 값이 들어 있다
        raise ImageDescriptionPolicyAdminError("policy_unavailable") from None

    previous = str(current.get("revision") or "")
    return PolicyChangeOutcome(
        workspace=request.workspace,
        revision=revision,
        previous_revision=previous,
        enabled=request.enabled,
        changed_keys=changed,
        audit=_audit_record(request, revision=revision, previous=previous, changed=changed),
    )


def _safe(value: str) -> str:
    """ASCII 고정 코드만 통과시킨다. 예외 본문이 코드 자리로 새면 안 된다."""
    return value if _CODE.fullmatch(value) else "policy_unavailable"


def _code(exc: ImageDescriptionDenied) -> str:
    value = exc.args[0] if exc.args else ""
    return _safe(value) if isinstance(value, str) else "policy_unavailable"


def _audit_record(
    request: PolicyChangeRequest, *, revision: str, previous: str, changed: tuple[str, ...]
) -> dict:
    """`audit_store.record(**audit)` 에 그대로 넣을 수 있는 모양.

    사유가 여기 들어간다 — 원장 스키마에는 그 칸이 없다. 값(모델 이름·상한)은
    원장이 들고 있으므로 여기서는 **무엇이 바뀌었는지**만 적는다.
    """
    return {
        "actor": request.actor,
        "category": "archiver",
        "action": "image_description_policy_save",
        "target_type": "image_description_policy",
        "target_id": request.workspace or "global",
        "workspace": request.workspace or None,
        "outcome": "succeeded",
        "metadata": {
            "reason": request.reason,
            "enabled": request.enabled,
            "changed_keys": list(changed),
            "previous_revision": previous,
            "revision": revision,
        },
    }


__all__ = [
    "GLOBAL",
    "MAX_REASON_CHARS",
    "MERGED_KEYS",
    "PER_ROW_KEYS",
    "ImageDescriptionPolicyAdminError",
    "PolicyChangeOutcome",
    "PolicyChangePreview",
    "PolicyChangeRequest",
    "preview_policy_change",
    "save_policy_change",
]
