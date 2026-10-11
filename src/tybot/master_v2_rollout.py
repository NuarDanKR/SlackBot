"""Workspace-scoped rollout gate for the external-specialist master router.

The new orchestration path changes who writes business answers. It must never be
enabled repository-wide by merely deploying code. Operators opt in one workspace
at a time with ``TYBOT_MASTER_V2_WORKSPACES``.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

ENV_NAME = "TYBOT_MASTER_V2_WORKSPACES"
_WORKSPACE_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class RolloutConfigError(ValueError):
    """The rollout allowlist is malformed and must stay disabled."""


def enabled_workspaces(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return explicitly enabled workspace keys.

    A wildcard is deliberately unsupported. A global rollout must be represented
    by an explicit reviewed list so adding a workspace cannot silently opt it in.
    """
    source = os.environ if env is None else env
    raw = str(source.get(ENV_NAME, "") or "").strip()
    if not raw:
        return frozenset()

    keys: set[str] = set()
    for item in raw.split(","):
        key = item.strip().lower()
        if not key or key == "*" or not _WORKSPACE_KEY.fullmatch(key):
            raise RolloutConfigError(
                f"{ENV_NAME} contains an invalid workspace key: {item!r}"
            )
        keys.add(key)
    return frozenset(keys)


def is_enabled(workspace: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether the v2 master path is enabled for this exact workspace."""
    return workspace.strip().lower() in enabled_workspaces(env)


__all__ = ["ENV_NAME", "RolloutConfigError", "enabled_workspaces", "is_enabled"]
