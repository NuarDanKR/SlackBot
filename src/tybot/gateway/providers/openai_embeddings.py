"""OpenAI embedding adapter.  SDK is imported lazily, inside the call.

This file does not pick a model.  The model name and its dimension come from
the index model registry, which an operator fills in; this adapter only sends
what it is handed and reports what came back.

Nothing wires this adapter in by default -- see ``build_embedding_providers``
below.  It is reachable only once a model revision in the registry names the
``openai`` provider and a workspace policy approves that revision.
"""
from __future__ import annotations

import os
from collections.abc import Sequence

from ..base import ProviderTimeout
from ..embedding import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingResultInvalid,
    ProviderEmbeddings,
    ordered_vectors,
)
from ._timeout import is_timeout as _is_timeout


def _resolve_key() -> str | None:
    """DB first, environment second -- the same order the completion adapter uses."""
    try:
        from ...console.llm_secret_store import resolve_key

        return resolve_key("openai")
    except Exception:  # noqa: BLE001 - a key lookup must not break the caller
        return os.getenv("OPENAI_API_KEY")


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or _resolve_key()
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai  # lazy
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise RuntimeError("openai SDK missing: pip install openai") from exc
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    def embed(
        self,
        model: EmbeddingModel,
        texts: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderEmbeddings:
        model.checked()
        bodies = list(texts)
        if not bodies:
            raise EmbeddingResultInvalid("embedding_request_empty")
        if timeout_seconds is not None and timeout_seconds <= 0:
            # No budget left.  Calling anyway spends money on a result the
            # caller has already stopped waiting for.
            raise ProviderTimeout("embedding call had no time budget left")
        client = self._get_client()
        request: dict = {"model": model.model, "input": bodies}
        if timeout_seconds is not None:
            request["timeout"] = timeout_seconds
        try:
            response = client.embeddings.create(**request)
        except Exception as exc:
            if _is_timeout(exc):
                # Fixed message, and the cause is dropped on purpose: a 400 from
                # an embedding endpoint can echo the input back, and this text
                # is archive source material.
                raise ProviderTimeout("embedding call timed out") from None
            raise

        rows = [
            (getattr(item, "index", position), getattr(item, "embedding", ()))
            for position, item in enumerate(response.data)
        ]
        vectors = ordered_vectors(rows, expected=len(bodies))
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            # Unknown, not free.
            tokens = None
        return ProviderEmbeddings(
            provider=self.name,
            model=str(getattr(response, "model", "") or ""),
            vectors=vectors,
            input_tokens=tokens,
        )


def build_embedding_providers() -> dict[str, EmbeddingProvider]:
    """Adapters that exist, keyed by the provider name the registry uses.

    A provider absent from this map has no embedding API here, and the gateway
    adapter refuses rather than substituting another verb.  Anthropic is absent
    for exactly that reason -- it serves completions, not embeddings, and
    asking a chat model for vectors in prose produces text shaped like numbers
    that nothing downstream can tell from a real vector.

    This function is not called anywhere by default.  The caller passes the map
    it wants, so adding a file here never activates a provider on its own.
    """
    return {"openai": OpenAIEmbeddingProvider()}


__all__ = ["OpenAIEmbeddingProvider", "build_embedding_providers"]
