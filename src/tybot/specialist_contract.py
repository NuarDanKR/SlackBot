"""Narrow execution contract between TYBot and read-only specialist bots."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Protocol


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

    def __post_init__(self) -> None:
        if not self.question.strip() or not self.evidence:
            raise ContractViolation("질문과 권한 필터를 통과한 근거가 필요합니다.")
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
    except Exception:  # noqa: BLE001 - an adapter failure must not take down the master bot
        return SpecialistCallResult(fallback(), "fallback", "adapter-error")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
