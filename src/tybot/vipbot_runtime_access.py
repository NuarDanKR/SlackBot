"""Read VIPBot authorization through the dedicated database capability only."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from . import bot_access
from .vip_access import ALLOWED, NO_SOURCES, Authorization

logger = logging.getLogger("tybot.vipbot_runtime_access")

_WORKSPACE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_DENIED_CODES = frozenset(
    {
        bot_access.NO_IDENTITY,
        bot_access.UNKNOWN_EMPLOYEE,
        bot_access.INACTIVE_EMPLOYEE,
        bot_access.NO_GRANT,
        bot_access.EXPIRED,
        NO_SOURCES,
    }
)


def _failed(workspace: str, slack_user: str) -> Authorization:
    return Authorization(False, bot_access.CHECK_FAILED, workspace, slack_user)


def _grant_id(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def authorize_runtime(
    connect: Callable[[], object], *, workspace: str, slack_user: str
) -> Authorization:
    """Return a closed authorization decision from ``vipbot_authorization``.

    The function accepts coordinates only. Questions, answers, and evidence text
    have no parameter and therefore cannot enter the authorization database path.
    """
    workspace = str(workspace or "").strip().lower()
    slack_user = str(slack_user or "").strip().upper()
    if not _WORKSPACE.fullmatch(workspace) or not _SLACK_USER.fullmatch(slack_user):
        return _failed(workspace, slack_user)

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM public.vipbot_authorization(%s, %s)",
                (workspace, slack_user),
            )
            rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - authorization failure is denial
        logger.warning(
            "[%s] VIPBot runtime authorization failed user=%s code=%s",
            workspace,
            slack_user,
            type(exc).__name__,
        )
        return _failed(workspace, slack_user)

    if not rows:
        return _failed(workspace, slack_user)
    code = str(rows[0].get("code") or "")
    employee = str(rows[0].get("emp_no") or "")
    raw_grant_id = rows[0].get("grant_id")
    if any(
        str(row.get("code") or "") != code
        or str(row.get("emp_no") or "") != employee
        or row.get("grant_id") != raw_grant_id
        for row in rows[1:]
    ):
        return _failed(workspace, slack_user)

    emp_no = employee or None
    grant_id = _grant_id(raw_grant_id)
    if code in _DENIED_CODES:
        if len(rows) != 1 or any(
            row.get("source_workspace") is not None or row.get("channel_id") is not None
            for row in rows
        ):
            return _failed(workspace, slack_user)
        return Authorization(
            False,
            code,
            workspace,
            slack_user,
            emp_no=emp_no,
            grant_id=grant_id,
        )
    if code != ALLOWED or not emp_no or grant_id is None:
        return _failed(workspace, slack_user)

    sources: set[tuple[str, str]] = set()
    for row in rows:
        source_workspace = str(row.get("source_workspace") or "")
        channel_id = str(row.get("channel_id") or "")
        if not _WORKSPACE.fullmatch(source_workspace) or not re.fullmatch(
            r"[CG][A-Z0-9]{6,}", channel_id
        ):
            return _failed(workspace, slack_user)
        sources.add((source_workspace, channel_id))
    if not sources:
        return _failed(workspace, slack_user)
    return Authorization(
        True,
        ALLOWED,
        workspace,
        slack_user,
        emp_no=emp_no,
        grant_id=grant_id,
        sources=frozenset(sources),
    )


__all__ = ["authorize_runtime"]
