"""B-81 이미지 설명 배치의 보수적인 연결 계층.

정본 후보 선별, 이미 발행된 원본 제외, 원본 bytes 재검증, 기존 승인에 따른 예약,
one-shot worker 실행을 순서대로 잇는다. 이 모듈은 승인을 만들거나 정책을 켜지 않고,
실패를 재시도하지 않으며, 결과에 원본 bytes·설명 본문·경로를 넣지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .image_description import PROMPT_REVISION, ImageInput
from .image_description_clearance import clear_for_dispatch
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
    if checks.get("pii_blocked") is not False:
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
    approval_id_for,
    checks_for,
    dispatch_expires_at: datetime,
    limit: int = 50,
) -> ImageDescriptionBatch:
    """현재 정본에서 승인된 후보만 한 번씩 실행한다.

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
        checks = {key: checks.get(key) for key in _STORE_CHECKS}
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
        items.append(
            _item(
                candidate,
                run.state,
                code=run.code,
                job_id=run.job_id,
                revision_id=run.revision_id,
            )
        )

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
) -> ImageDescriptionBatch:
    """DB 정책과 사람 안전 판정을 다시 읽어 승인된 원본만 실행한다.

    정책·clearance는 모델 호출 권한의 서로 다른 축이다. 이 함수는 현재 정책을 한 번
    읽고, 후보마다 그 정책과 정확히 결속된 active clearance를 조회한 뒤에만 원본을
    연다. :func:`run_approved_batch`와 저장소가 예약 시 같은 사실을 다시 검사하므로,
    중간에 정책이나 승인이 바뀌면 호출은 닫히는 쪽으로 끝난다.
    """
    policy, _config, _sources = store.effective_policy(workspace)

    def approval_id_for(candidate) -> str:
        return store.active_approval_id(
            workspace=candidate.workspace,
            channel=candidate.channel_id,
            file_id=candidate.file_id,
            original_sha256=candidate.original_sha256,
            provider=policy.provider,
            model=policy.model,
            policy_revision=policy.revision,
            prompt_revision=PROMPT_REVISION,
        )

    def checks_for(candidate, doc) -> dict:
        if policy.enabled is not True or policy.workspace_enabled is not True:
            return {
                "access_verified": False,
                "pii_blocked": True,
                "bot_generated": False,
                "clearance_code": "image_description_disabled",
            }
        assessment = store.active_clearance(
            workspace=candidate.workspace,
            channel_id=candidate.channel_id,
            file_id=candidate.file_id,
            original_sha256=candidate.original_sha256,
            provider=policy.provider,
            model=policy.model,
            policy_revision=policy.revision,
        )
        clearance = clear_for_dispatch(
            candidate,
            doc=doc,
            assessment=assessment,
            provider=policy.provider,
            model=policy.model,
            policy_revision=policy.revision,
            now=now,
        )
        return clearance.as_checks() | {
            "bot_generated": False,
            "clearance_code": clearance.code,
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
        approval_id_for=approval_id_for,
        checks_for=checks_for,
        dispatch_expires_at=dispatch_expires_at,
        limit=limit,
    )


__all__ = [
    "ImageDescriptionBatch",
    "ImageDescriptionBatchItem",
    "run_approved_batch",
    "run_cleared_batch",
]
