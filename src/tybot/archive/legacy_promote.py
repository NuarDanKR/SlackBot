"""Promote a quarantined legacy archive into the per-channel live layout.

The source is never modified.  Planning is the default; callers must pass
``apply=True`` to write.  Raw messages are re-ingested through the canonical
writer so coordinate deduplication, masking, schema validation, and locks stay
identical to live collection.  Attachment extracts are copied only when the
same revision is absent or byte-identical.

2026-11-24 (owner decisions, B-98 apply):

* Legacy raw lines that inline an attachment extract (``[첨부추출:…]`` /
  ``[첨부본문:…]``) are **not** promoted.  The current layout keeps attachment
  bodies only in ``attachments/<file-id>/<revision>.md``; re-inlining them would
  create 50MB day files and count the same attachment twice.  Names that have
  no canonical attachment document are reported so conversion can be rerun.
* When the live channel's newest document is **public** (the Archiver takes
  this from Slack ``is_private``), promoted lines and attachment documents take
  the live policy.  Old code marked every channel private; a public Slack
  channel's history is already readable by the whole workspace.  A private
  live policy that differs from the legacy one still blocks.
* ``exclude`` lets an operator leave reviewed items (a Slackbot DM, a
  conflicting attachment revision) in quarantine without blocking the rest.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..pii_screen import screen_document
from ..sensitive_mask import mask_sensitive
from . import attachment_doc, attachment_reader, writer
from .shadow_paths import (
    ShadowPathError,
    archive_dir,
    dm_archive_dir,
    existing_channel_root,
)
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
    visibility: str = ""
    acl: frozenset[str] = frozenset()
    share_with: frozenset[str] = frozenset()


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
    #: 원문에 펼쳐져 있던 첨부 본문 줄. 정본(attachments/)으로만 들어간다.
    skipped_attachment_lines: int = 0
    #: 원문에 이름은 있는데 원본·운영 어디에도 정본 문서가 없는 첨부.
    attachment_names_without_document: list[str] = field(default_factory=list)
    #: 운영(Slack) 공개 정책을 따른 채널.
    live_policy_adopted: list[str] = field(default_factory=list)
    #: 운영자가 `exclude` 로 격리에 남긴 항목.
    excluded: list[str] = field(default_factory=list)
    #: 같은 file ID·revision(= 같은 원본)의 운영 정본이 이미 있어 운영 쪽을 둔 첨부.
    live_attachment_kept: int = 0
    #: 그중 옛 쪽에만 본문이 있는 것 — 운영에서 다시 변환할 대상.
    attachment_text_only_in_legacy: list[str] = field(default_factory=list)
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


def _newest_live_doc(target: Path) -> ArchiveDoc | None:
    """운영 채널의 **가장 최근** 원문. 지금 멤버·공개 여부가 그 문서에 있다."""
    existing = sorted((target / "raw").glob("*.md"))
    return load_doc(existing[-1]) if existing else None


def _same_policy(source: ArchiveDoc, current: ArchiveDoc | None) -> bool:
    if current is None:
        return True
    return (
        current.visibility == source.visibility
        and current.acl == source.acl
        and current.share_with == source.share_with
        and current.dm_user == source.dm_user
    )


def _is_excluded(relative: str, exclude: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + "/") for prefix in exclude
    )


#: 대괄호가 든 파일명(`[190801]정산서.xlsx`)까지 잡는다. `LEGACY_LINE` 은 첫 `]` 에서
#: 끊겨 `[190801` 만 남고, 그러면 정본 문서와 이름이 맞지 않는다.
_INLINE_NAME = re.compile(
    r"^\[(?:첨부추출|첨부본문):(?P<name>.+?\.[A-Za-z0-9]{1,6})\](?:\s|$)"
)


def _inline_attachment_name(text: str) -> str | None:
    match = attachment_doc.LEGACY_LINE.match(text)
    if not match:
        return None
    full = _INLINE_NAME.match(text)
    return (full or match).group("name").strip()


def _split_attachment_lines(
    messages: tuple[writer.IncomingMessage, ...],
) -> tuple[tuple[writer.IncomingMessage, ...], list[str]]:
    kept: list[writer.IncomingMessage] = []
    names: list[str] = []
    for message in messages:
        name = _inline_attachment_name(message.text)
        if name is not None:
            names.append(name)
        else:
            kept.append(message)
    return tuple(kept), names


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
    adopt_policy: bool = False,
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
    if adopt_policy and visibility is not None:
        # 채널이 운영(Slack) 공개 정책을 따른다 — 첨부도 같은 정책으로 적는다.
        doc = replace(doc, visibility=visibility, acl=acl or frozenset())
    elif visibility is not None and (
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
    source: Path,
    destination: Path,
    report: PromotionReport,
    exclude: tuple[str, ...] = (),
    adopted: set[tuple[str, str]] | None = None,
    legacy_names: dict[tuple[str, str], set[str]] | None = None,
) -> tuple[
    list[_RawPlan],
    dict[tuple[str, str], Path],
    dict[tuple[str, str], tuple[str, frozenset[str]]],
    dict[tuple[str, str, str], tuple[str, frozenset[str]]],
]:
    adopted = adopted if adopted is not None else set()
    legacy_names = legacy_names if legacy_names is not None else {}
    plans: list[_RawPlan] = []
    channels: dict[tuple[str, str], Path] = {}
    policies: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
    # DM 첨부도 **그 DM 원문의 정책**에 묶는다. (workspace, user, conversation) 단위.
    dm_policies: dict[tuple[str, str, str], tuple[str, frozenset[str]]] = {}
    paths = sorted(source.glob("*/channels/*/raw/*.md"))
    paths += sorted(source.glob("*/dm/*/raw/*.md"))
    for path in paths:
        if _is_excluded(_relative(path, source), exclude):
            report.excluded.append(_relative(path, source))
            continue
        report.source_documents += 1
        try:
            if _contains_symlink(source, path):
                raise LegacyPromotionError("source path contains a symlink")
            doc = load_doc(path)
            relative = path.relative_to(source)
            workspace = relative.parts[0]
            effective = (doc.visibility, doc.acl, doc.share_with)
            live_doc: ArchiveDoc | None = None
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
                if _contains_symlink(destination, target):
                    raise LegacyPromotionError("destination path contains a symlink")
                live_doc = _newest_live_doc(target)
                if live_doc is not None and live_doc.visibility == "public":
                    # 운영(Slack) 공개 채널 — 옛 코드의 비공개 표시보다 현재 사실이 맞다.
                    effective = (live_doc.visibility, live_doc.acl, live_doc.share_with)
                    if (doc.visibility, doc.acl, doc.share_with) != effective:
                        adopted.add(key)
                    policy = (live_doc.visibility, live_doc.acl)
                else:
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
                dm_key = (workspace, dm_user, doc.channel_id)
                dm_policy = (doc.visibility, doc.acl)
                if dm_key in dm_policies and dm_policies[dm_key] != dm_policy:
                    raise LegacyPromotionError(
                        "legacy documents for one DM have different policies"
                    )
                dm_policies[dm_key] = dm_policy
                if _contains_symlink(destination, target):
                    raise LegacyPromotionError("destination path contains a symlink")
                live_doc = _newest_live_doc(target)
            follows_live = (
                relative.parts[1] == "channels"
                and live_doc is not None
                and live_doc.visibility == "public"
            )
            if not follows_live and not _same_policy(doc, live_doc):
                raise LegacyPromotionError("source and live access policies differ")
            messages, inline_names = _split_attachment_lines(_messages(doc))
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
        report.skipped_attachment_lines += len(inline_names)
        if inline_names:
            legacy_names.setdefault((workspace, doc.channel_id), set()).update(
                inline_names
            )
        plans.append(
            _RawPlan(
                path, target, doc, messages, pending, dm_user,
                visibility=effective[0], acl=effective[1], share_with=effective[2],
            )
        )
    return plans, channels, policies, dm_policies


def _copy_plans(
    source: Path,
    destination: Path,
    channels: dict[tuple[str, str], Path],
    policies: dict[tuple[str, str], tuple[str, frozenset[str]]],
    report: PromotionReport,
    dm_policies: dict[tuple[str, str, str], tuple[str, frozenset[str]]] | None = None,
    exclude: tuple[str, ...] = (),
    adopted: set[tuple[str, str]] | None = None,
    names: dict[tuple[str, str], set[str]] | None = None,
) -> list[_CopyPlan]:
    """첨부 정본 복사 계획. **접근 정책을 확정하지 못하면 복사하지 않는다**(fail-closed).

    첨부는 자기 프론트매터의 visibility·ACL 을 들고 간다. 대조할 채널·DM 정책이
    없을 때 그대로 복사하면, 옛 첨부가 들고 온 더 넓은 정책이 운영 아카이브에
    들어간다(2026-11-19 보안 검토).
    """
    dm_policies = dm_policies or {}
    adopted = adopted if adopted is not None else set()
    names = names if names is not None else {}
    plans: list[_CopyPlan] = []
    paths = sorted(source.glob("*/channels/*/attachments/*/*.md"))
    paths += sorted(source.glob("*/dm/*/attachments/*/*.md"))
    for path in paths:
        if _is_excluded(_relative(path, source), exclude):
            report.excluded.append(_relative(path, source))
            continue
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
                # 운영 쪽에서 찾는다. ID·워크스페이스 형식, 여러 디렉터리, symlink 는
                # `existing_channel_root` 가 막는다 — 경로 조각을 glob 에 그대로 넣지 않는다.
                try:
                    target_root = existing_channel_root(
                        destination, workspace, channel_id
                    ) / "archive"
                except ShadowPathError:
                    target_root = None
                if target_root is not None:
                    live_doc = _newest_live_doc(target_root)
                    if live_doc is not None:
                        policy = (live_doc.visibility, live_doc.acl)
                        if live_doc.visibility == "public":
                            adopted.add((workspace, channel_id))
            if target_root is None:
                report.blocked.append(
                    f"{_relative(path, source)}: no unambiguous live channel target"
                )
                continue
            if policy is None:
                report.blocked.append(
                    f"{_relative(path, source)}: no access policy for target channel"
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
                adopt_policy=(
                    relative.parts[1] == "channels"
                    and (workspace, channel_id) in adopted
                ),
            )
            if relative.parts[1] == "channels":
                names.setdefault((workspace, channel_id), set()).add(loaded.name)
            if relative.parts[1] == "dm":
                dm_policy = dm_policies.get((workspace, user_id, loaded.channel_id))
                if dm_policy is None:
                    raise LegacyPromotionError("no DM document plan for this attachment")
                if (loaded.visibility, loaded.acl) != dm_policy:
                    raise LegacyPromotionError("attachment and DM access policies differ")
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
            # revision 은 원본 해시다 — 경로가 같으면 원본이 같다. 다른 것은 다시 처리하며
            # 붙은 값(permalink·처리 시각·변환 상태·공개 여부)이고, 운영 정본이 현재
            # 파이프라인의 결과다. 덮어쓰지 않고 운영 쪽을 둔다(2026-11-24).
            current = attachment_reader.load(target) if target.is_file() else None
            if current is None:
                report.blocked.append(
                    f"{_relative(path, source)}: live attachment revision differs"
                )
                continue
            report.live_attachment_kept += 1
            if loaded.text.strip() and not current.text.strip():
                report.attachment_text_only_in_legacy.append(_relative(path, source))
            continue
        if identical:
            report.identical_attachment_documents += 1
        else:
            report.pending_attachment_documents += 1
        plans.append(_CopyPlan(path, target, content, identical))
    return plans


def _live_attachment_names(target_root: Path) -> set[str]:
    found: set[str] = set()
    for path in sorted((target_root / "attachments").glob("*/*.md")):
        doc = attachment_reader.load(path)
        if doc is not None:
            found.add(doc.name)
    return found


def _report_missing_documents(
    channels: dict[tuple[str, str], Path],
    inline: dict[tuple[str, str], set[str]],
    canonical: dict[tuple[str, str], set[str]],
    report: PromotionReport,
) -> None:
    """원문에서 뺀 첨부 줄 가운데 정본 문서가 어디에도 없는 이름을 남긴다."""
    for key in sorted(inline):
        target = channels.get(key)
        have = set(canonical.get(key, set()))
        if target is not None:
            have |= _live_attachment_names(target)
        for name in sorted(inline[key]):
            if name in have or mask_sensitive(name).text in have:
                continue
            report.attachment_names_without_document.append(
                f"{key[0]}/{key[1]}: {mask_sensitive(name).text}"
            )


def promote(
    source_workspaces: Path | str,
    destination: Path | str,
    *,
    apply: bool = False,
    exclude: tuple[str, ...] | list[str] = (),
) -> PromotionReport:
    """Plan or apply a lossless promotion from a quarantined ``workspaces`` tree."""
    source = Path(source_workspaces)
    live = Path(destination)
    _validate_roots(source, live)
    excluded = tuple(item.strip().strip("/") for item in exclude if item.strip("/ "))
    report = PromotionReport(apply=apply)
    adopted: set[tuple[str, str]] = set()
    inline: dict[tuple[str, str], set[str]] = {}
    canonical: dict[tuple[str, str], set[str]] = {}
    raw_plans, channels, policies, dm_policies = _raw_plans(
        source, live, report, excluded, adopted, inline
    )
    copy_plans = _copy_plans(
        source, live, channels, policies, report, dm_policies,
        excluded, adopted, canonical,
    )
    _report_missing_documents(channels, inline, canonical, report)
    report.live_policy_adopted = [f"{ws}/{cid}" for ws, cid in sorted(adopted)]
    if not apply or report.blocked:
        return report

    for plan in raw_plans:
        kwargs = {
            "workspace": plan.doc.workspace,
            "channel": plan.doc.channel,
            "channel_id": plan.doc.channel_id,
            "messages": list(plan.messages),
            "visibility": plan.visibility or plan.doc.visibility,
            "acl": sorted(plan.acl if plan.visibility else plan.doc.acl),
            "share_with": sorted(
                plan.share_with if plan.visibility else plan.doc.share_with
            ),
            # 어느 격리분에서 왔는지 남긴다(예: archive-legacy-quarantine-20261124).
            "imported_from": source.parent.name,
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
