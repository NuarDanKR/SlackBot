"""The one real embedding adapter, exercised against a fake SDK.

The SDK is never installed for these: a fake module is put in ``sys.modules``
before the adapter reaches for it.  That is also the point of the first test --
importing this package must not require any provider SDK.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from tybot.gateway.base import ProviderTimeout
from tybot.gateway.embedding import EmbeddingModel, EmbeddingResultInvalid
from tybot.gateway.providers.openai_embeddings import (
    OpenAIEmbeddingProvider,
    build_embedding_providers,
)

SECRET = "주민등록번호 880101-1234567 가 들어 있는 본문"
MODEL = EmbeddingModel(provider="openai", model="text-embedding-x", dimensions=2)


class Row:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class Usage:
    def __init__(self, prompt_tokens):
        self.prompt_tokens = prompt_tokens


class Response:
    def __init__(self, rows, *, model="text-embedding-x", usage=None):
        self.data = rows
        self.model = model
        self.usage = usage


class Embeddings:
    def __init__(self, outcome):
        self.calls: list[dict] = []
        self._outcome = outcome

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class Client:
    def __init__(self, outcome, api_key=None):
        self.api_key = api_key
        self.embeddings = Embeddings(outcome)


@pytest.fixture
def sdk(monkeypatch):
    """Install a fake ``openai`` module and hand back a handle to the client."""
    holder: dict[str, Client] = {}

    def install(outcome):
        module = types.ModuleType("openai")

        def factory(api_key=None):
            holder["client"] = Client(outcome, api_key=api_key)
            return holder["client"]

        module.OpenAI = factory
        monkeypatch.setitem(sys.modules, "openai", module)
        return holder

    return install


def provider() -> OpenAIEmbeddingProvider:
    # An explicit key keeps the secret store and the environment out of the test.
    return OpenAIEmbeddingProvider(api_key="test-key")


def test_the_sdk_is_imported_inside_the_call_not_at_module_level():
    """Importing the gateway must not require any provider SDK to be installed."""
    source = pathlib.Path("src/tybot/gateway/providers/openai_embeddings.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    top_level = [
        name.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for name in node.names
    ]

    assert "openai" not in top_level
    inside = [
        name.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
        if name.name == "openai"
    ]
    assert inside == ["openai"]


def test_a_batch_is_sent_once_and_comes_back_in_request_order(sdk):
    sdk(Response([Row(1, (0.3, 0.4)), Row(0, (0.1, 0.2))], usage=Usage(11)))
    result = provider().embed(MODEL, ["alpha", "bravo"], timeout_seconds=5.0)

    assert result.provider == "openai"
    assert result.model == "text-embedding-x"
    assert result.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert result.input_tokens == 11


def test_the_timeout_is_put_on_the_sdk_call(sdk):
    holder = sdk(Response([Row(0, (0.1, 0.2))], usage=Usage(3)))
    provider().embed(MODEL, ["alpha"], timeout_seconds=2.5)

    sent = holder["client"].embeddings.calls[0]
    assert sent["timeout"] == 2.5
    assert sent["model"] == "text-embedding-x"
    assert sent["input"] == ["alpha"]


def test_a_spent_budget_stops_before_the_call(sdk):
    holder = sdk(Response([Row(0, (0.1, 0.2))]))
    with pytest.raises(ProviderTimeout):
        provider().embed(MODEL, ["alpha"], timeout_seconds=0)

    # Nothing was sent: spending on a result nobody is waiting for is waste.
    assert "client" not in holder


def test_a_timeout_carries_a_fixed_message_and_drops_the_cause(sdk):
    class ReadTimeout(Exception):
        pass

    sdk(ReadTimeout("read timed out while sending " + SECRET))
    with pytest.raises(ProviderTimeout) as caught:
        provider().embed(MODEL, [SECRET], timeout_seconds=1.0)

    assert str(caught.value) == "embedding call timed out"
    assert SECRET not in f"{caught.value!r} {caught.value.args!r}"
    # A chained traceback would print the provider's body wherever this is logged.
    assert caught.value.__cause__ is None


def test_a_non_timeout_error_is_left_for_the_caller_to_classify(sdk):
    """The adapter does not invent a code here. The gateway boundary owns that."""
    sdk(RuntimeError("400 invalid_request"))
    with pytest.raises(RuntimeError, match="invalid_request"):
        provider().embed(MODEL, ["alpha"], timeout_seconds=1.0)


@pytest.mark.parametrize(
    "rows",
    [
        [Row(0, (0.1, 0.2)), Row(0, (0.3, 0.4))],
        [Row(0, (0.1, 0.2))],
        [Row(0, (0.1, 0.2)), Row(7, (0.3, 0.4))],
    ],
)
def test_a_response_that_does_not_line_up_with_the_request_is_refused(sdk, rows):
    sdk(Response(rows))
    with pytest.raises(EmbeddingResultInvalid):
        provider().embed(MODEL, ["alpha", "bravo"], timeout_seconds=1.0)


def test_an_unreported_usage_is_unknown_not_zero(sdk):
    sdk(Response([Row(0, (0.1, 0.2))], usage=None))

    assert provider().embed(MODEL, ["alpha"]).input_tokens is None


def test_a_response_without_model_identity_does_not_assume_the_requested_model(sdk):
    sdk(Response([Row(0, (0.1, 0.2))], model=""))

    assert provider().embed(MODEL, ["alpha"]).model == ""


@pytest.mark.parametrize("tokens", [None, -1, True, "7", 7.5])
def test_an_unusable_usage_reading_is_unknown_not_zero(sdk, tokens):
    sdk(Response([Row(0, (0.1, 0.2))], usage=Usage(tokens)))

    assert provider().embed(MODEL, ["alpha"]).input_tokens is None


def test_an_empty_request_is_refused_before_the_sdk_is_touched(sdk):
    holder = sdk(Response([]))
    with pytest.raises(EmbeddingResultInvalid, match="empty"):
        provider().embed(MODEL, [])

    assert "client" not in holder


def test_the_adapter_cannot_be_used_for_completion():
    """One verb. Nothing here can be talked into answering in prose."""
    assert not hasattr(provider(), "complete")


def test_the_registry_of_adapters_omits_providers_without_an_embedding_api():
    built = build_embedding_providers()

    assert set(built) == {"openai"}
    # Anthropic serves completions. Listing it here would be the shortcut that
    # turns a chat answer into something stored as a vector.
    assert "anthropic" not in built


def test_no_adapter_is_wired_in_by_default():
    """Adding a file under providers/ must not start sending archive text out."""
    import tybot.embedding_gateway as gateway

    source = pathlib.Path("src/tybot/embedding_gateway.py").read_text(encoding="utf-8")

    assert "build_embedding_providers" not in source
    assert not hasattr(gateway, "build_embedding_providers")
