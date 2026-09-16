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


# 단계 이름. **`primary` 를 쓰지 않는다** — 그 이름이 탐색과 최종 합성을 함께
# 뜻해서, 콘솔에 `specialist-timeout:primary` 가 떠도 어디서 끝났는지 몰랐다.
DISCOVERY = "discovery"
FINALIZE = "finalize"
RECOVERY = "recovery"
TOTAL = "total"
STAGES = (DISCOVERY, FINALIZE, RECOVERY, TOTAL)


@dataclass(frozen=True)
class SpecialistDeadlines:
    """단계별 예산. **한곳에서 검증한다** — 상수로 흩어 두면 합이 안 맞는다.

    ## 왜 180초인가

    2026-09-16 운영: 같은 비교 질문이 13:03 에 **77.3초로 성공**했고 14:23·14:31 에
    74.5~76.2초로 `timeout` 이 났다. 75초 예산은 그 질문이 원래 쓰던 시간보다
    **짧았다** — 고친 것이 아니라 회귀였다.

    그래서 상한을 늘리되 **단계가 서로를 침범하지 못하게** 나눈다. 늘리기만 하면
    같은 광범위 검색이 더 오래 돌 뿐이고, p90 과 비용만 는다(§4 가 그쪽을 막는다).

    ```text
    discovery 100 + finalize 45 + recovery 20 + delivery_reserve 15 = total 180
    ```

    합이 정확히 맞아야 한다. 남으면 아무도 안 쓰는 시간이고, 모자라면 뒤 단계가
    시작도 못 한다.
    """

    total: float = 180.0
    # 추가 검색·문서 열람을 **새로 시작할 수 있는** 구간.
    discovery: float = 100.0
    # 이미 읽은 근거로 답을 만드는 무도구 호출 1회. **discovery 가 빌려 쓸 수 없다.**
    finalize: float = 45.0
    # 허용된 실패에서 다시 한 번. 여기까지가 전문 봇의 시간이다.
    recovery: float = 20.0
    # QA 기록·Canvas/Slack 전송. 전문 봇이 쓰면 답이 늦게 도착한다.
    delivery_reserve: float = 15.0
    # Provider 1회 상한. 단계 잔여와 비교해 **작은 쪽**을 쓴다.
    per_call: float = 45.0

    def __post_init__(self) -> None:
        values = (self.total, self.discovery, self.finalize, self.recovery,
                  self.delivery_reserve, self.per_call)
        if min(values) <= 0:
            raise ValueError("시간 예산은 모두 0보다 커야 합니다.")
        staged = self.discovery + self.finalize + self.recovery + self.delivery_reserve
        if staged != self.total:
            # 「대략 맞다」 로 두면 어느 단계가 모자란지 사고가 나야 안다.
            raise ValueError(
                f"단계 합({staged})이 total({self.total})과 다릅니다: "
                f"discovery {self.discovery} + finalize {self.finalize} + "
                f"recovery {self.recovery} + reserve {self.delivery_reserve}"
            )

    # --- 절대 경계 ------------------------------------------------------------
    #
    # 시작 시각에 더해서 쓴다. 단계가 바뀔 때 45초를 **새로 더하지 않는다** —
    # 그러면 단계 수만큼 시간이 늘어난다.
    @property
    def discovery_ends(self) -> float:
        return self.discovery

    @property
    def finalize_ends(self) -> float:
        return self.discovery + self.finalize

    @property
    def recovery_ends(self) -> float:
        return self.discovery + self.finalize + self.recovery

    @property
    def hard_ends(self) -> float:
        """전문 봇이 쓸 수 있는 마지막 순간. 전달 예약은 그 뒤다."""
        return self.recovery_ends


DEADLINES = SpecialistDeadlines()


