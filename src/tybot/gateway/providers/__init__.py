"""프로바이더 어댑터. SDK는 각 어댑터에서 lazy import."""
from __future__ import annotations

from ..base import Provider


def build_default_providers() -> dict[str, Provider]:
    """실 프로바이더를 구성한다. 해당 SDK/키가 있어야 실제 호출 가능."""
    from .anthropic_provider import AnthropicProvider
    from .openai_provider import OpenAIProvider

    return {
        "anthropic": AnthropicProvider(),
        "openai": OpenAIProvider(),
    }
