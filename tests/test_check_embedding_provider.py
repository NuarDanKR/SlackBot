"""The opt-in provider smoke tool, exercised without a network or an SDK.

Two things are being held down here.  One is that the default run sends
nothing -- a diagnostic that bills money when run without arguments is a trap.
The other is that nothing it prints can be pasted into a ticket and leak: not
the synthetic bodies, not the vectors, not the key, not a provider error body.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "check_embedding_provider", ROOT / "scripts" / "check_embedding_provider.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

KEY = "sk-live-must-never-be-printed-0123456789"
PLAN = ["--provider", "openai", "--model", "text-embedding-x", "--dimensions", "2"]


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
    def __init__(self, outcome, calls):
        self._outcome = outcome
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class Client:
    def __init__(self, outcome, calls, api_key=None):
        self.api_key = api_key
        self.embeddings = Embeddings(outcome, calls)


@pytest.fixture
def sdk(monkeypatch):
    """Put a fake ``openai`` in ``sys.modules`` and record every call made."""
    calls: list[dict] = []
    keys: list[str | None] = []

    def install(outcome):
        module = types.ModuleType("openai")

        def factory(api_key=None):
            keys.append(api_key)
            return Client(outcome, calls, api_key=api_key)

        module.OpenAI = factory
        monkeypatch.setitem(sys.modules, "openai", module)
        return calls, keys

    return install


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    # The env-file loader must not reach a real file during a test.
    monkeypatch.setattr(check, "load_env_file", lambda: "", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "tybot.envfile",
        types.SimpleNamespace(load_env_file=lambda: ""),
    )


def ok_response(count=3, width=2):
    return Response(
        [Row(number, tuple(0.1 for _ in range(width))) for number in range(count)],
        usage=Usage(42),
    )


def run(capsys, argv):
    code = check.main(argv)
    return code, capsys.readouterr().out


def report(capsys, argv):
    code, out = run(capsys, [*argv, "--json"])
    return code, json.loads(out)


# --- plan mode sends nothing ----------------------------------------------------


def test_the_default_run_sends_nothing(sdk, capsys):
    calls, keys = sdk(ok_response())
    code, found = report(capsys, PLAN)

    assert code == 0
    assert found["mode"] == "plan"
    assert found["calls_made"] == 0
    assert found["result"] == "planned"
    # No SDK client was built, so no key was read either.
    assert calls == [] and keys == []


def test_plan_mode_does_not_look_for_a_key(monkeypatch, capsys):
    """A plan must run on a laptop with nothing configured.

    If it needed a key it would fail where there is nothing to fail about, and
    the operator would learn to reach for ``--execute`` to "make it work".
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    looked = []
    monkeypatch.setattr(
        check, "resolve_api_key", lambda *a, **k: looked.append(a) or "x"
    )
    code, found = report(capsys, PLAN)

    assert code == 0 and found["key_checked"] is False
    assert looked == []


def test_plan_mode_never_imports_a_provider_sdk(monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "openai", raising=False)
    report(capsys, PLAN)

    assert "openai" not in sys.modules


# --- execute makes exactly one call ---------------------------------------------


def test_execute_makes_exactly_one_call(sdk, key, capsys):
    calls, _ = sdk(ok_response())
    code, found = report(capsys, [*PLAN, "--execute"])

    assert code == 0
    assert found["result"] == "ok"
    assert found["calls_made"] == 1
    assert len(calls) == 1
    assert calls[0]["model"] == "text-embedding-x"
    assert calls[0]["timeout"] == 60.0


def test_a_failure_is_not_retried(sdk, key, capsys):
    calls, _ = sdk(RuntimeError("400 invalid_request"))
    code, found = report(capsys, [*PLAN, "--execute"])

    assert code == 1
    assert found["code"] == "embedding_call_failed"
    # One attempt. A diagnostic that retries hides the very flakiness it exists
    # to find, and bills twice for it.
    assert len(calls) == 1


def test_the_timeout_reaches_the_sdk(sdk, key, capsys):
    calls, _ = sdk(ok_response())
    report(capsys, [*PLAN, "--execute", "--timeout", "3.5"])

    assert calls[0]["timeout"] == 3.5


def test_the_three_synthetic_sentences_are_what_gets_sent(sdk, key, capsys):
    calls, _ = sdk(ok_response())
    report(capsys, [*PLAN, "--execute"])

    assert calls[0]["input"] == list(check.SYNTHETIC_INPUTS)
    assert 2 <= len(check.SYNTHETIC_INPUTS) <= 3


