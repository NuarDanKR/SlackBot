"""The gateway boundary for B-12 embedding calls, exercised with a fake provider.

No SDK, no network, no database.  What these check is the boundary's own
promises: one call, nothing retried, nothing substituted, and no body in
anything that can reach a log.
"""
from __future__ import annotations

import hashlib

import pytest

from tybot.embedding_gateway import (
    EMBEDDING_FAILURE_CODES,
    EmbeddingBinding,
    EmbeddingCallError,
    GatewayEmbedder,
    db_bindings,
)
from tybot.embedding_index_job import EmbeddingBatch, EmbeddingInput
from tybot.gateway.base import ProviderTimeout
from tybot.gateway.embedding import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingResultInvalid,
    EmbeddingUnsupported,
    ProviderEmbeddings,
    ordered_vectors,
)

REVISION = "local-v1"
SECRET = "주민등록번호 880101-1234567 가 들어 있는 본문"


def item(number: int, text: str) -> EmbeddingInput:
    return EmbeddingInput(
        chunk_no=number,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def inputs(*texts: str) -> tuple[EmbeddingInput, ...]:
    return tuple(item(number, text) for number, text in enumerate(texts))


def binding(**overrides) -> EmbeddingBinding:
    values = {
        "revision": REVISION,
        "provider": "fake",
        "model": "fake-embed-2",
        "dimensions": 2,
        "state": "active",
        "input_price_per_mtok": None,
    }
    values.update(overrides)
    return EmbeddingBinding(**values)


class FakeProvider:
    """Records every call.  Has no ``complete`` -- reaching for one is an error."""

    name = "fake"

    def __init__(self, *, vectors=None, raises=None, provider=None, model=None,
                 tokens=None):
        self.calls: list[tuple[EmbeddingModel, tuple[str, ...], float | None]] = []
        self._vectors = vectors
        self._raises = raises
        self._provider = provider
        self._model = model
        self._tokens = tokens

    def embed(self, model, texts, *, timeout_seconds=None):
        self.calls.append((model, tuple(texts), timeout_seconds))
        if self._raises is not None:
            raise self._raises
        vectors = (
            tuple((0.1, 0.2) for _ in texts) if self._vectors is None else self._vectors
        )
        return ProviderEmbeddings(
            provider=self._provider or self.name,
            model=self._model or model.model,
            vectors=tuple(vectors),
            input_tokens=self._tokens,
        )


ABSENT = object()


def embedder(provider=None, *, bound=ABSENT, **kwargs) -> GatewayEmbedder:
    found = binding() if bound is ABSENT else bound
    return GatewayEmbedder(
        # Whatever the registry holds is handed back as-is, including a row for
        # another revision -- the boundary's own guard has to catch that.
        bindings=lambda _revision: found,
        providers={"fake": provider} if provider is not None else {},
        **kwargs,
    )


def call(embed: GatewayEmbedder, *texts: str) -> EmbeddingBatch:
    return embed.embed(
        inputs(*(texts or ("alpha", "bravo"))),
        model_revision=REVISION,
        policy_revision="workspace:tyit:r3",
    )


# --- the happy path, and the shape of the call ---------------------------------


def test_one_batch_is_one_call_in_plan_order():
    provider = FakeProvider()
    result = call(embedder(provider), "alpha", "bravo", "charlie")

    assert isinstance(result, EmbeddingBatch)
    assert result.model_revision == REVISION
    assert len(result.vectors) == 3
    assert len(provider.calls) == 1
    model, texts, timeout = provider.calls[0]
    assert texts == ("alpha", "bravo", "charlie")
    assert (model.provider, model.model, model.dimensions) == ("fake", "fake-embed-2", 2)
    assert timeout == 60.0


def test_the_timeout_is_handed_to_the_provider_not_merely_measured():
    provider = FakeProvider()
    call(embedder(provider, timeout_seconds=2.5))

    assert provider.calls[0][2] == 2.5


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), True, "30"])
def test_an_unusable_timeout_is_refused_at_construction(bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        embedder(FakeProvider(), timeout_seconds=bad)


# --- one-shot: nothing is retried, nothing is substituted ----------------------


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (ProviderTimeout("slow"), "embedding_call_timeout"),
        (TimeoutError("slow"), "embedding_call_timeout"),
        (RuntimeError("provider said " + SECRET), "embedding_call_failed"),
        (EmbeddingUnsupported("fake"), "embedding_provider_unsupported"),
        (EmbeddingResultInvalid("embedding_result_misaligned"), "embedding_result_incomplete"),
    ],
)
def test_a_failed_call_is_refused_once_with_a_fixed_code(raised, code):
    provider = FakeProvider(raises=raised)
    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(provider))

    assert caught.value.code == code
    assert code in EMBEDDING_FAILURE_CODES
    # One attempt. A retry doubles the bill on a call that may have half
    # succeeded, and the caller reruns from the canonical source anyway.
    assert len(provider.calls) == 1


