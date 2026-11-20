"""B-81 이미지 설명 배치의 보수적인 연결 계층.

정본 후보 선별, 이미 발행된 원본 제외, 원본 bytes 재검증, 정책 결속 예약,
one-shot worker 실행을 순서대로 잇는다. 이 모듈은 정책을 켜지 않고, 승인 행을
만들지 않으며(가짜 `pii_cleared` 행 포함), 실패를 재시도하지 않고, 결과에 원본
bytes·설명 본문·경로를 넣지 않는다.

2026-11-20 오너 결정: 이미지별 PII clearance 와 건별 외부 전송 approval 은 런타임
전제가 아니다. 정책 ON·현재 ACL·정본 좌표와 원본 해시·봇 출력 제외·무료 모델과
quota·중복 방지는 그대로다.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime

from .image_description import PROMPT_REVISION, ImageInput
from .image_description_clearance import verify_current_access
from .image_description_original import load_original
from .image_description_runner import CALLER_CHECKS
from .image_description_source import (
    ImageCandidate,
    confirmed_human_file_ids,
    select_image_candidates,
)
from .image_description_store import ImageDescriptionDenied
from .image_description_worker import dispatch_reserved_image

_CODE = re.compile(r"^[a-z0-9_]+$")
_TRUSTED_CHECKS = ("source_verified", "bot_generated")
_FRESH_CHECKS = frozenset((*CALLER_CHECKS, *_TRUSTED_CHECKS, "clearance_code"))
_STORE_CHECKS = (*CALLER_CHECKS, *_TRUSTED_CHECKS)


@dataclass(frozen=True)
class ImageDescriptionBatchItem:
    workspace: str
    channel_id: str
    file_id: str
    state: str
    message_ts: str = ""
    code: str = ""
    job_id: str = ""
    revision_id: str = ""


@dataclass(frozen=True)
class ImageDescriptionBatch:
    selected: int
    skipped: int
    items: tuple[ImageDescriptionBatchItem, ...]
    skip_counts: dict[str, int]


def _code(exc: Exception, fallback: str) -> str:
    """예외 본문 대신 검증된 제어 코드만 내보낸다."""
    value = getattr(exc, "code", "")
    if not value and isinstance(exc, ImageDescriptionDenied) and exc.args:
        value = exc.args[0]
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def _item(
    candidate: ImageCandidate,
    state: str,
    *,
    code: str = "",
    job_id: str = "",
    revision_id: str = "",
) -> ImageDescriptionBatchItem:
    return ImageDescriptionBatchItem(
        workspace=candidate.workspace,
        channel_id=candidate.channel_id,
        file_id=candidate.file_id,
        message_ts=candidate.message_ts,
        state=state,
        code=code,
        job_id=job_id,
        revision_id=revision_id,
    )


def _fresh_check_blocker(checks: object) -> str:
    if not isinstance(checks, dict):
        return "checks_invalid"
    if set(checks) - _FRESH_CHECKS:
        return "checks_invalid"
    clearance_code = checks.get("clearance_code", "")
    if clearance_code:
        return (
            clearance_code
            if isinstance(clearance_code, str) and _CODE.fullmatch(clearance_code)
            else "checks_invalid"
        )
    if checks.get("access_verified") is not True:
        return "access_unverified"
    # PII 판별은 전제가 아니다. 호출자가 명시적으로 막았을 때만 막는다.
    if checks.get("pii_blocked", False) is not False:
        return "pii_blocked"
    if checks.get("bot_generated") is not False:
        return "bot_generated"
    return ""


class _OriginalDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def run_approved_batch(
    docs,
    *,
    workspace: str,
    store,
    router,
    load_image,
    checks_for,
    dispatch_expires_at: datetime,
    approval_id_for=None,
    completion_notifier=None,
    limit: int = 50,
) -> ImageDescriptionBatch:
    """현재 정본에서 보낼 수 있는 후보만 한 번씩 실행한다.

    ``approval_id_for`` 는 옛 건별 승인 경로(호환)다. 주지 않으면 정책 판에 결속된
    예약을 한다 — 승인 행을 찾지도 만들지도 않는다.

    ``load_image(candidate, doc)`` 는 관리 경로에서 bytes와 해시를 다시 확인한
    :class:`ImageInput`을 돌려줘야 한다. ``checks_for`` 는 실행 직전의 ACL·PII·
    봇 생성 여부를 제공한다. 어느 콜백도 실패 본문을 결과에 싣지 않는다.
    """
    current = tuple(doc for doc in docs if doc.workspace == workspace)
    indexed = store.indexed_sources(workspace)
    selection = select_image_candidates(
        current,
        human_file_ids=confirmed_human_file_ids(current),
        already_indexed=indexed,
        limit=limit,
    )
    by_key = {
        (doc.workspace, doc.channel_id, doc.sha256.lower()): doc
        for doc in current
        if doc.sha256
    }
    items: list[ImageDescriptionBatchItem] = []

    for candidate in selection.candidates:
        doc = by_key.get(candidate.dedupe_key)
        if doc is None:
            items.append(_item(candidate, "blocked", code="source_changed"))
            continue
        try:
            checks = checks_for(candidate, doc)
        except Exception:  # noqa: BLE001 - ACL/PII 조회의 본문을 내보내지 않는다
            items.append(_item(candidate, "blocked", code="checks_failed"))
            continue
        blocker = _fresh_check_blocker(checks)
        if blocker:
            items.append(_item(candidate, "blocked", code=blocker))
            continue
        # `clearance_code` is a coordinator decision aid, not a dispatch fact.
        # Keep the two caller facts and two loader-derived facts only.
        checks = {key: checks.get(key, False) for key in _STORE_CHECKS}
        approval_id = ""
        if approval_id_for is not None:
            try:
                approval_id = approval_id_for(candidate)
            except Exception:  # noqa: BLE001 - DB/provider 본문 없이 고정 코드만 보낸다.
                items.append(_item(candidate, "blocked", code="approval_lookup_failed"))
                continue
            if not approval_id:
                items.append(_item(candidate, "approval_required", code="approval_missing"))
                continue
        try:
            image = load_image(candidate, doc)
        except Exception as exc:  # noqa: BLE001 - 본문 없는 코드로 접는다
            items.append(_item(candidate, "blocked", code=_code(exc, "source_load_failed")))
            continue
        if not isinstance(image, ImageInput):
            items.append(_item(candidate, "blocked", code="source_load_invalid"))
            continue
        # source_verified는 callback의 주장이 아니라 loader가 bytes·해시를 다시
        # 확인하고 ImageInput을 만든 뒤에만 참이 된다.
        checks = checks | {"source_verified": True}
        try:
            receipt = store.reserve(
                image,
                approval_id=approval_id,
                prompt_revision=PROMPT_REVISION,
                expires_at=dispatch_expires_at,
                checks=checks,
            )
        except Exception as exc:  # noqa: BLE001 - 저장소도 제어 코드만 내보낸다
            items.append(_item(candidate, "blocked", code=_code(exc, "reserve_failed")))
            continue
        try:
            run = dispatch_reserved_image(
                store,
                receipt.dispatch_id,
                image,
                router=router,
                checks=checks,
            )
        except Exception as exc:  # noqa: BLE001 - worker 본문·provider 응답 금지
            items.append(
                _item(
                    candidate,
                    "unknown",
                    code=_code(exc, "dispatch_failed"),
                    job_id=receipt.dispatch_id,
                )
            )
            continue
        item = _item(
            candidate,
            run.state,
            code=run.code,
            job_id=run.job_id,
            revision_id=run.revision_id,
        )
        items.append(item)
        # Notification is only an acknowledgement of an already-published
        # search projection.  It never changes the worker result and cannot
        # cause a second provider call when its local outbox is unavailable.
        if run.revision_id and candidate.message_ts and completion_notifier is not None:
            with contextlib.suppress(Exception):
                completion_notifier(item)

    return ImageDescriptionBatch(
        selected=len(selection.candidates),
        skipped=len(selection.skipped),
        items=tuple(items),
        skip_counts=selection.counts,
    )


def run_cleared_batch(
    docs,
    *,
    workspace: str,
    store,
    router,
    archive_root,
    dispatch_expires_at: datetime,
    now: datetime,
    limit: int = 50,
    completion_notifier=None,
) -> ImageDescriptionBatch:
    """DB 정책과 현재 정본을 다시 읽어 보낼 수 있는 원본만 실행한다.

    이 함수는 현재 정책을 한 번 읽고, 후보마다 정본 좌표·원본 해시·현재 ACL 을 확인한
    뒤에만 원본을 연다. 이미지별 PII clearance·건별 approval 은 조회하지 않는다
    (2026-11-20 오너 결정). :func:`run_approved_batch`와 저장소가 예약·dispatch 시
    정책 판을 다시 대조하므로, 중간에 정책이 바뀌면 호출은 닫히는 쪽으로 끝난다.
    """
    policy, _config, _sources = store.effective_policy(workspace)

    def checks_for(candidate, doc) -> dict:
        if policy.enabled is not True or policy.workspace_enabled is not True:
            return {
                "access_verified": False,
                "bot_generated": False,
                "clearance_code": "image_description_disabled",
            }
        code = verify_current_access(candidate, doc=doc)
        return {
            "access_verified": code == "",
            "bot_generated": False,
            "clearance_code": code,
        }

    def open_original(candidate, doc) -> ImageInput:
        loaded = load_original(
            candidate,
            archive_root=archive_root,
            doc=doc,
            max_image_bytes=policy.max_image_bytes,
        )
        if not loaded.ok:
            raise _OriginalDenied(loaded.error_code or "source_load_failed")
        return loaded.image

    return run_approved_batch(
        docs,
        workspace=workspace,
        store=store,
        router=router,
        load_image=open_original,
        checks_for=checks_for,
        dispatch_expires_at=dispatch_expires_at,
        completion_notifier=completion_notifier,
        limit=limit,
    )


__all__ = [
    "ImageDescriptionBatch",
    "ImageDescriptionBatchItem",
    "run_approved_batch",
    "run_cleared_batch",
]
