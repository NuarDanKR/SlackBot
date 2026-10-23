"""Bound the complete text sent to a specialist model, not just each tool result."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tybot.gateway.base import LLMResponse, Message, ToolCall, ToolSpec
from tybot.specialist_adapters import (
    MAX_MODEL_TEXT_CHARS,
    AdapterError,
    ToolSpecialist,
    _model_text_chars,
    _require_model_text_room,
)
from tybot.specialist_contract import AuthorizedEvidence, SpecialistRequest
from tybot.specialist_tools import ToolBox


def test_request_size_counts_seed_tool_results_and_tool_schemas():
    tools = (ToolSpec("search", "find records", {"type": "object"}),)
    messages = [
        Message("system", "rules"),
        Message("user", "seed" * 10),
        Message("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "result"}]),
    ]

    assert _model_text_chars(messages, tools) > _model_text_chars(messages)
    assert _model_text_chars(messages) >= 5 + 40 + 6


def test_oversized_request_is_refused_before_model_cost_is_incurred():
    messages = [Message("user", "x" * (MAX_MODEL_TEXT_CHARS + 1))]

    with pytest.raises(AdapterError, match="model-input-too-large"):
        _require_model_text_room(messages)


def test_oversized_edit_never_calls_the_model():
    class Router:
        calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("oversized input reached provider")

    router = Router()
    adapter = ToolSpecialist("hermes", router, toolbox=ToolBox(None, None), rules="rules")
    request = SimpleNamespace(
        question="reformat", editing_text="x" * (MAX_MODEL_TEXT_CHARS + 1), deadline=None,
    )

    with pytest.raises(AdapterError, match="model-input-too-large"):
        adapter.complete(request)
    assert router.calls == 0


def test_tool_reserve_keeps_seed_and_new_result_inside_request_envelope(monkeypatch):
    box = ToolBox(store=None, ctx=None)
    monkeypatch.setattr(box, "_dispatch", lambda _name, _args: "x" * 30_000)
    adapter = ToolSpecialist("hermes", router=None, toolbox=box, rules="rules")
    messages = [Message("system", "s" * 40_000), Message("user", "e" * 40_000)]

    reserve = adapter._tool_reserve(messages, [])
    result, status = adapter._run_tool("search", {"query": "site"}, reserve_chars=reserve)
    next_messages = [*messages, Message("user", [
        {"type": "tool_result", "tool_use_id": "c1", "content": result},
    ])]

    assert reserve > 8_000
    assert status == "truncated"
    assert _model_text_chars(next_messages) <= MAX_MODEL_TEXT_CHARS
    _require_model_text_room(next_messages)
    assert not box.touched.evidence_hits


def test_image_payload_is_not_mistaken_for_converted_text():
    messages = [Message("user", [
        {"type": "text", "text": "question"},
        {"type": "image", "source": {"data": "x" * 200_000}},
    ])]

    assert _model_text_chars(messages) == len("textquestion")
    _require_model_text_room(messages)


def test_large_converted_text_never_reaches_model_as_oversized_request(monkeypatch):
    class Router:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, tools=(), **_kwargs):
            _require_model_text_room(messages, tools)
            self.calls += 1
            if self.calls <= 3:
                name = ("request_search" if self.calls == 1 else
                        "search" if self.calls == 2 else "read_document")
                args = {"query": f"q{self.calls}"} if name != "read_document" else {"name": "file"}
                return LLMResponse(
                    "", "model", "provider", 10, 10, 0,
                    tool_calls=(ToolCall(id=f"c{self.calls}", name=name, input=args),),
                )
            return LLMResponse("bounded answer", "model", "provider", 10, 10, 0)

    box = ToolBox(store=None, ctx=None)
    monkeypatch.setattr(box, "_dispatch", lambda _name, _args: "x" * 30_000)
    router = Router()
    adapter = ToolSpecialist("hermes", router, toolbox=box, rules="rules")
    request = SpecialistRequest(
        question="compare records",
        evidence=(AuthorizedEvidence.from_acl_filter(
            workspace="tyit", text="e" * 40_000, authorization_id="scope-1",
        ),),
    )

    assert adapter.complete(request) == "bounded answer"
    assert router.calls == 4
    assert box.budget.calls == 3
    assert box.budget.chars < box.budget.max_chars
