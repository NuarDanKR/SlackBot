"""Server-owned selection of isolated or shared Hermes RPC endpoints."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .master_v2_rollout import ENV_NAME, RolloutConfigError, enabled_workspaces
from .specialist_v3_client import socket_path

SHARED_ENV_NAME = "TYBOT_HERMES_SHARED_WORKSPACES"


@dataclass(frozen=True)
class RpcEndpoint:
    socket: str
    credential_workspace: str


@dataclass(frozen=True)
class EndpointPolicy:
    shared_workspaces: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EndpointPolicy:
        source = os.environ if env is None else env
        shared = enabled_workspaces({ENV_NAME: source.get(SHARED_ENV_NAME, "")})
        if not shared <= enabled_workspaces(source):
            raise RolloutConfigError(
                f"{SHARED_ENV_NAME} must be a subset of {ENV_NAME}"
            )
        return cls(shared)

    def for_request(self, specialist: str, workspace: str) -> RpcEndpoint:
        # Validate the actual request keys even when the transport uses shared.
        isolated = socket_path(specialist, workspace)
        if specialist == "hermes" and workspace in self.shared_workspaces:
            return RpcEndpoint(socket_path(specialist, "shared"), "shared")
        return RpcEndpoint(isolated, workspace)
