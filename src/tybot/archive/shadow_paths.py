"""Channel-scoped shadow layout. Live archives keep their existing paths."""

from __future__ import annotations

import re
from pathlib import Path


class ShadowPathError(ValueError):
    pass


def _component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣()_-]+", "_", value.strip().lstrip("#"))
    return cleaned.strip("_")[:80] or "unnamed"


def channel_root(root: Path | str, workspace: str, channel_id: str, name: str) -> Path:
    """Resolve the first channel directory by ID, regardless of later renames."""
    if not re.fullmatch(r"(?:[CG][A-Z0-9]{8,}|legacy-[a-f0-9]{16})", channel_id):
        raise ShadowPathError("invalid channel ID for shadow layout")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace):
        raise ShadowPathError("invalid workspace key for shadow layout")
    base = Path(root) / workspace
    if not base.resolve().is_relative_to(Path(root).resolve()):
        raise ShadowPathError("shadow workspace directory escaped its root")
    matches = sorted(path for path in base.glob(f"{channel_id}__*") if path.is_dir())
    if len(matches) > 1:
        raise ShadowPathError("multiple shadow directories claim one channel ID")
    if matches and matches[0].is_symlink():
        raise ShadowPathError("shadow channel directory is a symlink")
    return matches[0] if matches else base / f"{channel_id}__{_component(name)}"


def existing_channel_root(root: Path | str, workspace: str, channel_id: str) -> Path:
    """Find a previously written channel without inventing a new display name."""
    candidate = channel_root(root, workspace, channel_id, channel_id)
    if not candidate.is_dir():
        raise ShadowPathError("shadow channel directory is missing")
    return candidate


def archive_dir(root: Path | str, workspace: str, channel_id: str, name: str) -> Path:
    return channel_root(root, workspace, channel_id, name) / "archive"


# --- Private per-user DM namespace (B-68) -----------------------------------
#
# A DM lives outside channel enumeration so that "excluded by default" is a
# property of the path, not of a filter someone has to remember. Channel
# consumers glob `<workspace>/<channel-id>__<name>/`; none of them reach
# `<workspace>/dm/<user-id>/`.

#: Slack IM conversation. `mpim` and channels fail closed: a group DM is not a
#: 1:1 DM, and this layout keys on one person.
DM_CHANNEL_ID = re.compile(r"D[A-Z0-9]{7,}")

#: Slack user. `U` is a member, `W` an Enterprise Grid member.
DM_USER_ID = re.compile(r"[UW][A-Z0-9]{7,}")


def dm_root(
    root: Path | str, workspace: str, user_id: str, dm_channel_id: str
) -> Path:
    """Resolve one person's DM directory, refusing anything that is not a 1:1 DM.

    The channel ID is checked even though it is not part of the path. A `D`
    prefix is the only evidence this layout can carry that the conversation was
    an IM; without the check a group DM would land in one member's private
    namespace and the other members would never appear.
    """
    if not DM_USER_ID.fullmatch(user_id):
        raise ShadowPathError("invalid Slack user ID for the DM layout")
    if not DM_CHANNEL_ID.fullmatch(dm_channel_id):
        raise ShadowPathError(
            "not a 1:1 DM conversation; group DMs and channels are not stored here"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace):
        raise ShadowPathError("invalid workspace key for shadow layout")
    base = Path(root) / workspace / "dm" / user_id
    if not base.resolve().is_relative_to(Path(root).resolve()):
        raise ShadowPathError("shadow DM directory escaped its root")
    refuse_symlinked_chain(root, base)
    return base


def refuse_symlinked_chain(root: Path | str, target: Path) -> None:
    """`root` 아래 `target` 까지 내려가는 **모든 칸**이 링크가 아니어야 한다.

    마지막 칸만 보면 `<workspace>` 나 `dm` 이 다른 곳을 가리킬 때 통과한다. 그때
    경로 문자열은 개인 공간처럼 보이고 실제 파일은 남의 자리에 쌓인다 — 오류가
    나지 않는 종류의 사고다.

    `root` 자신은 보지 않는다. 아카이브 뿌리를 링크로 두는 것은 운영자의 선택이고,
    그 선택은 이 모듈이 판정할 자리가 아니다.
    """
    base = Path(root)
    current = base
    for part in Path(target).relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise ShadowPathError(f"shadow DM path contains a symlink: {part}")


def dm_archive_dir(
    root: Path | str, workspace: str, user_id: str, dm_channel_id: str
) -> Path:
    archive = dm_root(root, workspace, user_id, dm_channel_id) / "archive"
    refuse_symlinked_chain(root, archive)
    return archive
