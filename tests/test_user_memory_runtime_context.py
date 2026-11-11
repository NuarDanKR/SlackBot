from types import SimpleNamespace
from unittest.mock import Mock

from tybot.access import RequestContext
from tybot.answer import (
    AnswerEngine,
    AnswerPrompts,
    _response_cache_scope,
    _with_user_memory,
)
from tybot.slack.pilot import WorkspaceBot
from tybot.user_memory import CLAIM, MemoryContext, UserMemory


def _claim() -> UserMemory:
    return UserMemory(
        id=17,
        workspace="tyit",
        user_id="U1",
        kind=CLAIM,
        body="확정 비율은 12%",
        normalized="확정 비율은 12%",
        search_terms=("확정 금액",),
        evidence_refs=(
            {
                "kind": "archive_line",
                "workspace": "tyit",
                "channel_id": "C1",
                "document_path": "workspaces/tyit/channels/C1/raw/2026-11-11.md",
                "line_no": 3,
                "content_hash": "abc",
            },
        ),
    )


def _bot(service, store) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.store = store
    bot._user_memory_service_obj = service
    return bot


def test_current_claim_is_reopened_before_personal_context_is_attached(monkeypatch):
    monkeypatch.setattr(
        "tybot.user_memory.claim_matches_evidence", lambda _memory, _lines: True
    )
    service = Mock()
    store = Mock()
    store.resolve_refs.return_value = (
        [SimpleNamespace(line=SimpleNamespace(text="확정 비율은 12%"))],
        [],
    )

    def context(**kwargs):
        assert kwargs["include_search_hints"] is False
        assert kwargs["claim_is_current"](_claim())
        return MemoryContext(prompt="<user_memory>확인됨</user_memory>", memory_ids=(17,))

    service.context.side_effect = context
    ctx = RequestContext(workspace="tyit", user_id="U1")

    got = _bot(service, store)._with_user_memory_context(
        ctx, user_id="U1", question="확정 금액 알려줘"
    )

    assert got.user_memory_prompt == "<user_memory>확인됨</user_memory>"
    assert got.user_memory_ids == (17,)
    refs = store.resolve_refs.call_args.args[0]
    assert len(refs) == 1 and refs[0].workspace == "tyit"
    assert store.resolve_refs.call_args.args[1] is ctx


def test_dropped_claim_coordinate_cannot_enter_the_request_context():
    service = Mock()
    store = Mock()
    store.resolve_refs.return_value = ([], ["permission_changed"])

    def context(**kwargs):
        assert not kwargs["claim_is_current"](_claim())
        return MemoryContext()

    service.context.side_effect = context
    ctx = RequestContext(workspace="tyit", user_id="U1")

    got = _bot(service, store)._with_user_memory_context(
        ctx, user_id="U1", question="확정 금액 알려줘"
    )

    assert got is ctx
    assert got.user_memory_prompt == ""


def test_personal_memory_changes_the_system_prompt_and_response_cache_scope():
    plain = RequestContext(workspace="tyit", user_id="U1")
    remembered = RequestContext(
        workspace="tyit",
        user_id="U1",
        user_memory_prompt="<user_memory>간단히 답변</user_memory>",
        user_memory_ids=(4,),
    )

    assert _with_user_memory("base", plain) == "base"
    assert "간단히 답변" in _with_user_memory("base", remembered)
    assert _response_cache_scope(plain, [], ".", namespace="answer") != (
        _response_cache_scope(remembered, [], ".", namespace="answer")
    )


def test_specialist_rendering_receives_the_same_bounded_personal_context():
    router = Mock()
    router.complete.return_value = SimpleNamespace(
        text="전문가 원문",
        model="render-model",
        cost_usd=0.1,
    )
    engine = AnswerEngine(
        store=None,
        router=router,
        prompts=AnswerPrompts(specialist="executive format"),
    )

    rendered = engine._render_specialist_answer(
        "전문가 원문",
        "질문",
        model=None,
        source_model="specialist-model",
        source_cost_usd=0.2,
        user_memory_prompt="<user_memory>간단히 답변</user_memory>",
    )

    assert rendered[0] == "전문가 원문"
    messages = router.complete.call_args.args[0]
    assert "executive format" in messages[0].content
    assert "간단히 답변" in messages[0].content