@dataclass
class Deadline:
    """이 요청 하나의 시계. **재시도마다 새로 주지 않는다.**

    `time.monotonic()` 기준 시작 시각을 **한 번만** 만들고, adapter·Gateway·
    Provider·도구가 같은 절대 경계를 본다.
    """

    settings: SpecialistDeadlines = DEADLINES
    started: float = 0.0
    # 시간이 끝난 뒤 늦게 도착한 결과를 게시하지 않기 위한 표식.
    # 스레드를 죽일 수 없으니 **어댑터가 보고 스스로 멈춘다.**
    aborted: bool = False
    timeout_stage: str = ""
    # 지금 어느 단계인가. 복구 가능 여부를 **단계로** 판단한다 — `aborted` 하나만
    # 보면 discovery 에서 시간이 끝난 요청이 최종 합성 기회까지 잃는다.
    stage: str = DISCOVERY
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.started:
            self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    def _ends_at(self, stage: str) -> float:
        return {
            DISCOVERY: self.settings.discovery_ends,
            FINALIZE: self.settings.finalize_ends,
            RECOVERY: self.settings.recovery_ends,
            TOTAL: self.settings.hard_ends,
        }.get(stage, self.settings.hard_ends)

    def remaining(self, stage: str = TOTAL) -> float:
        """이 단계가 쓸 수 있는 남은 시간. **음수가 되지 않는다.**"""
        return max(0.0, self._ends_at(stage) - self.elapsed)

    def call_timeout(self, *, stage: str = DISCOVERY) -> float:
        """Provider 한 번에 줄 시간. `min(단계 잔여, per_call)`."""
        return max(0.0, min(self.remaining(stage), self.settings.per_call))

    def enter(self, stage: str) -> None:
        """단계를 옮긴다. 시간을 더하지 않는다 — 경계는 절대값이다."""
        self.stage = stage

    def require_time(self, stage: str, *, need: float = 0.0) -> None:
        """이 단계를 시작할 시간이 남았는가. 아니면 `StageTimeout`.

        **시작하기 전에 본다.** 시작하고 나서 보면 이미 쓴 시간은 돌아오지 않는다.
        """
        if self.expired:
            # **표식을 먼저 세운다.** 예전에는 여기서 바로 raise 해서, 전체 시간이
            # 끝난 요청이 `aborted=False` 로 남았다 — 늦게 온 결과를 버리는 검사가
            # 그 표식을 보는데 서 있지 않았다(테스트가 잡았다).
            self.abort(TOTAL)
            raise StageTimeout(self.timeout_stage or TOTAL, "이미 종료된 요청입니다.")
        if self.remaining(stage) <= need:
            self.abort(stage)
            raise StageTimeout(stage)

    @property
    def expired(self) -> bool:
        """요청이 끝났는가. **단계 timeout 과 다르다.**

        discovery 에서 시간이 끝난 것은 「탐색은 그만」 이지 「요청 종료」 가 아니다.
        이미 `abort(TOTAL)` 된 경우도 끝난 것으로 본다 — 안 그러면 바깥에서 닫은
        요청이 안쪽에서 계속 돈다.
        """
        return self.aborted or self.remaining(TOTAL) <= 0

    def abort(self, stage: str) -> None:
        """이 단계에서 끝났다고 표시한다. 늦게 온 결과는 쓰지 않는다."""
        if not self.timeout_stage:
            self.timeout_stage = stage
        # 전체가 끝난 경우에만 요청 자체를 닫는다. discovery 초과는 **전환**이다.
        if stage == TOTAL or self.expired:
            self.aborted = True

    def may_finalize(self) -> bool:
        """최종 합성을 시작할 시간이 남았는가."""
        return not self.aborted and self.remaining(FINALIZE) > 0

    def may_recover(self) -> bool:
        """복구 합성을 한 번 더 할 시간이 남았는가."""
        return not self.aborted and self.remaining(RECOVERY) > 0


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
    # 바깥 대기는 **전문 봇에게 허용된 마지막 순간**까지다. 전달 예약은 그 뒤라
    # 여기서 쓰지 않는다.
    wait = deadline.remaining(TOTAL) if request.deadline else timeout_seconds
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
        # 어느 단계에서 끝났는지 어댑터가 안다. 없으면 전체로 본다 —
        # `primary` 라는 모호한 이름은 더 만들지 않는다.
        stage = str(getattr(adapter, "phase", "") or deadline.stage or TOTAL)
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
