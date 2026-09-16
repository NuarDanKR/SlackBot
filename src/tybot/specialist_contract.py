"""Narrow execution contract between TYBot and read-only specialist bots."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("tybot.specialist_contract")


# --- 시간 예산 (2026-09-16 장애) ----------------------------------------------
#
# 장애: 같은 비교 질문이 94.7초 `timeout` 과 88.8초 `invalid-output:empty` 로
# 실패했다. 근거 20건을 확보한 **뒤** Hermes 실행 단계에서 죽었다.
#
# 원인은 **상한이 계층마다 따로 놀았다**는 것이다.
#
# | 어디 | 상한 | 누가 보나 |
# |---|---|---|
# | `execute()` | 90초 | 바깥 스레드 |
# | 도구 루프 | 8라운드 | 어댑터 |
# | `ToolBudget` | 45초 | **도구 실행 직전에만** |
# | Provider | 없음 | 아무도 |
#
# 하나의 deadline 을 공유하지 않으니 느린 LLM 호출 하나가 도구 예산을 넘겨도
# 계속 돌고, 여러 라운드의 합이 90초를 넘었다. **90초를 늘리는 수정은 금지다** —
# 그건 장애를 숨긴다.
#
# 여기서는 **monotonic 기준 하나**를 만들어 task·adapter·Gateway·Provider·도구
# 루프가 같은 값을 본다. 벽시계(`datetime.now()`)를 쓰지 않는다 — NTP 보정 한 번에
# 남은 시간이 음수가 된다.
MAX_LIVE_CALLS = 8

# 취소할 수 없는 스레드를 요청마다 무제한 만들지 않는다. Python 은 실행 중인
# 스레드를 죽이지 못하므로, 상한이 없으면 timeout 이 쌓일수록 살아 있는
# Provider 호출과 비용이 함께 는다.
_workers = threading.BoundedSemaphore(MAX_LIVE_CALLS)


class StageTimeout(RuntimeError):
    """단계 시간이 끝났다. **계약 위반이 아니다** — 우리가 멈춘 것이다."""

    def __init__(self, stage: str, message: str = "") -> None:
        super().__init__(message or f"{stage} 단계 시간이 끝났습니다.")
        self.stage = stage


@dataclass(frozen=True)
class SpecialistDeadlines:
    """단계별 예산. **한곳에서 검증한다** — 상수로 흩어 두면 합이 안 맞는다.

    기본값 근거(설계 §4): 사용자 대기 상한 75초를 primary 탐색·recovery 최종화·
    전달 예약으로 나눈다. 늘려서 장애를 숨기지 않는다.
    """

    total: float = 75.0
    # primary 가 끝나야 하는 시점. 이 뒤로는 **새 검색을 시작하지 않는다.**
    primary: float = 55.0
    # Provider 1회 상한. 남은 시간과 비교해 **작은 쪽**을 쓴다.
    per_call: float = 25.0
    # 무도구 최종화 1회에 남겨 두는 시간.
    recovery: float = 15.0
    # QA 기록과 Slack 전달. 여기까지 먹으면 답이 늦게 도착한다.
    reserve: float = 5.0

    def __post_init__(self) -> None:
        if min(self.total, self.primary, self.per_call, self.recovery, self.reserve) <= 0:
            raise ValueError("시간 예산은 모두 0보다 커야 합니다.")
        if self.primary + self.recovery + self.reserve > self.total:
            # 합이 넘으면 recovery 가 시작도 못 하고 끝난다 — 있으나 마나 한 단계가
            # 되고, 그건 「복구가 안 된다」 로 보인다.
            raise ValueError(
                f"primary+recovery+reserve({self.primary + self.recovery + self.reserve})"
                f" 가 total({self.total}) 을 넘습니다."
            )


DEADLINES = SpecialistDeadlines()


@dataclass
class Deadline:
    """이 요청 하나의 시계. **재시도마다 새로 주지 않는다.**"""

    settings: SpecialistDeadlines = DEADLINES
    started: float = 0.0
    # 시간이 끝난 뒤 늦게 도착한 결과를 게시하지 않기 위한 표식.
    # 스레드를 죽일 수 없으니 **어댑터가 보고 스스로 멈춘다.**
    aborted: bool = False
    timeout_stage: str = ""
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.started:
            self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    def remaining(self, *, reserve: bool = True) -> float:
        """남은 시간. **음수가 되지 않는다.**

        `reserve` 면 전달 예약을 뺀 값이다 — 그 시간을 쓰면 답이 늦게 도착한다.
        """
        budget = self.settings.total - (self.settings.reserve if reserve else 0.0)
        return max(0.0, budget - self.elapsed)

    def remaining_primary(self) -> float:
        """primary 가 쓸 수 있는 남은 시간. 경계를 넘으면 0 이다."""
        return max(0.0, self.settings.primary - self.elapsed)

    def call_timeout(self, *, stage: str = "primary") -> float:
        """Provider 한 번에 줄 시간. `min(남은 시간, per_call)`."""
        left = self.remaining_primary() if stage == "primary" else self.remaining()
        return max(0.0, min(left, self.settings.per_call))

    def require_time(self, stage: str, *, need: float = 0.0) -> None:
        """이 단계를 시작할 시간이 남았는가. 아니면 `StageTimeout`.

        **시작하기 전에 본다.** 시작하고 나서 보면 이미 쓴 시간은 돌아오지 않는다.
        """
        if self.aborted:
            raise StageTimeout(self.timeout_stage or stage, "이미 종료된 요청입니다.")
        left = self.remaining_primary() if stage == "primary" else self.remaining()
        if left <= need:
            self.abort(stage)
            raise StageTimeout(stage)

    def abort(self, stage: str) -> None:
        """더 진행하지 않는다. 늦게 온 결과도 쓰지 않는다."""
        if not self.aborted:
            self.aborted = True
            self.timeout_stage = stage

    def may_recover(self) -> bool:
        """무도구 최종화를 한 번 더 할 시간이 남았는가."""
        return self.remaining() >= min(self.settings.recovery, self.settings.per_call)

# 본문 상한. **프롬프트에 적는 값과 같은 값이다**(`specialist_adapters`).
#
# 갈리면 「부탁은 3,000, 검사는 20,000」 이 되어 전문 봇이 상한을 지킬 이유가
# 없어진다. 지키는지 보는 쪽과 지키라고 말하는 쪽이 같은 숫자를 봐야 한다.
#
# **여기를 임의로 올리지 않는다**(B-52). 3,000 자를 넘어야 하는 답은 Hermes 를
# 늘리는 것이 아니라 보고서 전문 봇(`clio`)이 맡는다. 상한을 올리면 그 분리가
# 조용히 없어지고, 모든 질문이 Hermes 의 장문으로 흘러간다.
#
# 2026-09-16 사고: 상한을 20,000 → 3,000 으로 내린 직후 "현장 간 비교 분석해
# 자세히 설명해줘" 가 실패했다. 원인은 상한 자체가 아니라 **최초 요청에서 상한을
# 약하게 말한 것**이었다 — 사용자의 "자세히" 가 배경 정책 한 줄을 이겼다.
# 고친 자리는 프롬프트다(`MASTER_OUTPUT_POLICY`).
TARGET_OUTPUT_CHARS = 3_000
MAX_OUTPUT_CHARS = 3_000

# 계약 위반 사유. **한 덩어리로 뭉개지 않는다** — 빈 응답·출처 포함·폭주는
# 사람이 할 일이 서로 다르다.
VIOLATION_EMPTY = "invalid-output:empty"
VIOLATION_MAX_TOKENS = "invalid-output:max-tokens"
VIOLATION_SOURCES = "invalid-output:sources"
VIOLATION_TOO_LONG = "invalid-output:too-long"

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
    "ProviderTimeout": "provider-timeout",
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
    editing_text: str = ""
    # 이 요청 하나의 시계. **어댑터·Gateway·도구 루프가 같은 값을 본다**(장애 §2.1).
    # 없으면 `execute()` 가 기본 예산으로 하나 만든다.
    deadline: Deadline | None = None
    # **표시 힌트**. 「Markdown 표로 답하라」 같은 형식 안내뿐이다(설계 §3.3).
    #
    # Canvas 를 만들라는 **동작 요청은 여기 오지 않는다.** 전문 봇에 그걸 보내면
    # "Canvas 편집은 내 역할이 아니다" 라는 실행 거절이 돌아오고, 근거 추출이
    # 성공했는데도 답이 실패로 끝난다. 생성·공유는 호출자 몫이다.
    display_hint: str = ""

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
    # 성공한 답의 길이. **버리지 않고 재는 값이다** — 목표를 얼마나 넘는지 보여야
    # 천장을 조일지 프롬프트를 고칠지 판단할 수 있다. 0 이면 재지 못했다.
    output_chars: int = 0

    @property
    def over_target(self) -> bool:
        """본문 상한을 넘었는가. 통과한 답은 언제나 `False` 다."""
        return self.output_chars > TARGET_OUTPUT_CHARS


class OutputViolation(ContractViolation):
    """어느 규칙이 깨졌는지 들고 다닌다. 본문은 담지 않는다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validated_text(value: object, *, stop_reason: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        # **왜 비었는지**를 코드로 가른다(장애 §2.3). `max_tokens` 에서 끊긴 것과
        # thinking 블록만 돌아온 것은 사람이 할 일이 다르다 — 앞은 토큰을
        # 늘리거나 짧게 쓰게 하고, 뒤는 Provider·모델을 본다.
        if stop_reason == "max_tokens":
            raise OutputViolation(
                VIOLATION_MAX_TOKENS,
                "전문 봇이 토큰 상한에서 끊겨 본문을 남기지 못했습니다.",
            )
        raise OutputViolation(
            VIOLATION_EMPTY,
            f"전문 봇이 비어 있는 응답을 반환했습니다(stop_reason={stop_reason or '-'}).",
        )
    text = value.strip()
    lowered = text.lower()
    if "출처:" in text or "slack.com/archives/" in lowered or "file://" in lowered:
        raise OutputViolation(VIOLATION_SOURCES, "출처는 마스터 봇만 부착할 수 있습니다.")
    if len(text) > MAX_OUTPUT_CHARS:
        # 길이 숫자는 업무 내용이 아니다. **적어 둬야** 프롬프트를 고칠지
        # 상한을 볼지 판단할 수 있다. 예전에는 「초과」 만 남아서 3,050 자인지
        # 9,000 자인지 알 수 없었다.
        raise OutputViolation(
            VIOLATION_TOO_LONG,
            f"전문 봇 응답이 본문 상한을 넘었습니다({len(text)} > {MAX_OUTPUT_CHARS}).",
        )
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
    deadline = request.deadline or Deadline()
    # 살아 있는 호출 수를 막는다. 취소할 수 없는 스레드를 요청마다 무제한 만들면
    # timeout 이 쌓일수록 Provider 호출과 비용이 함께 는다(장애 §2.2).
    if not _workers.acquire(blocking=False):
        log.warning("전문 봇 동시 호출 상한(%d)에 걸렸습니다", MAX_LIVE_CALLS)
        return SpecialistCallResult(fallback(), "fallback", "specialist-busy")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tybot-specialist")
    future = pool.submit(adapter.complete, request)
    # 바깥 대기는 **남은 시간**이다. 고정 90초를 쓰면 안쪽 예산과 따로 논다.
    wait = deadline.remaining() if request.deadline else timeout_seconds
    try:
        # **바닥값을 두지 않는다.** 남은 시간이 0 이면 지금 시작하면 안 된다는
        # 뜻이고, `future.result(timeout=0)` 은 그 자리에서 timeout 이다.
        raw = future.result(timeout=wait)
        text = _validated_text(raw, stop_reason=getattr(adapter, "last_stop_reason", ""))
        if deadline.aborted:
            # 시간이 끝난 뒤 도착했다. **게시하지 않는다** — 사용자는 이미 실패를
            # 받았고, 여기서 또 보내면 답이 두 번 간다.
            log.warning("시간이 끝난 뒤 도착한 전문 봇 결과를 버립니다")
            return SpecialistCallResult(
                fallback(), "fallback", f"specialist-timeout:{deadline.timeout_stage}"
            )
        return SpecialistCallResult(text, "success", output_chars=len(text))
    except StageTimeout as exc:
        # 어댑터가 **스스로 멈췄다.** Provider 호출도 이미 끝났다는 뜻이라,
        # 이 뒤에 살아 남는 작업이 없다.
        return SpecialistCallResult(
            fallback(), "fallback", f"specialist-timeout:{exc.stage}"
        )
    except TimeoutError as exc:
        # `ProviderTimeout` 은 표준 `TimeoutError` 의 하위 타입이다. 바깥
        # `future.result()` 대기 만료와 먼저 구별하지 않으면 SDK timeout까지
        # `specialist-timeout:primary` 로 잘못 기록된다.
        if error_code_for(exc) == "provider-timeout":
            return SpecialistCallResult(fallback(), "fallback", "provider-timeout")
        # 바깥에서 시간이 끝났다. 스레드는 못 죽이지만 **표식을 세워** 어댑터가
        # 다음 확인 지점에서 멈추게 한다. `future.cancel()` 만으로는 이미 실행
        # 중인 스레드가 안 멈춘다(장애 §2.2).
        stage = str(getattr(adapter, "phase", "") or "primary")
        deadline.abort(stage)
        future.cancel()
        return SpecialistCallResult(
            fallback(), "fallback", f"specialist-timeout:{stage}"
        )
    except ContractViolation as exc:
        # 응답 본문은 남기지 않는다. 다만 아래 사유는 모두 우리가 만든 고정
        # 검증 문구라서 업무 내용이나 모델 출력이 로그로 새지 않는다. 예전에는
        # 모든 위반을 `invalid-output` 하나로 접어 운영에서 빈 응답/출처 포함/
        # 길이 초과를 구별할 방법이 없었다.
        log.warning("전문 봇 응답 계약 위반: %s", exc)
        return SpecialistCallResult(
            fallback(), "contract_violation",
            getattr(exc, "code", "") or "invalid-output",
        )
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
        # **자리를 반드시 돌려준다.** 안 돌려주면 상한이 한 번씩 줄어들어
        # 결국 모든 질문이 `specialist-busy` 가 된다.
        _release_when_done(future)


def _release_when_done(future) -> None:
    """작업이 끝나면 동시 호출 자리를 돌려준다.

    `add_done_callback` 은 **이미 끝난 작업이면 즉시** 부른다. 그래서 정상 종료와
    timeout 뒤 늦은 종료 양쪽에서 한 번씩만 풀린다.
    """
    def _done(_fut) -> None:
        try:
            _workers.release()
        except ValueError:  # pragma: no cover - 두 번 풀리면 여기서 멈춘다
            log.warning("전문 봇 동시 호출 자리를 두 번 반납했습니다")

    future.add_done_callback(_done)
