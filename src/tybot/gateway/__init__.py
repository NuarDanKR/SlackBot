"""LLM 게이트웨이 — 모든 LLM 호출의 단일 진입점.

모델 선택 · 민감도별 라우팅 · 비용 가드 · 사용 모델/토큰/비용 로깅.
프로바이더 SDK는 lazy import 되므로 이 패키지 import 만으로는 SDK가 필요 없다.
"""
from .base import LLMResponse, Message, ModelSpec, Provider, Sensitivity
from .cost import CostGuard, CostLimitExceeded
from .router import ModelNotAllowed, Router, UnknownModel

__all__ = [
    "CostGuard",
    "CostLimitExceeded",
    "LLMResponse",
    "Message",
    "ModelNotAllowed",
    "ModelSpec",
    "Provider",
    "Router",
    "Sensitivity",
    "UnknownModel",
]
