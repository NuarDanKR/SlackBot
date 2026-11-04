"""The one place a B-12 index batch turns into a provider call.

This implements ``embedding_index_job.Embedder``.  It owns three jobs and
nothing else:

* decide **which** registered model revision this call is for, and refuse if
  the registry does not describe it as an active embedding model;
* make **one** call, with a timeout bound to the SDK;
* check the answer against what was asked before a single vector is handed on.

What it deliberately does not do:

* **No retry, no fallback.**  A retry doubles the bill on a call that may have
  partly succeeded, and a fallback publishes vectors from a model other than
  the one the document's manifest will claim -- two revisions mixed inside one
  index, with nothing in the table to show it.  The caller reruns.
* **No completion.**  A provider without an embedding API is refused, never
  routed through ``complete``.
* **No bodies anywhere.**  Source text, vectors, and provider error bodies stay
  out of exceptions, logs and ``repr``.  Everything this module raises is one
  fixed code from :data:`EMBEDDING_FAILURE_CODES`.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tybot.embedding_index_job import (
    EMBEDDING_FAILURE_OUTCOMES,
    EmbeddingBatch,
    EmbeddingFailure,
    EmbeddingInput,
)
from tybot.gateway.base import ProviderTimeout
from tybot.gateway.embedding import (
    MAX_DIMENSIONS,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingResultInvalid,
    EmbeddingUnsupported,
)
from tybot.gateway.providers._timeout import is_timeout as _is_timeout

#: Every refusal this module can produce.  Fixed words: they go into counters
#: and operator-facing screens, so they must not vary with the request.
EMBEDDING_FAILURE_CODES = EMBEDDING_FAILURE_OUTCOMES

#: A whole batch, not one chunk.  Short enough that a stuck provider does not
#: hold a run open; long enough that a large batch is not cut off mid-flight.
DEFAULT_TIMEOUT_SECONDS = 60.0


class EmbeddingCallError(EmbeddingFailure):
    """A refused embedding call.  ``str()`` is a fixed code and nothing else."""

@dataclass(frozen=True)
class EmbeddingBinding:
    """What the model registry says one revision is.

    Read from ``search_embedding_model_revision``.  This module never invents
    a provider or a model name, and never substitutes one for another.
    """

    revision: str
    provider: str
    model: str
    dimensions: int
    state: str = "active"
    input_price_per_mtok: float | None = None

    def usable(self, revision: str) -> bool:
        return (
            self.revision == revision
            and self.state == "active"
            and isinstance(self.dimensions, int)
            and not isinstance(self.dimensions, bool)
            and 1 <= self.dimensions <= MAX_DIMENSIONS
            and bool(self.provider)
            and self.provider == self.provider.strip()
            and bool(self.model)
            and self.model == self.model.strip()
            and (
                self.input_price_per_mtok is None
                or (
                    not isinstance(self.input_price_per_mtok, bool)
                    and isinstance(self.input_price_per_mtok, (int, float))
                    and math.isfinite(float(self.input_price_per_mtok))
                    and float(self.input_price_per_mtok) >= 0
                )
            )
        )


def db_bindings(connection) -> Callable[[str], EmbeddingBinding | None]:
    """Look one revision up in the registry.  Reads metadata, never a body."""

    def lookup(revision: str) -> EmbeddingBinding | None:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT revision, provider, model, dimensions, state,
                          input_price_per_mtok
                     FROM search_embedding_model_revision
                    WHERE revision = %s""",
                (revision,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return EmbeddingBinding(
            revision=str(row[0]),
            provider=str(row[1]),
            model=str(row[2]),
            dimensions=int(row[3]),
            state=str(row[4]),
            input_price_per_mtok=(None if row[5] is None else float(row[5])),
        )

    return lookup


class GatewayEmbedder:
    """``embedding_index_job.Embedder`` backed by the gateway's embedding API.

    Providers are injected.  Nothing here discovers or enables one, so adding
    an adapter file cannot by itself start sending archive text anywhere.
    """

    def __init__(
        self,
        *,
        bindings: Callable[[str], EmbeddingBinding | None],
        providers: Mapping[str, EmbeddingProvider],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(bindings):
            raise ValueError("bindings must be callable")
        if not isinstance(providers, Mapping):
            raise ValueError("providers must be a mapping")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._bindings = bindings
        self._providers = dict(providers)
        self._timeout = float(timeout_seconds)

    def __repr__(self) -> str:
        # Providers only, by name.  No texts are ever held on this object, and
        # listing adapters by repr keeps it that way if one later holds a key.
        return f"GatewayEmbedder(providers={sorted(self._providers)!r})"

    def embed(
        self,
        inputs: tuple[EmbeddingInput, ...],
        *,
        model_revision: str,
        policy_revision: str,
    ) -> EmbeddingBatch:
        texts = _checked_texts(inputs)
        if not _trimmed(model_revision) or not _trimmed(policy_revision):
            # ``policy_revision`` is the caller's proof that an authorization
            # was taken for this batch.  Missing, we do not call out.
            raise EmbeddingCallError("embedding_request_invalid")

        binding = self._bindings(model_revision)
        if not isinstance(binding, EmbeddingBinding) or not binding.usable(
            model_revision
        ):
            raise EmbeddingCallError("embedding_model_unsupported")

        provider = self._providers.get(binding.provider)
        if (
            provider is None
            or not isinstance(provider, EmbeddingProvider)
            or provider.name != binding.provider
        ):
            # No embedding adapter for this provider. There is no stand-in:
            # a completion model asked for vectors answers with prose.
            raise EmbeddingCallError("embedding_provider_unsupported")

        model = EmbeddingModel(
            provider=binding.provider,
            model=binding.model,
            dimensions=binding.dimensions,
        )
        result = self._call_once(provider, model, texts)
        vectors = _checked_vectors(result, model, expected=len(texts))
        return EmbeddingBatch.checked(
            model_revision,
            vectors,
            cost_usd=self._cost(binding.input_price_per_mtok, result.input_tokens),
        )

    def _call_once(self, provider, model: EmbeddingModel, texts: tuple[str, ...]):
        """One attempt.  Every failure path ends here, as a fixed code.

        ``from None`` throughout: an embedding endpoint's 4xx body often echoes
        the input it rejected, and that input is archive source text.  A chained
        traceback would carry it into every log that prints the exception.
        """
        try:
            return provider.embed(model, texts, timeout_seconds=self._timeout)
        except EmbeddingUnsupported:
            raise EmbeddingCallError("embedding_provider_unsupported") from None
        except ProviderTimeout:
            raise EmbeddingCallError("embedding_call_timeout") from None
        except EmbeddingResultInvalid:
            # The call succeeded and came back unusable -- a missing row, a
            # repeated index. Either way the batch is short of what was asked.
            raise EmbeddingCallError("embedding_result_incomplete") from None
        except EmbeddingCallError:
            raise
        except Exception as exc:  # noqa: BLE001 - any provider body must not escape
            # KeyboardInterrupt and SystemExit are deliberately not caught: a
            # stop signal is not a provider failure.
            if _is_timeout(exc):
                raise EmbeddingCallError("embedding_call_timeout") from None
            raise EmbeddingCallError("embedding_call_failed") from None

    @staticmethod
    def _cost(price_per_mtok: float | None, tokens: int | None) -> float | None:
        """Unknown stays ``None``.

        ``0.0`` would be a measurement -- it would sum into a run total and read
        as "this run was free", which is exactly the wrong thing to believe
        about a batch that was sent to an external provider.
        """
        if price_per_mtok is None or tokens is None:
            return None
        if (
            isinstance(price_per_mtok, bool)
            or not isinstance(price_per_mtok, (int, float))
            or not math.isfinite(float(price_per_mtok))
            or float(price_per_mtok) < 0
        ):
            return None
        return tokens / 1_000_000 * float(price_per_mtok)


def _trimmed(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _checked_texts(inputs: Sequence[EmbeddingInput]) -> tuple[str, ...]:
    """Send exactly what the plan prepared, in the plan's order.

    The digest is re-checked here rather than trusted.  It is the only thing
    that ties the body about to leave this process to the chunk coordinates
    that will be stored for it; if the two ever drift, every citation for this
    document points at text that was never embedded.
    """
    if not isinstance(inputs, (tuple, list)) or not inputs:
        raise EmbeddingCallError("embedding_request_invalid")
    texts: list[str] = []
    for position, item in enumerate(inputs):
        text = getattr(item, "text", None)
        digest = getattr(item, "text_sha256", None)
        number = getattr(item, "chunk_no", None)
        if (
            not isinstance(text, str)
            or not text
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number != position
            or not isinstance(digest, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        ):
            raise EmbeddingCallError("embedding_request_invalid")
        texts.append(text)
    return tuple(texts)


def _checked_vectors(result, model: EmbeddingModel, *, expected: int):
    """Compare the answer with the question before anything is handed on."""
    provider = getattr(result, "provider", None)
    answered = getattr(result, "model", None)
    vectors = getattr(result, "vectors", None)
    if provider != model.provider or answered != model.model:
        # The bill, the registry row and the stored manifest would all disagree
        # about which model produced these numbers.
        raise EmbeddingCallError("embedding_identity_mismatch")
    if not isinstance(vectors, (tuple, list)) or len(vectors) != expected:
        # Truncated or padded. Publishing either would pair a chunk with a
        # vector for different text.
        raise EmbeddingCallError("embedding_result_incomplete")
    prepared: list[tuple[float, ...]] = []
    for vector in vectors:
        if not isinstance(vector, (tuple, list)) or len(vector) != model.dimensions:
            raise EmbeddingCallError("embedding_dimension_mismatch")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise EmbeddingCallError("embedding_vector_invalid")
        prepared.append(tuple(float(value) for value in vector))
    return tuple(prepared)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EMBEDDING_FAILURE_CODES",
    "EmbeddingBinding",
    "EmbeddingCallError",
    "GatewayEmbedder",
    "db_bindings",
]
