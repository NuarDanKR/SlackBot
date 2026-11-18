"""Vision 전송 민감도 계약 — B-81(단독 이미지)과 B-80(PDF 페이지)이 **같은 규칙**을 쓴다.

## 오너 결정 (2026-11-19)

당사는 일반 업무 문서를 별도 기밀로 분류하지 않는다. 그래서 일반 업무 이미지·PDF 는
Google Vision 전송을 허용하고, **정책과 모델 카탈로그의 민감도 표기가 다르다는 이유만으로
막지 않는다.**

전에는 정책이 `internal` 이고 카탈로그가 `public` 이면 `sensitivity_not_allowed` 로 막혔다.
두 표기는 같은 「일반 업무」 를 다른 말로 적은 것뿐이었는데, 사람이 둘을 맞추지 않으면
기능 전체가 조용히 멈췄다.

## 계약

| 표기 | 단계 | Google Vision |
|---|---|---|
| `public` · `internal` | 일반 업무(`GENERAL_BUSINESS`) | 허용 |
| `confidential` | 기밀 | 카탈로그가 기밀까지 승인한 경우만 |

- 정책·카탈로그·Router 에 들어가는 값은 모두 `normalize()` 를 거친다. 그래서 정책이 public 이든
  internal 이든 Router 에는 같은 `internal` 이 가고, 운영 Router 의 모델 상한도 같은 값이다
- **더 넓게 올리지 않는다.** 일반 업무가 기밀로 바뀌는 일도, 기밀이 일반으로 내려가는 일도 없다

## 완화하지 않는 것

이 계약은 **표기 불일치**만 푼다. 다음은 그대로다.
- PII·민감 문서 차단(`pii_blocked`·`pii_screen`·문서 자체가 민감물인 격리)
- OCR 이 불완전하면 안전하다고 추정하지 않는다
- 원본 불변, LLM 결과는 파생 전사
- 같은 원본·페이지 재호출·재청구 금지(원장 identity UNIQUE)
- provider 오류 뒤 자동 재시도 금지
"""
from __future__ import annotations

from tybot.gateway.base import Sensitivity

#: 일반 업무 문서의 민감도. public 과 internal 은 이 하나로 모인다.
GENERAL_BUSINESS = Sensitivity.INTERNAL

#: 결정 기록(설계·BACKLOG 와 대조용).
DECISION_DATE = "2026-11-19"


def normalize(value: Sensitivity) -> Sensitivity:
    """표기 → 계약 단계. 열거형이 아니면 `ValueError` — 문자열을 짐작하지 않는다."""
    if not isinstance(value, Sensitivity):
        raise ValueError("sensitivity_invalid")
    if value in (Sensitivity.PUBLIC, Sensitivity.INTERNAL):
        return GENERAL_BUSINESS
    return Sensitivity.CONFIDENTIAL


def catalog_allows(catalog_max: Sensitivity, policy: Sensitivity) -> bool:
    """카탈로그가 이 정책의 문서를 받을 수 있는가. 같은 계약 단계 안이면 참."""
    return normalize(policy).rank() <= normalize(catalog_max).rank()


def request_sensitivity(policy: Sensitivity) -> Sensitivity:
    """Router·provider 경계에 넘길 값. 운영 Router 의 모델 상한도 이 값을 쓴다."""
    return normalize(policy)


__all__ = [
    "DECISION_DATE",
    "GENERAL_BUSINESS",
    "catalog_allows",
    "normalize",
    "request_sensitivity",
]