# --- a mismatch fails with a fixed code -----------------------------------------


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (Response([Row(0, (0.1, 0.2))], usage=Usage(1)), "embedding_result_incomplete"),
        (ok_response(count=3, width=5), "embedding_dimension_mismatch"),
        (ok_response(count=4), "embedding_result_incomplete"),
        (Response([Row(n, (0.1, 0.2)) for n in range(3)], model="other-model"),
         "embedding_identity_mismatch"),
    ],
)
def test_an_answer_that_does_not_match_fails_with_a_fixed_code(
    sdk, key, capsys, response, code
):
    sdk(response)
    exit_code, found = report(capsys, [*PLAN, "--execute"])

    assert exit_code == 1
    assert found["code"] == code
    assert found["code_known"] is True


def test_a_matching_answer_is_reported_as_ok(sdk, key, capsys):
    """Only testing refusals would let a tool that fails everything pass."""
    sdk(ok_response())
    code, found = report(capsys, [*PLAN, "--execute"])

    assert code == 0
    assert found["vectors_returned"] == 3
    assert found["vector_widths"] == [2]
    assert found["identity_matched"] is True


# --- a missing key stops before the call ----------------------------------------


def test_a_missing_key_is_refused_before_anything_is_sent(sdk, monkeypatch, capsys):
    calls, keys = sdk(ok_response())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(
        sys.modules, "tybot.envfile", types.SimpleNamespace(load_env_file=lambda: "")
    )
    code, found = report(capsys, [*PLAN, "--execute"])

    assert code == 1
    assert found["code"] == "smoke_api_key_missing"
    assert calls == [] and keys == []


def test_a_blank_key_counts_as_missing(sdk, monkeypatch, capsys):
    calls, _ = sdk(ok_response())
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    monkeypatch.setitem(
        sys.modules, "tybot.envfile", types.SimpleNamespace(load_env_file=lambda: "")
    )
    code, found = report(capsys, [*PLAN, "--execute"])

    assert code == 1 and found["code"] == "smoke_api_key_missing"
    assert calls == []


def test_an_unsupported_provider_is_refused_before_a_key_is_read(monkeypatch, capsys):
    looked = []
    monkeypatch.setattr(check, "resolve_api_key", lambda *a, **k: looked.append(a))
    code, found = report(
        capsys,
        ["--provider", "anthropic", "--model", "m", "--dimensions", "2", "--execute"],
    )

    assert code == 1
    assert found["code"] == "smoke_provider_unsupported"
    assert looked == []
    # Anthropic serves completions. Routing this through one would return prose
    # shaped like numbers that nothing downstream can tell from a vector.
    assert "anthropic" not in check.SMOKE_PROVIDERS


def test_secret_store_is_an_explicit_supported_key_source(sdk, monkeypatch, capsys):
    calls, keys = sdk(ok_response())
    monkeypatch.setitem(
        sys.modules,
        "tybot.console.llm_secret_store",
        types.SimpleNamespace(resolve_key=lambda provider: KEY if provider == "openai" else None),
    )

    code, found = report(
        capsys, [*PLAN, "--execute", "--key-from", "secret-store"]
    )

    assert code == 0 and found["result"] == "ok"
    assert len(calls) == 1
    assert keys == [KEY]


def test_secret_store_reads_env_file_before_looking_up_the_key(sdk, monkeypatch, capsys):
    """비밀 저장소는 `DATABASE_URL` 을 env 에서 찾는다. env 파일을 안 읽으면 DB 를
    보지도 않고 「키 없음」 이 된다 — 2026-11-09 서버에서 실제로 그랬다."""
    _calls, keys = sdk(ok_response())
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def load_env_file():
        monkeypatch.setenv("DATABASE_URL", "postgresql://from-env-file")
        return "test"

    monkeypatch.setitem(
        sys.modules, "tybot.envfile", types.SimpleNamespace(load_env_file=load_env_file)
    )
    monkeypatch.setitem(
        sys.modules,
        "tybot.console.llm_secret_store",
        types.SimpleNamespace(
            resolve_key=lambda provider: KEY if os.getenv("DATABASE_URL") else None
        ),
    )

    code, found = report(capsys, [*PLAN, "--execute", "--key-from", "secret-store"])

    assert code == 0 and found["result"] == "ok"
    assert keys == [KEY]


