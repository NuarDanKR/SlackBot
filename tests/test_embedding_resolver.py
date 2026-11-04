from __future__ import annotations

import hashlib

import pytest

from tybot.embedding_resolver import VisibleVectorSource, resolve_candidates
from tybot.embedding_search import VectorCandidate


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def candidate(
    text: str = "alpha beta",
    *,
    path: str = "workspaces/tyit/channels/C1/raw/2026-11-04.md",
    start: int = 0,
    end: int = 5,
) -> VectorCandidate:
    return VectorCandidate(
        workspace="tyit",
        channel_id="C1",
        doc_path=path,
        source_revision=digest(text),
        chunker_revision="chunk-v1:max=1200:ov=120",
        model_revision="local-v1",
        chunk_no=0,
        start_offset=start,
        end_offset=end,
        text_sha256=digest(text[start:end]),
        distance=0.1,
    )


def source(text: str = "alpha beta") -> VisibleVectorSource:
    return VisibleVectorSource(
        workspace="tyit",
        channel_id="C1",
        doc_path="workspaces/tyit/channels/C1/raw/2026-11-04.md",
        text=text,
    )


def test_exact_current_visible_source_is_resolved():
    got = resolve_candidates([candidate()], open_visible=lambda *_: source())
    assert [item.text for item in got.resolved] == ["alpha"]
    assert got.dropped == ()


def test_permission_denial_and_missing_source_share_one_non_oracle_code():
    got = resolve_candidates([candidate()], open_visible=lambda *_: None)
    assert got.resolved == ()
    assert got.dropped == ("source_not_visible",)


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("changed body", "source_revision_changed"),
        ("alpha beta plus", "source_revision_changed"),
    ],
)
def test_changed_source_revision_is_never_used(replacement, code):
    got = resolve_candidates([candidate()], open_visible=lambda *_: source(replacement))
    assert got.resolved == ()
    assert got.dropped == (code,)


def test_bad_offsets_and_changed_chunk_hash_are_dropped():
    bad_bounds = candidate(end=99)
    # Keep the source revision valid so the bounds check is the rejecting gate.
    bad_bounds = VectorCandidate(**{**bad_bounds.__dict__, "source_revision": digest("alpha beta")})
    got = resolve_candidates([bad_bounds], open_visible=lambda *_: source())
    assert got.dropped == ("chunk_bounds_changed",)

    changed_hash = VectorCandidate(**{**candidate().__dict__, "text_sha256": "0" * 64})
    got = resolve_candidates([changed_hash], open_visible=lambda *_: source())
    assert got.dropped == ("chunk_hash_changed",)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "/absolute.md",
        "workspaces\\secret.md",
        "a/./b.md",
        "a//b.md",
        "C:/secret.md",
        "bad\x00path.md",
        "",
    ],
)
def test_untrusted_database_paths_are_never_passed_to_the_opener(path):
    opened = []
    got = resolve_candidates(
        [candidate(path=path)],
        open_visible=lambda *args: opened.append(args),
    )
    assert opened == []
    assert got.dropped == ("source_not_visible",)


def test_opener_cannot_return_another_workspace_channel_or_path():
    wrong = source()
    wrong = VisibleVectorSource(**{**wrong.__dict__, "channel_id": "C2"})
    got = resolve_candidates([candidate()], open_visible=lambda *_: wrong)
    assert got.dropped == ("source_not_visible",)


def test_each_visible_source_is_opened_only_once_for_multiple_chunks():
    calls = []
    text = "alpha beta"
    candidates = [candidate(text, start=0, end=5), candidate(text, start=6, end=10)]
    got = resolve_candidates(
        candidates,
        open_visible=lambda *args: calls.append(args) or source(),
    )
    assert [item.text for item in got.resolved] == ["alpha", "beta"]
    assert len(calls) == 1


def test_candidate_count_is_bounded_before_any_source_is_opened():
    opened = []
    with pytest.raises(ValueError, match="500"):
        resolve_candidates(
            [candidate()] * 501,
            open_visible=lambda *args: opened.append(args),
        )
    assert opened == []


def test_drop_codes_and_objects_do_not_include_source_text():
    got = resolve_candidates([candidate()], open_visible=lambda *_: None)
    assert "alpha" not in repr(got)