def test_a_provider_without_an_embedding_api_is_never_routed_to_completion():
    """The registry names a provider we have no embedding adapter for.

    There is no stand-in. A chat model asked in prose for a vector answers with
    text shaped like numbers, and nothing downstream could tell that apart from
    a real embedding -- it would be stored as evidence coordinates and rank
    search results.
    """
    completion_calls = []

    class CompletionOnly:
        name = "fake"

        def complete(self, *args, **kwargs):
            completion_calls.append(args)
            raise AssertionError("completion must not be reached")

    embed = GatewayEmbedder(
        bindings=lambda _revision: binding(provider="anthropic"),
        providers={"fake": CompletionOnly()},
    )
    with pytest.raises(EmbeddingCallError) as caught:
        call(embed)

    assert caught.value.code == "embedding_provider_unsupported"
    assert completion_calls == []


def test_an_unknown_provider_is_refused_before_anything_is_sent():
    provider = FakeProvider()
    embed = GatewayEmbedder(
        bindings=lambda _revision: binding(provider="nowhere"),
        providers={"fake": provider},
    )
    with pytest.raises(EmbeddingCallError) as caught:
        call(embed)

    assert caught.value.code == "embedding_provider_unsupported"
    assert provider.calls == []


# --- the registry decides what runs; this module never does --------------------


@pytest.mark.parametrize(
    "bound",
    [
        None,
        binding(state="draft"),
        binding(state="retired"),
        binding(revision="other-v1"),
        binding(provider="  "),
        binding(provider=" fake"),
        binding(provider="fake "),
        binding(model=""),
        binding(model=" fake-embed-2"),
        binding(model="fake-embed-2 "),
        binding(dimensions=0),
        binding(dimensions=16_001),
        binding(dimensions=True),
    ],
)
def test_a_model_the_registry_does_not_describe_is_refused(bound):
    provider = FakeProvider()
    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(provider, bound=bound))

    assert caught.value.code == "embedding_model_unsupported"
    assert provider.calls == []


def test_a_binding_from_somewhere_else_is_not_trusted():
    provider = FakeProvider()
    embed = GatewayEmbedder(
        bindings=lambda _revision: {"provider": "fake", "model": "fake-embed-2"},
        providers={"fake": provider},
    )
    with pytest.raises(EmbeddingCallError) as caught:
        call(embed)

    assert caught.value.code == "embedding_model_unsupported"
    assert provider.calls == []


def test_a_registered_name_must_point_to_an_embedding_provider_with_that_name():
    class WrongProvider(FakeProvider):
        name = "other"

    provider = WrongProvider()
    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(provider))

    assert caught.value.code == "embedding_provider_unsupported"
    assert provider.calls == []


def test_a_completion_only_object_under_the_expected_name_is_not_called():
    class CompletionOnly:
        name = "fake"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("completion must not be reached")

    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(CompletionOnly()))

    assert caught.value.code == "embedding_provider_unsupported"


# --- the answer is compared with the question ----------------------------------


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"provider": "elsewhere"}, "embedding_identity_mismatch"),
        ({"model": "fake-embed-3"}, "embedding_identity_mismatch"),
        ({"vectors": ((0.1, 0.2),)}, "embedding_result_incomplete"),
        ({"vectors": ((0.1, 0.2), (0.3, 0.4), (0.5, 0.6))}, "embedding_result_incomplete"),
        ({"vectors": ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))}, "embedding_dimension_mismatch"),
        ({"vectors": ((0.1,), (0.2,))}, "embedding_dimension_mismatch"),
        ({"vectors": ((0.1, float("nan")), (0.3, 0.4))}, "embedding_vector_invalid"),
        ({"vectors": ((0.1, float("inf")), (0.3, 0.4))}, "embedding_vector_invalid"),
        ({"vectors": ((0.1, True), (0.3, 0.4))}, "embedding_vector_invalid"),
        ({"vectors": ((0.1, "0.2"), (0.3, 0.4))}, "embedding_vector_invalid"),
    ],
)
def test_an_answer_that_does_not_match_the_question_is_refused(kwargs, code):
    provider = FakeProvider(**kwargs)
    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(provider), "alpha", "bravo")

    assert caught.value.code == code


