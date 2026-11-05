from __future__ import annotations

from tybot import bot_access, vip_access, vip_request
from tybot.access import RequestContext


def _authorization(*, allowed=True, code=vip_access.ALLOWED):
    return vip_access.Authorization(
        allowed,
        code,
        "tyit",
        "U1",
        emp_no="E1",
        grant_id=7,
        sources=(
            frozenset({("mgmt", "C0EXEC001")}) if allowed else frozenset()
        ),
    )


def test_authorization_happens_before_any_answer_work(monkeypatch):
    order: list[str] = []

    def authorize(**_kwargs):
        order.append("authorize")
        return _authorization()

    def generate(ctx: RequestContext):
        order.append("generate")
        assert ctx.evidence_sources == {("mgmt", "C0EXEC001")}
        return "answer"

    monkeypatch.setattr(vip_request, "authorize_slack", authorize)
    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=generate,
    )
    assert order == ["authorize", "generate"]
    assert got.generated and got.value == "answer"


def test_denied_user_never_reaches_search_or_llm_callback(monkeypatch):
    monkeypatch.setattr(
        vip_request,
        "authorize_slack",
        lambda **_kwargs: _authorization(
            allowed=False, code=bot_access.NO_GRANT
        ),
    )

    def forbidden(_ctx):  # pragma: no cover - calling it is the regression
        raise AssertionError("answer generation ran before VIP authorization")

    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=forbidden,
    )
    assert not got.generated and got.value is None
    assert got.authorization.code == bot_access.NO_GRANT


def test_database_failure_never_reaches_answer_callback(monkeypatch):
    monkeypatch.setattr(
        vip_request,
        "authorize_slack",
        lambda **_kwargs: _authorization(
            allowed=False, code=bot_access.CHECK_FAILED
        ),
    )
    called = False

    def generate(_ctx):
        nonlocal called
        called = True

    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=generate,
    )
    assert not called
    assert got.authorization.code == bot_access.CHECK_FAILED


def test_missing_sources_never_reaches_answer_callback(monkeypatch):
    monkeypatch.setattr(
        vip_request,
        "authorize_slack",
        lambda **_kwargs: _authorization(
            allowed=False, code=vip_access.NO_SOURCES
        ),
    )
    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=lambda _ctx: "must not run",
    )
    assert not got.generated and got.authorization.code == vip_access.NO_SOURCES


def test_answer_failure_propagates_without_becoming_a_permission_decision(monkeypatch):
    monkeypatch.setattr(
        vip_request, "authorize_slack", lambda **_kwargs: _authorization()
    )

    def broken(_ctx):
        raise RuntimeError("answer_failed")

    try:
        vip_request.run_authorized(
            connect=object(),
            client=object(),
            workspace="tyit",
            slack_user="U1",
            generate=broken,
        )
    except RuntimeError as exc:
        assert str(exc) == "answer_failed"
    else:  # pragma: no cover
        raise AssertionError("answer failure was hidden as an access result")


def test_empty_answer_is_not_reported_as_generated(monkeypatch):
    monkeypatch.setattr(
        vip_request, "authorize_slack", lambda **_kwargs: _authorization()
    )
    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=lambda _ctx: "   ",
    )
    assert not got.generated


def test_answer_object_with_empty_text_is_not_reported_as_generated(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        vip_request, "authorize_slack", lambda **_kwargs: _authorization()
    )
    got = vip_request.run_authorized(
        connect=object(),
        client=object(),
        workspace="tyit",
        slack_user="U1",
        generate=lambda _ctx: SimpleNamespace(text="", reason="error"),
    )
    assert not got.generated
