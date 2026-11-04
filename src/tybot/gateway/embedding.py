"""Embedding is its own gateway surface.  Completion is never reused for it.

The gateway grew around one verb -- ``complete`` -- and an embedding call is a
different thing: it takes many short bodies at once, returns fixed-width
vectors rather than prose, and has no sampling, no tools and no stop reason.

Reusing ``complete`` for it would be the easy move and the wrong one.  A model
asked in prose to "return the embedding" answers with text that *looks* like
numbers, and nothing downstream can tell that apart from a real vector: it
would be stored as evidence coordinates and silently rank search results.  So
a provider that has no embedding API has **no substitute** here -- it raises
:class:`EmbeddingUnsupported` and the call stops.

This module holds types only.  Provider SDKs are imported lazily, and only
under ``gateway/providers``.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: Same ceiling the index schema enforces (``dimensions BETWEEN 1 AND 16000``).
MAX_DIMENSIONS = 16_000


class EmbeddingUnsupported(RuntimeError):
    """This provider has no embedding API.

    Raised instead of falling back to a completion call.  The message is a
    fixed word; a provider name is fine, a request body is not.
    """


class EmbeddingResultInvalid(RuntimeError):
    """The provider's response does not line up with the request.

    Separate from a transport failure: the call succeeded and came back
    unusable.  The message is a fixed code and never carries a body.
    """


@dataclass(frozen=True)
class EmbeddingModel:
    """What a registered model revision actually is, on the wire.

    The adapter does not choose these values.  They are read from the model
    registry, which an operator fills in deliberately.
    """

    provider: str
    model: str
    dimensions: int

    def checked(self) -> EmbeddingModel:
        for name, value in (("provider", self.provider), ("model", self.model)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and trimmed")
        if (
            isinstance(self.dimensions, bool)
            or not isinstance(self.dimensions, int)
            or not 1 <= self.dimensions <= MAX_DIMENSIONS
        ):
            raise ValueError("dimensions is out of range")
        return self


@dataclass(frozen=True)
class ProviderEmbeddings:
    """One provider response.

    ``vectors`` is kept out of ``repr`` deliberately.  A vector is derived from
    the source body: a few thousand of them in a log line is both a leak and an
    unreadable log.  ``input_tokens`` is ``None`` when the provider did not
    report usage -- not ``0``, which would read as "this call was free".
    """

    provider: str
    model: str
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    input_tokens: int | None = None


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Narrow: one verb, no sampling, no tools, no streaming.

    ``timeout_seconds`` is bound to the SDK call, not merely measured around
    it.  Without that, one slow call eats the caller's whole budget and leaves
    a thread nobody can cancel.
    """

    name: str

    def embed(
        self,
        model: EmbeddingModel,
        texts: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderEmbeddings: ...


def ordered_vectors(
    rows: Sequence[tuple[int, Sequence[float]]], *, expected: int
) -> tuple[tuple[float, ...], ...]:
    """Put provider rows back in request order, or refuse.

    Providers return each row with the index of the input it answers, and they
    are allowed to return them in any order.  Sorting is not enough: a response
    that repeats one index and omits another sorts cleanly into the right
    *count* and the wrong *alignment*, and then every chunk after that point
    carries a vector for a different piece of text.  Nothing downstream can
    see that -- the coordinates are valid, the dimensions are right, and the
    search results are simply wrong.

    So the index set must be exactly ``range(expected)``.
    """
    if expected <= 0:
        raise EmbeddingResultInvalid("embedding_request_empty")
    seen: dict[int, tuple[float, ...]] = {}
    for index, vector in rows:
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingResultInvalid("embedding_result_misaligned")
        if index in seen or not 0 <= index < expected:
            raise EmbeddingResultInvalid("embedding_result_misaligned")
        seen[index] = tuple(float(value) for value in vector)
    if len(seen) != expected:
        raise EmbeddingResultInvalid("embedding_result_incomplete")
    return tuple(seen[index] for index in range(expected))


__all__ = [
    "MAX_DIMENSIONS",
    "EmbeddingModel",
    "EmbeddingProvider",
    "EmbeddingResultInvalid",
    "EmbeddingUnsupported",
    "ProviderEmbeddings",
    "ordered_vectors",
]