def test_a_matching_answer_passes_the_same_checks():
    """Only testing refusals would let a check that refuses everything pass."""
    result = call(embedder(FakeProvider()), "alpha", "bravo")

    assert result.vectors == ((0.1, 0.2), (0.1, 0.2))


def test_an_answer_without_actual_model_identity_is_refused():
    class MissingIdentity(FakeProvider):
        def embed(self, model, texts, *, timeout_seconds=None):
            self.calls.append((model, tuple(texts), timeout_seconds))
            return ProviderEmbeddings("fake", "", ((0.1, 0.2),) * len(texts), 1)

    with pytest.raises(EmbeddingCallError) as caught:
        call(embedder(MissingIdentity()))

    assert caught.value.code == "embedding_identity_mismatch"


# --- the request itself ---------------------------------------------------------


def test_a_digest_that_does_not_match_its_body_is_refused_before_the_call():
    """The digest is the only thing tying the body sent out to the stored chunk.

    If the two drift, every citation for that document points at text that was
    never embedded -- and nothing downstream can see it.
    """
    provider = FakeProvider()
    wrong = (EmbeddingInput(chunk_no=0, text_sha256="0" * 64, text="alpha"),)
    with pytest.raises(EmbeddingCallError) as caught:
        embedder(provider).embed(
            wrong, model_revision=REVISION, policy_revision="workspace:tyit:r3"
        )

    assert caught.value.code == "embedding_request_invalid"
    assert provider.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        (),
        (EmbeddingInput(chunk_no=1, text_sha256=hashlib.sha256(b"a").hexdigest(), text="a"),),
        (EmbeddingInput(chunk_no=0, text_sha256=hashlib.sha256(b"").hexdigest(), text=""),),
    ],
)
def test_an_unusable_request_never_reaches_the_provider(bad):
    provider = FakeProvider()
    with pytest.raises(EmbeddingCallError) as caught:
        embedder(provider).embed(
            bad, model_revision=REVISION, policy_revision="workspace:tyit:r3"
        )

    assert caught.value.code == "embedding_request_invalid"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("revision", "policy"),
    [("", "workspace:tyit:r3"), (" local-v1", "p"), (REVISION, ""), (REVISION, " p")],
)
def test_a_call_without_a_revision_or_a_policy_proof_is_refused(revision, policy):
    """``policy_revision`` is the caller's proof an authorization was taken."""
    provider = FakeProvider()
    with pytest.raises(EmbeddingCallError) as caught:
        embedder(provider).embed(
            inputs("alpha"), model_revision=revision, policy_revision=policy
        )

    assert caught.value.code == "embedding_request_invalid"
    assert provider.calls == []


# --- cost: unknown is None, never zero ------------------------------------------


def test_an_unreported_usage_leaves_the_cost_unknown():
    result = call(embedder(FakeProvider(tokens=None)))

    assert result.cost_usd is None


def test_a_known_price_without_usage_is_still_unknown():
    result = call(
        embedder(FakeProvider(tokens=None), bound=binding(input_price_per_mtok=0.02))
    )

    assert result.cost_usd is None


def test_usage_without_a_known_price_is_still_unknown():
    result = call(embedder(FakeProvider(tokens=1_000_000)))

    assert result.cost_usd is None


def test_a_known_price_and_reported_usage_make_a_cost():
    result = call(
        embedder(
            FakeProvider(tokens=500_000),
            bound=binding(input_price_per_mtok=0.02),
        )
    )

    assert result.cost_usd == pytest.approx(0.01)


def test_an_explicit_zero_price_is_a_known_free_call():
    result = call(
        embedder(
            FakeProvider(tokens=500_000),
            bound=binding(input_price_per_mtok=0.0),
        )
    )

    assert result.cost_usd == 0.0


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1, True, "0.02"])
def test_an_unusable_registry_price_refuses_the_model_before_the_call(price):
    provider = FakeProvider(tokens=1000)
    with pytest.raises(EmbeddingCallError) as caught:
        call(
            embedder(
                provider,
                bound=binding(input_price_per_mtok=price),
            )
        )

    assert caught.value.code == "embedding_model_unsupported"
    assert provider.calls == []


def test_a_reported_zero_cost_is_not_invented_from_nothing():
    """A real zero only comes from a real price and real usage."""
    result = call(
        embedder(
            FakeProvider(tokens=0),
            bound=binding(input_price_per_mtok=0.02),
        )
    )

    assert result.cost_usd == 0.0


