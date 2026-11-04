"""Shadow-only rank fusion for keyword and revalidated vector evidence (B-12).

This module does not search, open archive files, or change ``SearchHit.score``.
Both inputs must already be ranked and permission-checked.  The output keeps
the original hit object and adds a separate reciprocal-rank score so keyword
and vector quality can be compared without silently changing production
answers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from tybot.archive.store import SearchHit

MAX_RESULTS = 500
DEFAULT_RANK_CONSTANT = 60


@dataclass(frozen=True)
class HybridHit:
    fusion_score: float
    keyword_rank: int | None
    vector_rank: int | None
    hit: SearchHit = field(repr=False)

    @property
    def origin(self) -> str:
        if self.keyword_rank is not None and self.vector_rank is not None:
            return "both"
        return "keyword" if self.keyword_rank is not None else "vector"


@dataclass(frozen=True)
class HybridRanking:
    vector_state: str  # unavailable | empty | available
    keyword_count: int
    vector_count: int
    overlap_count: int
    results: tuple[HybridHit, ...] = field(repr=False)

    @property
    def hits(self) -> tuple[SearchHit, ...]:
        return tuple(item.hit for item in self.results)


def fuse(
    keyword_hits: list[SearchHit] | tuple[SearchHit, ...],
    vector_hits: list[SearchHit] | tuple[SearchHit, ...] | None,
    *,
    limit: int,
    keyword_weight: float = 1.0,
    vector_weight: float = 1.0,
    rank_constant: int = DEFAULT_RANK_CONSTANT,
) -> HybridRanking:
    """Fuse two ranked lists by reciprocal rank, preserving failover exactly."""
    _check_options(limit, keyword_weight, vector_weight, rank_constant)
    keywords = _unique(keyword_hits)
    vectors = _unique(vector_hits or ())
    vector_state = "unavailable" if vector_hits is None else "available" if vectors else "empty"

    # Vector lookup failure or an empty vector result must not perturb the
    # existing keyword path.  This is stronger than producing an equivalent
    # sort: callers receive the same hit objects in the same order.
    if not vectors:
        results = tuple(
            HybridHit(
                fusion_score=keyword_weight / (rank_constant + rank),
                keyword_rank=rank,
                vector_rank=None,
                hit=hit,
            )
            for rank, hit in enumerate(keywords[:limit], start=1)
        )
        return HybridRanking(vector_state, len(keywords), 0, 0, results)

    keyword_rank = {_identity(hit): rank for rank, hit in enumerate(keywords, start=1)}
    vector_rank = {_identity(hit): rank for rank, hit in enumerate(vectors, start=1)}
    by_identity = {_identity(hit): hit for hit in keywords}
    for hit in vectors:
        by_identity.setdefault(_identity(hit), hit)

    ranked: list[tuple[tuple, HybridHit]] = []
    for identity, hit in by_identity.items():
        kr = keyword_rank.get(identity)
        vr = vector_rank.get(identity)
        score = 0.0
        if kr is not None:
            score += keyword_weight / (rank_constant + kr)
        if vr is not None:
            score += vector_weight / (rank_constant + vr)
        ranked.append(
            (
                (
                    -score,
                    kr if kr is not None else math.inf,
                    vr if vr is not None else math.inf,
                    identity,
                ),
                HybridHit(score, kr, vr, hit),
            )
        )
    ranked.sort(key=lambda item: item[0])
    results = tuple(item[1] for item in ranked[:limit])
    overlap = len(keyword_rank.keys() & vector_rank.keys())
    return HybridRanking("available", len(keywords), len(vectors), overlap, results)


def _unique(hits) -> tuple[SearchHit, ...]:
    seen: set[tuple[str, str, str, int]] = set()
    out: list[SearchHit] = []
    for hit in hits:
        if not isinstance(hit, SearchHit):
            raise TypeError("rank inputs must contain SearchHit")
        identity = _identity(hit)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(hit)
    return tuple(out)


def _identity(hit: SearchHit) -> tuple[str, str, str, int]:
    source = str(hit.line.source_path or hit.doc.path)
    return (str(hit.doc.workspace), str(hit.doc.channel_id or hit.doc.channel), source, int(hit.line.lineno))


def _check_options(
    limit: int, keyword_weight: float, vector_weight: float, rank_constant: int
) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    if (
        isinstance(rank_constant, bool)
        or not isinstance(rank_constant, int)
        or rank_constant < 1
    ):
        raise ValueError("rank_constant must be a positive integer")
    for name, value in (("keyword_weight", keyword_weight), ("vector_weight", vector_weight)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
