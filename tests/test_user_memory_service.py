from contextlib import contextmanager
from unittest.mock import Mock, patch

from tybot.user_memory import MemoryContext, MemoryDecision, UserMemory
from tybot.user_memory_service import UserMemoryService


@contextmanager
def connected(value):
    yield value


def prior(**overrides):
    row = {
        "record_id": "qa-1",
        "workspace": "tyit",
        "user": "U1",
        "channel_id": "D1",
        "thread_ts": "100.0",
        "evidence_refs": [{"kind": "archive_line"}],
    }
    row.update(overrides)
    return row


def test_feedback_requires_exact_workspace_and_user_lineage():
    service = UserMemoryService(lambda: connected(Mock()))

    wrong_user = service.remember(
        "앞으로 짧게 답해줘",
        workspace="tyit",
        user_id="U2",
        prior=prior(),
    )
    wrong_workspace = service.remember(
        "앞으로 짧게 답해줘",
        workspace="civil",
        user_id="U1",
        prior=prior(),
    )

    assert not wrong_user.saved
    assert wrong_user.decision.reason == "no_prior_turn"
    assert not wrong_workspace.saved
    assert wrong_workspace.decision.reason == "no_prior_turn"


def test_feedback_saves_only_coordinates_from_the_owned_prior_turn():
    connection = Mock()
    decision = MemoryDecision(
        kind="preference",
        body="format.length=short",
        normalized="format.length=short",
    )
    with (
        patch("tybot.user_memory_service.classify_feedback", return_value=decision),
        patch("tybot.user_memory_service.Store") as store_type,
    ):
        store_type.return_value.save.return_value = 17
        result = UserMemoryService(lambda: connected(connection)).remember(
            "앞으로 짧게 답해줘",
            workspace="tyit",
            user_id="U1",
            prior=prior(),
        )

    assert result.saved
    assert result.memory_id == 17
    store_type.return_value.save.assert_called_once_with(
        decision,
        workspace="tyit",
        user_id="U1",
        source_record_id="qa-1",
        source_channel_id="D1",
        source_thread_ts="100.0",
        evidence_refs=[{"kind": "archive_line"}],
    )


def test_missing_database_does_not_claim_that_memory_was_saved():
    result = UserMemoryService(lambda: connected(None)).remember(
        "앞으로 짧게 답해줘",
        workspace="tyit",
        user_id="U1",
        prior=prior(),
    )

    assert result.decision.stores
    assert result.unavailable
    assert not result.saved


def test_context_uses_exact_user_scope_and_callers_current_evidence_check():
    connection = Mock()
    claim = UserMemory(
        id=9,
        workspace="tyit",
        user_id="U1",
        kind="claim",
        body="확정 금액은 12억",
        normalized="claim",
        evidence_refs=({"kind": "archive_line"},),
    )
    expected = MemoryContext(prompt="checked", memory_ids=(9,))
    checker = Mock(return_value=True)
    with (
        patch("tybot.user_memory_service.Store") as store_type,
        patch("tybot.user_memory_service.build_context", return_value=expected) as build,
    ):
        store_type.return_value.active.return_value = [claim]
        got = UserMemoryService(lambda: connected(connection)).context(
            workspace="tyit",
            user_id="U1",
            question="확정 금액",
            claim_is_current=checker,
        )

    assert got == expected
    store_type.return_value.active.assert_called_once_with("tyit", "U1")
    build.assert_called_once_with(
        [claim],
        question="확정 금액",
        claim_is_current=checker,
        include_search_hints=True,
    )
    store_type.return_value.mark_used.assert_called_once_with(
        (9,), workspace="tyit", user_id="U1"
    )


def test_context_and_revoke_fail_closed_when_storage_is_unavailable():
    service = UserMemoryService(lambda: connected(None))

    assert service.context(
        workspace="tyit",
        user_id="U1",
        question="확정 금액",
        claim_is_current=lambda _memory: True,
    ) == MemoryContext()
    assert not service.revoke(1, workspace="tyit", user_id="U1", actor="U1")
    assert service.list_active(workspace="tyit", user_id="U1").unavailable
    assert service.revoke_result(
        1, workspace="tyit", user_id="U1", actor="U1"
    ).unavailable


def test_list_and_revoke_are_scoped_to_the_exact_user():
    connection = Mock()
    remembered = UserMemory(
        id=17,
        workspace="tyit",
        user_id="U1",
        kind="preference",
        body="format.length=short",
        normalized="format.length=short",
    )
    with patch("tybot.user_memory_service.Store") as store_type:
        store_type.return_value.active.return_value = [remembered]
        store_type.return_value.revoke.return_value = True
        service = UserMemoryService(lambda: connected(connection))

        listed = service.list_active(workspace="tyit", user_id="U1")
        revoked = service.revoke_result(
            17, workspace="tyit", user_id="U1", actor="U1"
        )

    assert listed.memories == (remembered,)
    assert revoked.revoked
    store_type.return_value.active.assert_called_once_with("tyit", "U1")
    store_type.return_value.revoke.assert_called_once_with(
        17, workspace="tyit", user_id="U1", actor="U1"
    )