# --- nothing carries a body ------------------------------------------------------


def test_no_body_appears_in_a_refusal():
    provider = FakeProvider(raises=RuntimeError("400 invalid_request: " + SECRET))
    with pytest.raises(EmbeddingCallError) as caught:
        embedder(provider).embed(
            inputs(SECRET), model_revision=REVISION, policy_revision="p"
        )

    rendered = f"{caught.value!r} {caught.value!s} {caught.value.args!r}"
    assert SECRET not in rendered
    assert "invalid_request" not in rendered
    # The cause is dropped on purpose: a chained traceback would print the
    # provider's body wherever the exception is logged.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_no_body_appears_in_the_embedder_or_its_result():
    provider = FakeProvider(tokens=7)
    embed = embedder(provider)
    result = embed.embed(
        inputs(SECRET), model_revision=REVISION, policy_revision="workspace:tyit:r3"
    )

    assert SECRET not in repr(embed)
    assert SECRET not in repr(result)
    # Vectors are derived from the body and are kept out of repr as well.
    assert "0.1" not in repr(ProviderEmbeddings("fake", "m", ((0.1, 0.2),), 1))


def test_the_plan_input_keeps_its_body_out_of_repr():
    assert SECRET not in repr(item(0, SECRET))


# --- the provider-facing contract -------------------------------------------------


def test_a_fake_provider_satisfies_the_narrow_protocol():
    assert isinstance(FakeProvider(), EmbeddingProvider)


def test_rows_are_put_back_in_request_order():
    rows = [(2, (0.5, 0.6)), (0, (0.1, 0.2)), (1, (0.3, 0.4))]

    assert ordered_vectors(rows, expected=3) == ((0.1, 0.2), (0.3, 0.4), (0.5, 0.6))


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([(0, (0.1,)), (0, (0.2,))], "misaligned"),
        ([(0, (0.1,)), (5, (0.2,))], "misaligned"),
        ([(0, (0.1,)), (True, (0.2,))], "misaligned"),
        ([(0, (0.1,))], "incomplete"),
    ],
)
def test_a_response_that_does_not_line_up_is_refused(rows, message):
    """A repeated index sorts into the right count and the wrong alignment.

    Every chunk past that point would carry a vector for different text, with
    valid coordinates and valid dimensions. Nothing downstream could see it.
    """
    with pytest.raises(EmbeddingResultInvalid, match=message):
        ordered_vectors(rows, expected=2)


def test_an_empty_request_has_no_ordering_to_check():
    with pytest.raises(EmbeddingResultInvalid, match="empty"):
        ordered_vectors([], expected=0)


@pytest.mark.parametrize(
    "bad",
    [
        {"provider": "", "model": "m", "dimensions": 2},
        {"provider": " p", "model": "m", "dimensions": 2},
        {"provider": "p", "model": "", "dimensions": 2},
        {"provider": "p", "model": "m", "dimensions": 0},
        {"provider": "p", "model": "m", "dimensions": 16_001},
        {"provider": "p", "model": "m", "dimensions": True},
    ],
)
def test_an_unusable_model_description_is_refused(bad):
    with pytest.raises(ValueError):
        EmbeddingModel(**bad).checked()


# --- the registry reader ----------------------------------------------------------


class Cursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(str(sql).split()), tuple(params)))

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.cur = Cursor(row)

    def cursor(self):
        return self.cur


def test_the_registry_reader_takes_metadata_only():
    conn = Connection(
        ("local-v1", "openai", "text-embedding-x", 1536, "active", 0.02)
    )
    found = db_bindings(conn)("local-v1")

    assert found == EmbeddingBinding(
        "local-v1", "openai", "text-embedding-x", 1536, "active", 0.02
    )
    sql, params = conn.cur.calls[0]
    assert params == ("local-v1",)
    # Metadata only: no key column, no body column, nothing but the row's identity.
    assert "search_embedding_model_revision" in sql
    assert "embedding" not in sql.replace("search_embedding_model_revision", "")


def test_an_unregistered_revision_reads_as_absent():
    assert db_bindings(Connection(None))("local-v1") is None


def test_an_unknown_failure_code_cannot_be_raised():
    """The codes are fixed words. A typo must fail here, not reach a screen."""
    with pytest.raises(ValueError, match="unknown embedding failure code"):
        EmbeddingCallError("embedding_went_wrong")