def test_secret_store_failure_is_body_free_and_stops_before_call(
    sdk, monkeypatch, capsys
):
    calls, keys = sdk(ok_response())
    private = "private database connection detail"
    monkeypatch.setitem(
        sys.modules,
        "tybot.console.llm_secret_store",
        types.SimpleNamespace(
            resolve_key=lambda _provider: (_ for _ in ()).throw(RuntimeError(private))
        ),
    )

    code, output = run(
        capsys,
        [*PLAN, "--execute", "--key-from", "secret-store", "--json"],
    )

    assert code == 1
    assert "smoke_api_key_missing" in output
    assert private not in output
    assert calls == [] and keys == []


# --- nothing printed can leak ----------------------------------------------------


def test_no_secret_body_or_vector_reaches_the_output(sdk, key, capsys):
    sdk(ok_response())
    _, out = run(capsys, [*PLAN, "--execute", "--json"])

    assert KEY not in out
    for sentence in check.SYNTHETIC_INPUTS:
        assert sentence not in out
    # Vector values are derived from the bodies; only their shape is reported.
    assert "0.1" not in out


def test_no_provider_error_body_reaches_the_output(sdk, key, capsys):
    sdk(RuntimeError("400 invalid_request: offending input was 'secret body'"))
    _, out = run(capsys, [*PLAN, "--execute"])

    assert "invalid_request" not in out
    assert "secret body" not in out
    assert "embedding_call_failed" in out


def test_the_plan_output_carries_no_secret(sdk, key, capsys):
    sdk(ok_response())
    _, out = run(capsys, PLAN)

    assert KEY not in out
    for sentence in check.SYNTHETIC_INPUTS:
        assert sentence not in out


# --- cost: unknown is None, never zero -------------------------------------------


def test_an_unpriced_run_leaves_the_cost_unconfirmed(sdk, key, capsys):
    sdk(ok_response())
    _, found = report(capsys, [*PLAN, "--execute"])

    assert found["cost_confirmed"] is False
    assert found["cost_usd"] is None


def test_a_priced_run_with_usage_reports_a_cost(sdk, key, capsys):
    sdk(ok_response())
    _, found = report(capsys, [*PLAN, "--execute", "--price-per-mtok", "0.02"])

    assert found["cost_confirmed"] is True
    assert found["cost_usd"] == pytest.approx(42 / 1_000_000 * 0.02)


def test_a_priced_run_without_usage_stays_unconfirmed(sdk, key, capsys):
    sdk(Response([Row(n, (0.1, 0.2)) for n in range(3)], usage=None))
    _, found = report(capsys, [*PLAN, "--execute", "--price-per-mtok", "0.02"])

    assert found["cost_confirmed"] is False
    assert found["cost_usd"] is None


# --- arguments --------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--provider", "openai", "--model", "m", "--dimensions", "0"],
        ["--provider", "openai", "--model", "m", "--dimensions", "16001"],
        ["--provider", "openai", "--model", " m", "--dimensions", "2"],
        ["--provider", "openai\nforged", "--model", "m", "--dimensions", "2"],
        ["--provider", "openai", "--model", "m\nforged", "--dimensions", "2"],
        ["--provider", "openai", "--model", "m", "--dimensions", "2", "--timeout", "0"],
        ["--provider", "openai", "--model", "m", "--dimensions", "2",
         "--price-per-mtok", "-1"],
    ],
)
def test_unusable_arguments_are_refused(argv, capsys):
    code, found = report(capsys, argv)

    assert code == 1
    assert found["code"] == "smoke_arguments_invalid"


@pytest.mark.parametrize("missing", ["--provider", "--model", "--dimensions"])
def test_every_unsafe_choice_must_be_named(missing):
    """No defaults for what gets sent where. The command line is the record."""
    argv = [item for item in PLAN]
    index = argv.index(missing)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        check.main(argv)


def test_an_unknown_smoke_code_cannot_be_raised():
    with pytest.raises(ValueError, match="unknown smoke failure code"):
        check.SmokeRefusal("smoke_went_wrong")


def test_the_two_code_families_do_not_overlap():
    """``smoke_*`` means it never left; the rest means the provider answered."""
    from tybot.embedding_index_job import EMBEDDING_FAILURE_OUTCOMES

    assert not (check.SMOKE_FAILURE_CODES & EMBEDDING_FAILURE_OUTCOMES)


# --- the tool reads no archive, log, or source database -----------------------------


def test_the_tool_never_opens_the_archive_or_the_source_database():
    source = (ROOT / "scripts" / "check_embedding_provider.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "ArchiveStore",
        "psycopg",
        "qa-issues",
        "archive_store",
        "iter_index_sources",
        "db_bindings",
    ):
        assert forbidden not in source
    # The bodies are literals in this file, not something read at runtime.
    assert all(isinstance(text, str) for text in check.SYNTHETIC_INPUTS)
