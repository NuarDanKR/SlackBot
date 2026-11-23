"""B-80 PDF 쪽 Vision 전사 — **정책·요금표·예산** 콘솔 경계.

## 왜 필요한가

B-80 원장(`VisionStore`)은 정책 행이 없으면 아무것도 예약하지 않는다(`policy_unavailable`).
그런데 그 행을 쓰는 길이 콘솔에도 스크립트에도 없었다. 서버에서 SQL 을 직접 치는 것은
운영 기능이 아니다(CLAUDE.md 「운영 기능은 콘솔에 둔다」).

## 요금표가 정산의 근거다

정산은 `토큰 × 요금표` 로 한다(`vision_store.TARIFF_KEYS`). gateway 가 말하는 비용은
Router 단가가 0 이면 0 이라 믿지 않는다. 그래서 요금표가 없으면 **켤 수 없다.**
요금표는 운영자가 provider 공식 가격표를 확인해 적는 값이고, 누가 언제 무엇으로
바꿨는지는 원장 감사(`archiver_vision_policy_audit`)와 콘솔 감사에 남는다.

## 저장은 허가가 아니다

저장은 행 하나를 고쳐 쓴다. 모델을 부르지 않고 소급을 시작하지 않는다. 예약·전송
직전에 `reserve`·`begin_dispatch` 가 현재 사실로 다시 판정한다.

## 화면에 나오지 않는 것

전사 본문·원본·키. 작업 목록은 좌표(파일 ID·쪽)·상태·금액만 보인다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..archive.vision_store import (
    CONFIG_KEYS,
    GLOBAL_ONLY_KEYS,
    TARIFF_KEYS,
    WORKSPACE_LOCKED_KEYS,
    VisionDenied,
    VisionStore,
    global_sensitivity,
    tariff,
)
from ..gateway.base import Sensitivity
from . import image_description_admin

GLOBAL = ""
MAX_REASON_CHARS = 500
MAX_JOBS = 200

#: `reserve` 가 **전역 행에서 따로** 읽는 상한. 전역 행이 들고 있지 않으면 늘 막힌다.
GLOBAL_ROW_KEYS = ("daily_usd", "max_concurrency")
#: 켜려면 합친 설정에 있어야 하는 칸. 민감도는 기본값(일반 업무)이 있어 빠진다.
REQUIRED_KEYS = (
    "provider", "model", "max_image_bytes", "max_output_tokens", "timeout_seconds",
    "daily_usd", "job_usd", "max_concurrency", *TARIFF_KEYS,
)
MONEY_KEYS = frozenset({"daily_usd", "job_usd", *TARIFF_KEYS})

_CODE = re.compile(r"^[a-z0-9_]+$")


class PdfVisionAdminError(RuntimeError):
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
class PolicyChangeOutcome:
    workspace: str
    revision: str
    previous_revision: str
    enabled: bool
    changed_keys: tuple[str, ...]
    audit: dict = field(default_factory=dict)


# --- 값 검사 -----------------------------------------------------------------


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def _count(value) -> bool:
    return type(value) is int and value > 0


def _seconds(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 600


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _money(value) -> bool:
    return _decimal(value) is not None


def _positive_money(value) -> bool:
    amount = _decimal(value)
    return amount is not None and amount > 0


VALIDATORS = {
    "provider": _text,
    "model": _text,
    "sensitivity": lambda v: isinstance(v, str) and v in {s.value for s in Sensitivity},
    "max_image_bytes": _count,
    "max_output_tokens": _count,
    "max_concurrency": _count,
    "timeout_seconds": _seconds,
    "daily_usd": _positive_money,
    "job_usd": _positive_money,
    # 0 원 요금표는 정산을 0 으로 만든다. 무료 모델은 B-81 건수 한도 경로다.
    "input_usd_per_mtok": _positive_money,
    "output_usd_per_mtok": _positive_money,
}
assert set(VALIDATORS) == set(CONFIG_KEYS), "원장 설정 칸과 콘솔 검사가 갈라졌다"


def _normalize(config: dict) -> dict:
    """금액은 문자열로 저장한다(Decimal 정밀도). float 로 두면 0.1 이 0.1000000000000000055 가 된다."""
    out = {}
    for key, value in config.items():
        if key in MONEY_KEYS:
            out[key] = str(_decimal(value))
        elif isinstance(value, str):
            out[key] = value.strip()
        else:
            out[key] = value
    return out


def _validate(request: PolicyChangeRequest) -> None:
    if not isinstance(request.workspace, str) or type(request.enabled) is not bool:
        raise PdfVisionAdminError("policy_invalid")
    if not isinstance(request.config, dict):
        raise PdfVisionAdminError("policy_invalid")
    if not _text(request.actor):
        raise PdfVisionAdminError("actor_required")
    if not isinstance(request.reason, str) or not request.reason.strip():
        raise PdfVisionAdminError("reason_required")
    if len(request.reason) > MAX_REASON_CHARS:
        raise PdfVisionAdminError("reason_too_long")
    if set(request.config) - set(CONFIG_KEYS):
        # 모르는 칸을 조용히 버리면 운영자는 저장됐다고 믿는다.
        raise PdfVisionAdminError("config_key_unknown")
    for key, value in sorted(request.config.items()):
        if not VALIDATORS[key](value):
            raise PdfVisionAdminError(f"config_invalid_{key}")
    if request.workspace != GLOBAL:
        # 모델·요금표는 전역 예산의 근거다. 워크스페이스 관리자가 바꾸면 전역 예산을 우회한다.
        # 민감도도 같다 — 낮추면 전역이 막은 모델 카탈로그를 통과한다(2026-11-19 보안 검토).
        overridden = tuple(sorted(set(request.config) & WORKSPACE_LOCKED_KEYS))
        if overridden:
            raise PdfVisionAdminError("config_global_only", blockers=overridden)


def activation_blockers(*, global_config: dict, merged: dict) -> tuple[str, ...]:
    """켤 수 있는가. 켤 때만 본다 — 끄는 것은 언제나 된다."""
    blockers = [f"activation_requires_{k}" for k in REQUIRED_KEYS
                if k not in merged or not VALIDATORS[k](merged[k])]
    blockers += [f"activation_requires_global_{k}" for k in (*GLOBAL_ROW_KEYS, *sorted(GLOBAL_ONLY_KEYS))
                 if k not in global_config or not VALIDATORS[k](global_config[k])]
    blockers += [f"workspace_overrides_{k}" for k in sorted(GLOBAL_ONLY_KEYS)
                 if k in merged and merged.get(k) != global_config.get(k)]
    expected = global_sensitivity(global_config)
    if str(merged.get("sensitivity", expected)) != expected:
        blockers.append("workspace_overrides_sensitivity")
    if blockers:
        return tuple(blockers)
    rates = tariff(merged)
    bound = Decimal(merged["max_output_tokens"]) * rates[1] / Decimal(1_000_000)
    if _decimal(merged["job_usd"]) < bound:
        # 쪽당 상한이 출력 상한 비용보다 작으면 모든 예약이 `reservation_below_tariff` 다.
        return ("job_usd_below_output_bound",)
    return ()


def _row_revision(expected: str | None, *, workspace: str) -> str | None:
    """읽기 응답의 합성 revision(`전역:워크스페이스`)을 쓰는 행의 revision 으로."""
    if expected is None or ":" not in expected:
        return expected
    head, _, tail = expected.partition(":")
    return head if workspace == GLOBAL else tail


# --- DB ----------------------------------------------------------------------


def _connect():
    try:
        return image_description_admin._connect()
    except image_description_admin.ImageDescriptionAdminError as exc:
        raise PdfVisionAdminError(_safe(str(exc))) from None


def _safe(value: str) -> str:
    return value if _CODE.fullmatch(value or "") else "pdf_vision_unavailable"


def _rows(cur, workspace: str) -> dict:
    cur.execute(
        """SELECT workspace, revision, enabled, config, updated_by, updated_at
             FROM archiver_vision_policy WHERE workspace = ANY(%s)""",
        ([GLOBAL, workspace],),
    )
    return {str(r["workspace"]): dict(r) for r in cur.fetchall()}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _usd(value) -> str | None:
    return None if value is None else str(value)


def snapshot(workspace: str, *, limit: int = 50, connect=None) -> dict:
    """정책·요금표·오늘 사용액·작업 상태. 본문·원본·키는 없다."""
    connector = connect or _connect
    limit = max(1, min(int(limit), MAX_JOBS))
    try:
        with connector() as conn, conn.cursor() as cur:
            rows = _rows(cur, workspace)
            cur.execute(
                """SELECT state, count(*) AS n FROM archiver_vision_job
                    WHERE workspace=%s GROUP BY state""", (workspace,))
            counts = {r["state"]: int(r["n"]) for r in cur.fetchall()}
            # 원장 `reserve` 와 같은 식: 진행·미확정은 예약액, 정산은 오늘(서울) 실제액.
            cur.execute(
                """SELECT COALESCE(sum(CASE
                       WHEN state IN ('reserved','dispatching','unknown') THEN reserved_usd
                       WHEN state='settled' AND (finished_at AT TIME ZONE 'Asia/Seoul')::date
                            = (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date THEN actual_usd
                       ELSE 0 END), 0) AS charged
                     FROM archiver_vision_job WHERE workspace=%s""", (workspace,))
            charged = cur.fetchone()["charged"]
            cur.execute(
                """SELECT id, state, scope->>'file_id' AS file_id,
                          (scope->>'page_number')::int AS page, reserved_usd, actual_usd,
                          created_at, finished_at
                     FROM archiver_vision_job WHERE workspace=%s
                    ORDER BY created_at DESC LIMIT %s""", (workspace, limit))
            jobs = cur.fetchall()
    except PdfVisionAdminError:
        raise
    except Exception:  # noqa: BLE001 - DB 예외 본문에 행 값이 들어 있다
        raise PdfVisionAdminError("pdf_vision_unavailable") from None

    global_row = rows.get(GLOBAL, {})
    local_row = rows.get(workspace, {})
    global_config = dict(global_row.get("config") or {})
    local_config = dict(local_row.get("config") or {})
    merged = global_config | local_config
    return {
        "workspace": workspace,
        "policy": {
            "revision": f"{global_row.get('revision', '')}:{local_row.get('revision', '')}",
            "globalEnabled": bool(global_row.get("enabled")),
            "workspaceEnabled": bool(local_row.get("enabled")),
            "workspaceRowExists": bool(local_row),
            "global": global_config,
            "workspace": local_config,
            "effective": merged,
            "tariffConfigured": tariff(merged) is not None,
            "blockers": list(activation_blockers(global_config=global_config, merged=merged)),
        },
        "todayChargedUsd": _usd(charged),
        "counts": counts,
        "jobs": [
            {
                "id": j["id"], "state": j["state"], "fileId": j["file_id"], "page": j["page"],
                "reservedUsd": _usd(j["reserved_usd"]), "actualUsd": _usd(j["actual_usd"]),
                "createdAt": _iso(j["created_at"]), "finishedAt": _iso(j["finished_at"]),
            }
            for j in jobs
        ],
    }


def save_policy_change(request: PolicyChangeRequest, *, connect=None) -> PolicyChangeOutcome:
    """검사한 뒤 행 하나를 고쳐 쓴다. 모델도 소급도 시작하지 않는다."""
    _validate(request)
    config = _normalize(request.config)
    connector = connect or _connect
    try:
        with connector() as conn:
            with conn.cursor() as cur:
                rows = _rows(cur, request.workspace)
            current = rows.get(request.workspace, {})
            global_config = dict(rows.get(GLOBAL, {}).get("config") or {})
            if request.workspace == GLOBAL:
                global_config = global_config | config
                merged = global_config
            else:
                merged = global_config | dict(current.get("config") or {}) | config
            if request.enabled:
                blockers = activation_blockers(global_config=global_config, merged=merged)
                if blockers:
                    raise PdfVisionAdminError("activation_blocked", blockers=blockers)
            # 칸 단위 덮어쓰기: 보낸 칸만 바꾸고 나머지는 지금 값 그대로.
            new_config = dict(current.get("config") or {}) | config
            revision = VisionStore(conn).save_policy(
                request.workspace,
                expected_revision=_row_revision(request.expected_revision,
                                                workspace=request.workspace),
                enabled=request.enabled, config=new_config, actor=request.actor,
            )
    except PdfVisionAdminError:
        raise
    except VisionDenied as exc:
        raise PdfVisionAdminError(_safe(str(exc.args[0]) if exc.args else "")) from None
    except Exception:  # noqa: BLE001 - DB 예외 본문에 행 값이 들어 있다
        raise PdfVisionAdminError("pdf_vision_unavailable") from None

    before = dict(current.get("config") or {})
    changed = tuple(sorted(k for k, v in config.items() if before.get(k) != v))
    if bool(current.get("enabled")) != request.enabled:
        changed = (*changed, "enabled")
    previous = str(current.get("revision") or "")
    return PolicyChangeOutcome(
        workspace=request.workspace, revision=revision, previous_revision=previous,
        enabled=request.enabled, changed_keys=changed,
        audit={
            "actor": request.actor,
            "category": "archiver",
            "action": "pdf_vision_policy_save",
            "target_type": "pdf_vision_policy",
            "target_id": request.workspace or "global",
            "workspace": request.workspace or None,
            "outcome": "succeeded",
            "metadata": {
                "reason": request.reason.strip(),
                "enabled": request.enabled,
                "changed_keys": list(changed),
                "previous_revision": previous,
                "revision": revision,
            },
        },
    )


__all__ = [
    "GLOBAL", "MAX_REASON_CHARS", "PdfVisionAdminError", "PolicyChangeOutcome",
    "PolicyChangeRequest", "activation_blockers", "save_policy_change", "snapshot",
]
