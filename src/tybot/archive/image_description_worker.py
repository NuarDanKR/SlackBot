"""예약된 B-81 이미지 설명 한 건을 정확히 한 번 실행한다.

큐 소비·원본 로딩·현재 ACL/PII 검증은 호출자가 맡는다. 이 함수는 영속 원장의
dispatch commit 뒤에만 모델을 부르고, 결과를 저장 전 안전 게이트로 보낸다.
자동 재시도나 유료 폴백은 없다.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from .image_description import ImageInput, ImageInputError, describe_image


class ImageDescriptionWorkerError(RuntimeError):
    """본문 없는 worker 오류. 원래 DB/provider 예외 문자열을 밖으로 내보내지 않는다."""

    def __init__(self, code: str, exception_type: str) -> None:
        self.code = code
        self.exception_type = exception_type
        super().__init__(f"{code}:{exception_type}")


@dataclass(frozen=True)
class ImageDescriptionRun:
    job_id: str
    state: str
    revision_id: str = ""
    code: str = ""


def dispatch_reserved_image(
    store,
    job_id: str,
    image: ImageInput,
    *,
    router,
    checks: dict,
) -> ImageDescriptionRun:
    """한 번 호출하고 결말을 원장에 쓴다. 미확정 전송은 다시 보내지 않는다."""
    grant = store.begin_dispatch(job_id, image, checks=checks)
    try:
        result = describe_image(
            image,
            router=router,
            model=grant.model,
            provider=grant.provider,
            max_output_tokens=grant.max_output_tokens,
            timeout_seconds=grant.timeout_seconds,
        )
    except ImageInputError:
        # restricted router 준비 실패 등 외부 호출 전 거절. 예외 본문은 저장하지 않는다.
        store.finish_without_call(job_id, code="call_preflight_rejected")
        return ImageDescriptionRun(
            job_id=job_id,
            state="blocked",
            code="call_preflight_rejected",
        )

    try:
        revision_id = store.finish_and_publish(job_id, result)
    except Exception as exc:  # noqa: BLE001 - 전송 뒤 모든 저장 실패는 unknown으로 닫는다
        # 모델을 불렀을 수 있다. 저장 실패를 재전송 가능 상태로 돌리면 중복 전송된다.
        with suppress(Exception):
            store.mark_unknown(job_id, code="publish_result_unknown")
        # DB 자체가 죽었으면 이 기록도 실패한다. 원래 예외를 보존하고 운영
        # reconciliation이 dispatching 행을 찾게 한다.
        raise ImageDescriptionWorkerError(
            "publish_failed",
            type(exc).__name__,
        ) from None
    return ImageDescriptionRun(
        job_id=job_id,
        state="published" if revision_id else "closed_without_revision",
        revision_id=revision_id,
    )
