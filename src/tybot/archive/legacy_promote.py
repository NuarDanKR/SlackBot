"""Promote a quarantined legacy archive into the per-channel live layout.

The source is never modified.  Planning is the default; callers must pass
``apply=True`` to write.  Raw messages are re-ingested through the canonical
writer so coordinate deduplication, masking, schema validation, and locks stay
identical to live collection.  Attachment extracts are copied only when the
same revision is absent or byte-identical.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..pii_screen import screen_document
from ..sensitive_mask import mask_sensitive
from . import attachment_doc, attachment_reader, writer
from .shadow_paths import ShadowPathError, archive_dir, dm_archive_dir
from .store import ArchiveDoc, RawLine, SchemaError, load_doc


class LegacyPromotionError(ValueError):
    """The promotion cannot safely start."""


@dataclass(frozen=True)
class _RawPlan:
    source: Path
    target: Path
    doc: ArchiveDoc
    messages: tuple[writer.IncomingMessage, ...]
    pending: int
    dm_user: str = ""


@dataclass(frozen=True)
class _CopyPlan:
    source: Path
    target: Path
    content: bytes
    identical: bool = False


@dataclass
class PromotionReport:
    apply: bool
    source_documents: int = 0
    source_messages: int = 0
    pending_messages: int = 0
    written_messages: int = 0
    source_attachment_documents: int = 0
    pending_attachment_documents: int = 0
    copied_attachment_documents: int = 0
    identical_attachment_documents: int = 0
    refused_messages: int = 0
    masked_values: dict[str, int] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked

    def as_json(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def _validate_roots(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not destination.is_absolute():
        raise LegacyPromotionError("source and destination must be absolute")
    if not source.is_dir() or source.is_symlink():
        raise LegacyPromotionError("source must be an existing non-symlink directory")
    if not destination.is_dir() or destination.is_symlink():
        raise LegacyPromotionError(
            "destination must be an existing non-symlink directory"
        )
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise LegacyPromotionError("source and destination must be disjoint")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _contains_symlink(root: Path, path: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _parse_timestamp(line: RawLine) -> datetime:
    value = line.ts.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        if not line.message_ts:
            raise LegacyPromotionError("invalid legacy line timestamp") from None
        try:
            parsed = datetime.fromtimestamp(float(line.message_ts), tz=UTC)
        except (ValueError, OverflowError) as exc:
            raise LegacyPromotionError("invalid Slack message coordinate") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=writer.KST)
    return parsed


def _messages(doc: ArchiveDoc) -> tuple[writer.IncomingMessage, ...]:
    return tuple(
        writer.IncomingMessage(
            ts=_parse_timestamp(line),
            speaker=line.speaker,
            text=line.text,
            source_ts=line.message_ts,
        )
        for line in doc.raw_lines
    )


def _seen(target: Path) -> tuple[set[str], set[str]]:
    coordinated: set[str] = set()
    plain: set[str] = set()
    for path in sorted((target / "raw").glob("*.md")):
        doc = load_doc(path)
        for line in doc.raw_lines:
            message = writer.IncomingMessage(
                ts=_parse_timestamp(line),
                speaker=line.speaker,
                text=line.text,
                source_ts=line.message_ts,
            )
            rendered = writer.format_line(message)
            coordinated.add(writer.dedupe_line(rendered))
            plain.add(writer.legacy_dedupe_line(rendered))
    return coordinated, plain


def _pending(messages: tuple[writer.IncomingMessage, ...], target: Path) -> int:
    coordinated, plain = _seen(target)
    count = 0
    for message in messages:
        rendered = writer.format_line(message)
        if message.source_ts:
            key = writer.dedupe_line(rendered)
            if key in coordinated:
                continue
            coordinated.add(key)
        else:
            key = writer.legacy_dedupe_line(rendered)
            if key in plain:
                continue
            plain.add(key)
        count += 1
    return count


def _same_policy(source: ArchiveDoc, target: Path) -> bool:
    existing = sorted((target / "raw").glob("*.md"))
    if not existing:
        return True
    current = load_doc(existing[0])
    return (
        current.visibility == source.visibility
        and current.acl == source.acl
        and current.share_with == source.share_with
        and current.dm_user == source.dm_user
    )


def _channel_id_from_directory(name: str) -> str:
    return name.split("__", 1)[0]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_attachment_content(
    path: Path,
    *,
    workspace: str,
    channel_id: str | None,
    file_id: str,
    revision: str,
    visibility: str | None,
    acl: frozenset[str] | None,
) -> tuple[attachment_doc.AttachmentDoc, bytes]:
    doc = attachment_reader.load(path)
    if doc is None:
        raise LegacyPromotionError("attachment document is unreadable")
    if (
        doc.workspace != workspace
        or (channel_id is not None and doc.channel_id != channel_id)
        or doc.file_id != file_id
        or doc.revision != revision
    ):
        raise LegacyPromotionError("attachment coordinates do not match its path")
    if visibility is not None and (
        doc.visibility != visibility or doc.acl != (acl or frozenset())
    ):
        raise LegacyPromotionError("attachment and channel access policies differ")
    screened = screen_document(
        doc.text,
        filename=doc.name,
        coverage_state=doc.conversion_state,
    )
    if screened.blocked:
        doc = replace(
            doc,
            text="",
            conversion_state=attachment_doc.BLOCKED,
            error_code="pii_refused",
        )
    else:
        masked_text = mask_sensitive(doc.text)
        masked_name = mask_sensitive(doc.name)
        doc = replace(doc, text=masked_text.text, name=masked_name.text)
    return doc, attachment_doc.render(doc).encode("utf-8")


def _raw_plans(
    source: Path, destination: Path, report: PromotionReport,
) -> tuple[
    list[_RawPlan],
    dict[tuple[str, str], Path],
    dict[tuple[str, str], tuple[str, frozenset[str]]],
]:
    plans: list[_RawPlan] = []
    channels: dict[tuple[str, str], Path] = {}
    policies: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
    paths = sorted(source.glob("*/channels/*/raw/*.md"))
    paths += sorted(source.glob("*/dm/*/raw/*.md"))
    for path in paths:
        report.source_documents += 1
        try:
            if _contains_symlink(source, path):
                raise LegacyPromotionError("source path contains a symlink")
            doc = load_doc(path)
            relative = path.relative_to(source)
            workspace = relative.parts[0]
            if doc.workspace != workspace:
                raise LegacyPromotionError("workspace frontmatter does not match path")
            dm_user = ""
            if relative.parts[1] == "channels":
                if not doc.channel_id:
                    raise LegacyPromotionError("channel document has no channel_id")
                if doc.visibility != "public" and not doc.acl:
                    raise LegacyPromotionError("private channel document has an empty ACL")
                target = archive_dir(
                    destination, workspace, doc.channel_id, doc.channel
                )
                key = (workspace, doc.channel_id)
                prior = channels.get(key)
                if prior is not None and prior != target:
                    raise LegacyPromotionError("one channel resolves to multiple targets")
                channels[key] = target
                policy = (doc.visibility, doc.acl)
                if key in policies and policies[key] != policy:
                    raise LegacyPromotionError(
                        "legacy documents for one channel have different policies"
                    )
                policies[key] = policy
            else:
                dm_user = relative.parts[2]
                if doc.dm_user and doc.dm_user != dm_user:
                    raise LegacyPromotionError("DM user frontmatter does not match path")
                if not doc.channel_id:
                    raise LegacyPromotionError("DM document has no Slack conversation ID")
                target = dm_archive_dir(
                    destination, workspace, dm_user, doc.channel_id
                )
            if _contains_symlink(destination, target):
                raise LegacyPromotionError("destination path contains a symlink")
            if not _same_policy(doc, target):
                raise LegacyPromotionError("source and live access policies differ")
            messages = _messages(doc)
            if any(writer.screen(mask_sensitive(message.text).text) for message in messages):
                raise LegacyPromotionError(
                    "a legacy message remains sensitive after masking"
                )
            pending = _pending(messages, target)
        except (LegacyPromotionError, SchemaError, OSError, ShadowPathError) as exc:
            report.blocked.append(f"{_relative(path, source)}: {exc}")
            continue
        report.source_messages += len(messages)
        report.pending_messages += pending
        plans.append(_RawPlan(path, target, doc, messages, pending, dm_user))
    return plans, channels, policies


def _copy_plans(
    source: Path,
    destination: Path,
    channels: dict[tuple[str, str], Path],
    policies: dict[tuple[str, str], tuple[str, frozenset[str]]],
    report: PromotionReport,
) -> list[_CopyPlan]:
    plans: list[_CopyPlan] = []
    paths = sorted(source.glob("*/channels/*/attachments/*/*.md"))
    paths += sorted(source.glob("*/dm/*/attachments/*/*.md"))
    for path in paths:
        report.source_attachment_documents += 1
        relative = path.relative_to(source)
        workspace = relative.parts[0]
        if _contains_symlink(source, path):
            report.blocked.append(
                f"{_relative(path, source)}: source path contains a symlink"
            )
            continue
        if relative.parts[1] == "channels":
            channel_id = _channel_id_from_directory(relative.parts[2])
            target_root = channels.get((workspace, channel_id))
            policy = policies.get((workspace, channel_id))
            if target_root is None:
                matches = sorted(
                    item
                    for item in (destination / workspace).glob(f"{channel_id}__*")
                    if item.is_dir() and not item.is_symlink()
                )
                if len(matches) == 1:
                    target_root = matches[0] / "archive"
                    raw = sorted((target_root / "raw").glob("*.md"))
                    if raw:
                        live_doc = load_doc(raw[0])
                        policy = (live_doc.visibility, live_doc.acl)
            if target_root is None:
                report.blocked.append(
                    f"{_relative(path, source)}: no unambiguous live channel target"
                )
                continue
            suffix = Path(*relative.parts[4:])
        else:
            user_id = relative.parts[2]
            target_root = None
            policy = None
            suffix = Path(*relative.parts[4:])
        file_id = suffix.parts[0]
        revision = Path(suffix.parts[-1]).stem
        try:
            loaded, content = _safe_attachment_content(
                path,
                workspace=workspace,
                channel_id=channel_id if relative.parts[1] == "channels" else None,
                file_id=file_id,
                revision=revision,
                visibility=policy[0] if policy else None,
                acl=policy[1] if policy else None,
            )
            if relative.parts[1] == "dm":
                target_root = dm_archive_dir(
                    destination, workspace, user_id, loaded.channel_id
                )
        except (LegacyPromotionError, OSError, ShadowPathError) as exc:
            report.blocked.append(f"{_relative(path, source)}: {exc}")
            continue
        assert target_root is not None
        target = target_root / "attachments" / suffix
        if _contains_symlink(destination, target):
            report.blocked.append(
                f"{_relative(path, source)}: destination path contains a symlink"
            )
            continue
        identical = target.is_file() and _digest_bytes(content) == _digest(target)
        if target.exists() and not identical:
            report.blocked.append(
                f"{_relative(path, source)}: live attachment revision differs"
            )
            continue
        if identical:
            report.identical_attachment_documents += 1
        else:
            report.pending_attachment_documents += 1
        plans.append(_CopyPlan(path, target, content, identical))
    return plans


def promote(
    source_workspaces: Path | str,
    destination: Path | str,
    *,
    apply: bool = False,
) -> PromotionReport:
    """Plan or apply a lossless promotion from a quarantined ``workspaces`` tree."""
    source = Path(source_workspaces)
    live = Path(destination)
    _validate_roots(source, live)
    report = PromotionReport(apply=apply)
    raw_plans, channels, policies = _raw_plans(source, live, report)
    copy_plans = _copy_plans(source, live, channels, policies, report)
    if not apply or report.blocked:
        return report

    for plan in raw_plans:
        kwargs = {
            "workspace": plan.doc.workspace,
            "channel": plan.doc.channel,
            "channel_id": plan.doc.channel_id,
            "messages": list(plan.messages),
            "visibility": plan.doc.visibility,
            "acl": sorted(plan.doc.acl),
            "share_with": sorted(plan.doc.share_with),
            "imported_from": "legacy-quarantine-2026-11-18",
        }
        if plan.dm_user:
            result = writer.ingest(
                live,
                **kwargs,
                dm_user=plan.dm_user,
                dm_directory=plan.target,
            )
        else:
            result = writer.ingest(live, **kwargs, channel_directory=plan.target)
        report.written_messages += result.written
        report.refused_messages += len(result.refused)
        for code, count in result.mask_counts.items():
            report.masked_values[code] = report.masked_values.get(code, 0) + count
    if report.refused_messages:
        report.blocked.append(
            f"{report.refused_messages} legacy messages were refused during apply"
        )
        return report

    for plan in copy_plans:
        if plan.identical:
            continue
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with plan.target.open("xb") as outgoing:
                outgoing.write(plan.content)
        except FileExistsError:
            if _digest_bytes(plan.content) != _digest(plan.target):
                report.blocked.append(
                    f"{_relative(plan.source, source)}: attachment changed during apply"
                )
                continue
        report.copied_attachment_documents += 1
    return report
