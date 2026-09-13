"""Narrow execution contract between TYBot and read-only specialist bots."""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("tybot.specialist_contract")

# 실패 이유를 **한 덩어리(`adapter-error`)로 뭉개지 않는다.**
#
# 2026-09-08 실측: 콘솔의 「최근 호출」 에 `마스터 폴백 / adapter-error` 만 떴고,
# 그것이 「모델 이름이 레지스트리에 없다」 인지 「프로바이더 키가 없다」 인지
# 「모델이 화났다」 인지 알 방법이 없었다. 로그에도 한 줄이 없었다 —
# 예외를 잡아 폴백만 하고 아무것도 남기지 않았기 때문이다.
#
# 이 셋은 사람이 할 일이 서로 완전히 다르다: 모델 이름을 고친다 / 키를 넣는다 /
# 다시 시도한다.
_ERROR_CODES: dict[str, str] = {
    "UnknownModel": "unknown-model",
    "ModelNotAllowed": "model-not-allowed",
    "CostLimitExceeded": "cost-limit",
}


def error_code_for(exc: BaseException) -> str:
    """예외 → 짧은 코드. 모르는 것은 `adapter-error` 로 남긴다."""
    return _ERROR_CODES.get(type(exc).__name__, "adapter-error")


def _reason(exc: BaseException) -> str:
    """로그에 남길 한 줄. 게이트웨이 헬퍼를 쓰되 없어도 돌아간다.

    계약 모듈은 게이트웨이에 의존하지 않는 것이 원칙이라 늦게 가져온다 —
    전문가가 HTTP 전송으로 바뀌어도 이 모듈은 그대로여야 한다.
    """
    try:
        from .gateway.base import error_reason

        return error_reason(exc)
    except Exception:  # noqa: BLE001 - 로그 문구가 답변을 막으면 안 된다
        return f"{type(exc).__name__}: {str(exc)[:200]}"


class ContractViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizedEvidence:
    """Evidence that the master bot has already filtered for one request scope."""

    workspace: str
    text: str
    authorization_id: str

    @classmethod
    def from_acl_filter(cls, *, workspace: str, text: str, authorization_id: str):
        if not workspace.strip() or not authorization_id.strip():
            raise ContractViolation("권한 판정 식별자와 워크스페이스가 필요합니다.")
        if not text.strip():
            raise ContractViolation("비어 있는 근거는 전문 봇에 전달하지 않습니다.")
        return cls(workspace.strip(), text, authorization_id.strip())


@dataclass(frozen=True)
class SpecialistRequest:
    question: str
    evidence: tuple[AuthorizedEvidence, ...]
    # 도구형 전문 봇은 **스스로 찾는다.** 마스터 검색이 0건이라고 부르지 못하게
    # 하면 그 봇의 값이 통째로 사라진다.
    #
    # 예전에는 "(마스터 검색 결과 없음)" 이라는 **가짜 근거 한 줄**을 만들어 이
    # 검사를 통과시켰다. 근거가 아닌 것을 근거 자리에 두면 그 자리를 더 이상
    # 믿을 수 없게 된다 — 빈 것은 빈 채로 두고, 빈 것을 허용한다고 밝힌다.
    allow_empty_evidence: bool = False
    # 권한·OCR·PII 검사를 모두 통과한 **이미지 원본 블록**. Messages API 에 그대로
    # 넣을 모양이다.
    #
    # 예전에는 시각 근거가 있으면 전문 봇을 아예 건너뛰고 마스터가 이미지를 직접
    # 읽어 답했다(2026-09-13 검증). 이미지 PDF 와 첨부 사진이 많은 업무에서는
    # 그 길이 「업무 답변은 전문 봇만」 규칙의 가장 큰 구멍이었다.
    visual: tuple = ()

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ContractViolation("질문이 필요합니다.")
        if not self.evidence and not self.allow_empty_evidence:
            raise ContractViolation("질문과 권한 필터를 통과한 근거가 필요합니다.")
        if not self.evidence:
            return
        authorization_ids = {item.authorization_id for item in self.evidence}
        if len(authorization_ids) != 1:
            raise ContractViolation("서로 다른 권한 판정의 근거를 한 호출에 섞을 수 없습니다.")


class SpecialistAdapter(Protocol):
    def complete(self, request: SpecialistRequest) -> str: ...


@dataclass(frozen=True)
class SpecialistCallResult:
    text: str
    result: str
    error_code: str = ""


def _validated_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation("전문 봇이 비어 있거나 잘못된 응답을 반환했습니다.")
    text = value.strip()
    lowered = text.lower()
    if "출처:" in text or "slack.com/archives/" in lowered or "file://" in lowered:
        raise ContractViolation("출처는 마스터 봇만 부착할 수 있습니다.")
    if len(text) > 20_000:
        raise ContractViolation("전문 봇 응답 길이가 계약 범위를 초과했습니다.")
    return text


def execute(
    adapter: SpecialistAdapter,
    request: SpecialistRequest,
    *,
    fallback: Callable[[], str],
    timeout_seconds: float = 20,
    confidence: float = 1.0,
    minimum_confidence: float = 0.6,
) -> SpecialistCallResult:
    """Run one specialist and fall back without granting storage or ACL capabilities."""
    if confidence < minimum_confidence:
        return SpecialistCallResult(fallback(), "fallback", "low-confidence")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tybot-specialist")
    future = pool.submit(adapter.complete, request)
    try:
        return SpecialistCallResult(_validated_text(future.result(timeout=timeout_seconds)), "success")
    except TimeoutError:
        future.cancel()
        return SpecialistCallResult(fallback(), "fallback", "timeout")
    except ContractViolation:
        return SpecialistCallResult(fallback(), "contract_violation", "invalid-output")
    except Exception as exc:  # noqa: BLE001 - an adapter failure must not take down the master bot
        # **반드시 남긴다.** 이 줄이 없어서 콘솔의 `adapter-error` 가 원인을 하나도
        # 말하지 못했다. 예외 메시지에 근거 본문은 들어가지 않는다(모델 이름·
        # 프로바이더·상태 코드뿐).
        log.warning(
            "전문가 호출 실패 code=%s %s",
            error_code_for(exc), _reason(exc),
        )
        return SpecialistCallResult(fallback(), "fallback", error_code_for(exc))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
