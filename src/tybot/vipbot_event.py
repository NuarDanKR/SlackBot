"""Pure VIPBot Slack event coordinator.

This module does not create a Slack app, open the archive, or construct an LLM.
Those dependencies are injected by the eventual Socket Mode runtime.  Its job is
to keep the security-sensitive order explicit: validate coordinates, authorize,
generate only for an allowed caller, and deliver through the DM-only policy.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from tybot.access import RequestContext
from tybot.bot_access import CHECK_FAILED
from tybot.vip_access import Authorization
from tybot.vipbot_delivery import (
    DeliveryResult,
    deliver_channel_answer,
    deliver_dm_answer,
)

T = TypeVar("T")

_WORKSPACE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_CHANNEL = re.compile(r"^[CDG][A-Z0-9]{6,}$")


@dataclass(frozen=True)
class EventResult:
    outcome: str
    reason: str = ""
    delivery: DeliveryResult | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"ignored", "handled", "failed"}:
            raise ValueError("unknown event outcome")
        if (self.outcome == "handled") != (self.delivery is not None):
            raise ValueError("delivery must match the event outcome")
        if self.reason not in {
            "",
            "invalid_event",
            "duplicate_event",
            "claim_failed",
            "authorization_failed",
            "authorization_changed",
            "generation_failed",
            "missing_sources",
        }:
            raise ValueError("unknown event reason")


def _question(event: Mapping[str, object], *, bot_user_id: str) -> str:
    text = event.get("text")
    if not isinstance(text, str):
        return ""
    channel = str(event.get("channel") or "")
    if channel.startswith("D"):
        return text.strip()
    mention = f"<@{bot_user_id}>"
    if not text.lstrip().startswith(mention):
        return ""
    return text.lstrip()[len(mention) :].strip()


def _valid_event(event: Mapping[str, object]) -> bool:
    channel = str(event.get("channel") or "")
    event_type = event.get("type")
    if channel.startswith("D"):
        return event_type == "message" and event.get("channel_type") == "im"
    return event_type == "app_mention" and channel.startswith(("C", "G"))


def _bound(decision: Authorization, *, workspace: str, slack_user: str) -> bool:
    return (
        type(decision) is Authorization
        and decision.allowed is True
        and decision.code == "allowed"
        and decision.workspace == workspace
        and decision.slack_user == slack_user
        and bool(decision.emp_no)
        and type(decision.grant_id) is int
        and decision.grant_id > 0
        and bool(decision.sources)
    )


def _same_scope(before: Authorization, after: Authorization) -> bool:
    return (
        _bound(after, workspace=before.workspace, slack_user=before.slack_user)
        and after.emp_no == before.emp_no
        and after.grant_id == before.grant_id
        and after.sources == before.sources
    )


def _failed_authorization(workspace: str, slack_user: str) -> Authorization:
    return Authorization(False, CHECK_FAILED, workspace, slack_user)


def _record_delivered(callback, question: str, answer, context: RequestContext) -> bool:
    try:
        callback(question, answer, context)
    except Exception:  # noqa: BLE001 - history failure must not undo delivery
        return False
    return True


def handle_event(
    event: Mapping[str, object],
    client,
    *,
    workspace: str,
    bot_user_id: str,
    authorize: Callable[..., Authorization],
    generate: Callable[[str, RequestContext], T],
    render: Callable[[T], str],
    event_id: str,
    claim_event: Callable[[str], bool],
    record: Callable[[str, T, RequestContext], object] | None = None,
) -> EventResult:
    """Handle one mention or DM while preserving authorization-before-generation."""
    if event.get("bot_id") or event.get("subtype") or not _valid_event(event):
        return EventResult("ignored", "invalid_event")

    normalized_workspace = str(workspace or "").strip().lower()
    slack_user = str(event.get("user") or "").strip().upper()
    channel = str(event.get("channel") or "").strip().upper()
    bot_user = str(bot_user_id or "").strip().upper()
    if (
        not _WORKSPACE.fullmatch(normalized_workspace)
        or not _SLACK_USER.fullmatch(slack_user)
        or not _CHANNEL.fullmatch(channel)
        or not _SLACK_USER.fullmatch(bot_user)
    ):
        return EventResult("ignored", "invalid_event")

    question = _question(event, bot_user_id=bot_user)
    if not question:
        return EventResult("ignored", "invalid_event")

    stable_event_id = str(event_id or "").strip()
    if not stable_event_id:
        return EventResult("ignored", "invalid_event")
    try:
        claimed = claim_event(stable_event_id)
    except Exception:  # noqa: BLE001 - claim storage details must not escape
        return EventResult("failed", "claim_failed")
    if not claimed:
        return EventResult("ignored", "duplicate_event")

    try:
        authorization = authorize(
            workspace=normalized_workspace,
            slack_user=slack_user,
        )
    except Exception:  # noqa: BLE001 - authorization details must not escape
        authorization = _failed_authorization(normalized_workspace, slack_user)
    answer = None
    generated = None
    generated_context = None
    if _bound(authorization, workspace=normalized_workspace, slack_user=slack_user):
        try:
            generated_context = authorization.context()
            value = generate(question, generated_context)
            citations = getattr(value, "citations", None)
            if not isinstance(citations, (list, tuple)) or not any(
                isinstance(item, str) and item.strip() for item in citations
            ):
                return EventResult("failed", "missing_sources")
            answer = str(render(value) or "")
            if not answer.strip():
                return EventResult("failed", "generation_failed")
            generated = value
        except Exception:  # noqa: BLE001 - question/model output must not escape
            return EventResult("failed", "generation_failed")

        try:
            current = authorize(
                workspace=normalized_workspace,
                slack_user=slack_user,
            )
        except Exception:  # noqa: BLE001 - authorization details must not escape
            current = _failed_authorization(normalized_workspace, slack_user)
        if not _same_scope(authorization, current):
            authorization = _failed_authorization(normalized_workspace, slack_user)
            answer = None
    elif authorization.allowed:
        authorization = _failed_authorization(normalized_workspace, slack_user)

    def check():
        return authorization

    if channel.startswith("D"):
        delivery = deliver_dm_answer(
            client,
            workspace=normalized_workspace,
            channel=channel,
            slack_user=slack_user,
            answer=answer,
            check_access=check,
        )
    else:
        delivery = deliver_channel_answer(
            client,
            workspace=normalized_workspace,
            channel=channel,
            slack_user=slack_user,
            answer=answer,
            check_access=check,
            thread_ts=str(event.get("thread_ts") or event.get("ts") or "") or None,
        )
    answer_delivered = delivery.outcome in {"delivered", "channel_notice_failed"}
    if answer_delivered and generated is not None and generated_context is not None and record:
        _record_delivered(record, question, generated, generated_context)
    return EventResult("handled", delivery=delivery)


__all__ = ["EventResult", "handle_event"]
