"""Fail-closed model and prompt contract for VIPBot answers.

The Slack runtime and the common answer engine are wired elsewhere.  This module
owns only the VIP-specific choices that must not silently fall back to TYBot's
defaults: model, prompt revision, output envelope, and daily cost ceiling.
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .answer import (
    ADVICE_PROMPT,
    INTERIM_ADVICE_PROMPT,
    SUMMARY_PROMPT,
    SYSTEM_PROMPT,
    AnswerPrompts,
)

PROFILE_REVISION = "vip-executive-v1"
APPROVED_MODEL = "claude-opus-5-5"
MAX_OUTPUT_TOKENS = 1800
COST_STATE_PATH = "/var/lib/tybot-vipbot/cost-state.json"

COST_LIMIT_UNCONFIGURED = "vip_cost_limit_unconfigured"
COST_LIMIT_INVALID = "vip_cost_limit_invalid"
MODEL_NOT_APPROVED = "vip_model_not_approved"

_MODEL_FLAG_RE = re.compile(r"--model=[A-Za-z0-9._\-]+")
_POSITIVE_DECIMAL_RE = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)

EXECUTIVE_APPENDIX = """

VIPBot 경영진 답변 규칙:
1. 첫 문장은 결론과 현재 상태를 한 문장으로 요약한다(BLUF).
2. 사실에는 원문에 적힌 날짜와 담당 조직을 함께 적는다. 없으면 없다고 밝힌다.
3. 원문 사실과 분석을 분리한다. 분석은 재무적 영향, 운영 리스크, 대안 비교 관점으로
   작성하되 원문에 없는 금액·확률·일정·인과관계를 만들어 내지 않는다.
4. 의사결정이 필요한 질문은 가능한 대안 2~3개와 각 장단점, 추가 확인사항을 제시한다.
5. 자료가 일부 범위뿐이면 전체 현황처럼 쓰지 않고 확인한 범위와 미확인 범위를 밝힌다.
6. 사람의 평가·의견은 작성자와 날짜를 붙이고 VIPBot 자신의 판단과 구분한다.
7. 출처 줄은 시스템이 원문 좌표로 붙인다. 출처를 지어내거나 답변 안에 새로 만들지 않는다.
""".strip()


class VipProfileError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VipProfile:
    revision: str
    model: str
    daily_limit_usd: float
    cost_state_path: str
    max_output_tokens: int
    prompts: AnswerPrompts


def _with_executive_contract(base: str) -> str:
    return f"{base.rstrip()}\n\n{EXECUTIVE_APPENDIX}"


def load_profile(environ: Mapping[str, str] | None = None) -> VipProfile:
    """Load a VIP profile only when its separate daily budget is explicit."""
    values = os.environ if environ is None else environ
    model = str(values.get("VIPBOT_MODEL", APPROVED_MODEL) or "").strip()
    if model != APPROVED_MODEL:
        raise VipProfileError(MODEL_NOT_APPROVED)

    raw_limit = str(values.get("VIPBOT_DAILY_LIMIT_USD", "") or "").strip()
    if not raw_limit:
        raise VipProfileError(COST_LIMIT_UNCONFIGURED)
    if _POSITIVE_DECIMAL_RE.fullmatch(raw_limit) is None:
        raise VipProfileError(COST_LIMIT_INVALID)
    try:
        daily_limit = float(raw_limit)
    except ValueError:
        raise VipProfileError(COST_LIMIT_INVALID) from None
    if not math.isfinite(daily_limit) or daily_limit <= 0:
        raise VipProfileError(COST_LIMIT_INVALID)

    return VipProfile(
        revision=PROFILE_REVISION,
        model=APPROVED_MODEL,
        daily_limit_usd=daily_limit,
        cost_state_path=COST_STATE_PATH,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        prompts=AnswerPrompts(
            answer=_with_executive_contract(SYSTEM_PROMPT),
            summary=_with_executive_contract(SUMMARY_PROMPT),
            advice=_with_executive_contract(ADVICE_PROMPT),
            interim_advice=_with_executive_contract(INTERIM_ADVICE_PROMPT),
        ),
    )


def question_for_engine(question: str) -> str:
    """Remove every user model override; the reviewed VIP profile owns routing."""
    return _MODEL_FLAG_RE.sub("", str(question or "")).strip()


__all__ = [
    "APPROVED_MODEL",
    "COST_LIMIT_INVALID",
    "COST_LIMIT_UNCONFIGURED",
    "COST_STATE_PATH",
    "EXECUTIVE_APPENDIX",
    "MAX_OUTPUT_TOKENS",
    "MODEL_NOT_APPROVED",
    "PROFILE_REVISION",
    "VipProfile",
    "VipProfileError",
    "load_profile",
    "question_for_engine",
]
