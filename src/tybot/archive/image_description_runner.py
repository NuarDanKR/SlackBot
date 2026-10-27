"""승인 하나를 **끝까지** 돌린다. 조각들을 잇는 자리다.

## 왜 따로 있나

조각은 다 있는데 잇는 코드가 없어 파이프라인이 한 번도 끝에서 끝까지 돌지
않았다. 이 모듈이 그 선을 긋는다.

```
후보 선별 → 원본 바이트 → 예약 → dispatch → 모델 1회 → 게이트 → 파생 색인
(source)    (original)   (store.reserve) (worker)   (guard)   (revision)
```

**새 규칙을 만들지 않는다.** 각 조각이 이미 가진 거절을 **순서대로 지나게**
할 뿐이고, 어느 단계에서 멈췄는지를 코드로 남긴다.

## 승인 하나당 사진 하나

`ImageDescriptionApproval` 은 `file_id` 와 `original_sha256` 을 들고 있다 —
**사진 한 장에 대한 승인**이다. 그래서 이 러너도 한 장만 돌린다. 여러 장을
한 승인으로 돌리면 사람이 승인한 범위보다 넓게 나간다(원칙 3).

배치는 호출자가 승인 목록을 돌며 이 함수를 반복해 부르는 것이다.

## 안전은 우리가 정하지 않는다

| 사실 | 누가 아나 |
|---|---|
| 승인된 그 바이트인가 | **우리** — 로더가 해시를 다시 잰다 |
| 봇이 만든 것인가 | **우리** — 정본 `human_upload_confirmed` |
| 지금 ACL 로 볼 수 있나 | **호출자** — 권한은 이 모듈 밖이다 |
| 개인정보가 있나 | **호출자** — 글자 검사로는 사진을 못 본다 |

앞 둘은 로더가 증명한 뒤에만 참으로 적는다. 뒤 둘은 **호출자가 명시적으로
말해야** 하고, 말하지 않으면 `preflight` 의 기본값(`access_verified=False`,
`pii_blocked=True`)이 그대로 남아 호출 전에 닫힌다.

`pii_blocked=False` 를 이 모듈이 스스로 적는 일은 **없다.**

## 모델·DB·네트워크

모델은 worker 가 한 번 부른다. DB 는 넘겨받은 store 가 쓴다. 이 모듈은 둘 다
직접 부르지 않는다 — 부르면 거절 순서를 건너뛸 자리가 생긴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .image_description_original import load_original
from .image_description_worker import dispatch_reserved_image

logger = logging.getLogger(__name__)

#: 어느 단계에서 멈췄나. **사유 코드와 따로** 센다.
STAGE_LOAD = "load_original"
STAGE_RESERVE = "reserve"
STAGE_DISPATCH = "dispatch"
STAGE_DONE = "done"

STAGES = (STAGE_LOAD, STAGE_RESERVE, STAGE_DISPATCH, STAGE_DONE)

#: 호출자가 말해 줘야 하는 두 가지. **우리가 못 아는 것들이다.**
CALLER_CHECKS = ("access_verified", "pii_blocked")


@dataclass(frozen=True)
class RunOutcome:
    """한 장의 결말. **본문을 담지 않는다.**

    `revision_id` 가 있으면 파생 색인에 줄이 생겼다는 뜻이다. 비어 있어도
    실패가 아닐 수 있다 — 게이트가 막았거나 비용이 붙은 경우다(`stage=done`).
    """

    workspace: str
    channel_id: str
    file_id: str
    stage: str
    error_code: str = ""
    job_id: str = ""
    revision_id: str = ""
    #: worker 가 돌려준 원장 상태(`published`·`closed_without_revision`·`blocked`).
    state: str = ""

    @property
    def dispatched(self) -> bool:
        """모델이 실제로 불렸나. **성공 여부가 아니다.**"""
        return self.stage == STAGE_DONE


def _checks(caller: dict | None) -> dict:
    """호출자가 말한 것 + 로더가 증명한 것.

    `source_verified` 와 `bot_generated` 는 여기서 **참으로 고정한다** — 로더가
    해시를 다시 재고 `human_upload_confirmed` 를 확인한 뒤에만 이 자리에
    도달하기 때문이다.

    나머지는 호출자 말 그대로다. **빠지면 넣어 주지 않는다** — 기본값이 닫혀
    있고, 여기서 메워 주면 그 기본값이 뜻을 잃는다.
    """
    said = dict(caller or {})
    unknown = set(said) - set(CALLER_CHECKS)
    if unknown:
        # `pii_blocked=False` 를 호출자가 아닌 곳에서 받는 길을 막는다.
        raise ValueError(f"unexpected check: {sorted(unknown)}")
    return said | {"source_verified": True, "bot_generated": False}


def run_approved_image(
    candidate,
    *,
    store,
    router,
    archive_root,
    doc,
    approval_id: str,
    prompt_revision: str,
    expires_at: datetime,
    max_image_bytes: int,
    checks: dict | None = None,
) -> RunOutcome:
    """후보 한 장을 승인 범위 안에서 끝까지 돌린다.

    `checks` 는 **호출자만 아는 것**(`access_verified`·`pii_blocked`)이다.
    다른 키를 넣으면 `ValueError` — `pii_blocked=False` 가 호출자 아닌 곳에서
    들어오는 길을 막는다.

    예외를 내지 않는다. 입력 계약 위반(`ValueError`)과 **DB 가 죽은 경우**만
    밖으로 나간다 — 뒤쪽은 worker 가 이미 `unknown` 으로 닫으려 시도한 뒤다.
    """
    verified = _checks(checks)

    loaded = load_original(
        candidate,
        archive_root=archive_root,
        doc=doc,
        max_image_bytes=max_image_bytes,
    )
    if not loaded.ok:
        return _stop(candidate, STAGE_LOAD, loaded.error_code)

    try:
        receipt = store.reserve(
            loaded.image,
            approval_id=approval_id,
            prompt_revision=prompt_revision,
            expires_at=expires_at,
            checks=verified,
        )
    except Exception as exc:  # noqa: BLE001 - 거절도 결말이다
        # **예외 본문을 담지 않는다.** 저장소 거절은 코드 하나를 들고 오지만
        # 그 밖의 예외는 질의문·행 값을 끌고 올 수 있다.
        return _stop(candidate, STAGE_RESERVE, _code(exc))

    try:
        run = dispatch_reserved_image(
            store,
            receipt.dispatch_id,
            loaded.image,
            router=router,
            checks=verified,
        )
    except Exception as exc:  # noqa: BLE001
        # worker 가 `unknown` 으로 닫으려 시도한 뒤다. 못 닫았으면 작업은
        # `dispatching` 에 남고 **재전송되지 않는다**(identity UNIQUE).
        return _stop(candidate, STAGE_DISPATCH, _code(exc),
                     job_id=receipt.dispatch_id)

    outcome = RunOutcome(
        workspace=candidate.workspace,
        channel_id=candidate.channel_id,
        file_id=candidate.file_id,
        stage=STAGE_DONE,
        job_id=receipt.dispatch_id,
        revision_id=run.revision_id,
        state=run.state,
        error_code=run.code,
    )
    _log(outcome)
    return outcome


def _code(exc: Exception) -> str:
    """거절 코드 하나. **메시지를 그대로 쓰지 않는다.**

    `ImageDescriptionDenied` 는 코드만 들고 오도록 만들어진 예외라 그 문자열을
    쓴다. 그 밖의 예외는 **형만** 남긴다 — psycopg 오류는 `detail` 에 실패한
    행 전체를 담고, 거기에는 설명 본문이 들어 있다.
    """
    from .image_description_store import ImageDescriptionDenied

    if isinstance(exc, ImageDescriptionDenied):
        said = str(exc).strip()
        return said if said.replace("_", "").isalnum() and said.isascii() else "denied"
    return "store_error"


def _stop(candidate, stage: str, code: str, *, job_id: str = "") -> RunOutcome:
    outcome = RunOutcome(
        workspace=candidate.workspace,
        channel_id=candidate.channel_id,
        file_id=candidate.file_id,
        stage=stage,
        error_code=code,
        job_id=job_id,
    )
    _log(outcome)
    return outcome


def _log(outcome: RunOutcome) -> None:
    """**좌표와 단계·코드만.** 설명·바이트·경로는 넣지 않는다."""
    logger.info(
        "image_description_run ws=%s ch=%s file=%s stage=%s state=%s code=%s "
        "job=%s revision=%s",
        outcome.workspace, outcome.channel_id, outcome.file_id,
        outcome.stage, outcome.state or "-", outcome.error_code or "-",
        outcome.job_id or "-", outcome.revision_id or "-",
    )


__all__ = [
    "CALLER_CHECKS",
    "STAGES",
    "STAGE_DISPATCH",
    "STAGE_DONE",
    "STAGE_LOAD",
    "STAGE_RESERVE",
    "RunOutcome",
    "run_approved_image",
]
