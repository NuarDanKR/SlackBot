"""Safe, aggregate-only runtime status for the B-81 image worker."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CODE = re.compile(r"^[a-z0-9_]*$")
SCHEMA = "tybot.archiver.image-description-runtime.v1"


@dataclass(frozen=True)
class ImageDescriptionRuntimeStatus:
    workspace: str
    state: str
    ran_at: str
    policy_revision: str = ""
    selected: int = 0
    skipped: int = 0
    published: int = 0
    blocked: int = 0
    pending_approval: int = 0
    unknown: int = 0
    error_code: str = ""
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if not _WORKSPACE.fullmatch(self.workspace):
            raise ValueError("workspace_invalid")
        if self.state not in {"disabled", "ok", "partial", "failed"}:
            raise ValueError("state_invalid")
        if not _CODE.fullmatch(self.error_code):
            raise ValueError("error_code_invalid")
        if self.schema != SCHEMA:
            raise ValueError("schema_invalid")
        for value in (
            self.selected,
            self.skipped,
            self.published,
            self.blocked,
            self.pending_approval,
            self.unknown,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("count_invalid")
        stamp = datetime.fromisoformat(self.ran_at)
        if stamp.utcoffset() is None:
            raise ValueError("ran_at_invalid")


def status_path(root: Path | str, workspace: str) -> Path:
    if not _WORKSPACE.fullmatch(workspace):
        raise ValueError("workspace_invalid")
    return Path(root) / "image-description" / "status" / f"{workspace}.json"


def write_status(root: Path | str, status: ImageDescriptionRuntimeStatus) -> Path:
    path = status_path(root, status.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    body = json.dumps(asdict(status), ensure_ascii=True, sort_keys=True) + "\n"
    try:
        temporary.write_text(body, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def failed_status(workspace: str, code: str) -> ImageDescriptionRuntimeStatus:
    return ImageDescriptionRuntimeStatus(
        workspace=workspace,
        state="failed",
        ran_at=datetime.now(UTC).isoformat(),
        error_code=code if _CODE.fullmatch(code) else "runtime_failed",
    )


__all__ = [
    "SCHEMA",
    "ImageDescriptionRuntimeStatus",
    "failed_status",
    "status_path",
    "write_status",
]
