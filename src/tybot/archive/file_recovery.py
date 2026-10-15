"""Explicit file-only recovery with separate receipts; never write message raw/ACKs."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from . import attachment_reader, attachment_writer, channel_files, channel_membership, shadow_paths
from .attachment_doc import revision_for
from .file_inventory import preview
from .files import AttachmentOrigin, attachment_storage, stage_attachments


def _safe_path(root: Path, path: Path) -> Path:
    shadow_paths.refuse_symlinked_chain(root, path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("recovery path escaped its root")
    return path


def existing_records(roots: list[Path], workspace: str, channel_id: str,
                     name: str, file_id: str) -> list[str]:
    """Conservative duplicate check, including orphan objects and legacy raw refs."""
    found = set()
    for root in roots:
        current = shadow_paths.channel_root(root, workspace, channel_id, name)
        legacy_base = root / "workspaces" / workspace / "channels"
        channels = [current, legacy_base / channel_id]
        channels.extend(legacy_base.glob(f"{channel_id}__*"))
        for channel in channels:
            _safe_path(root, channel)
            for kind, path in [
                ("staging", channel / "staging" / file_id),
                ("objects", channel / "objects" / file_id),
                ("canonical", channel / "archive" / "attachments" / file_id),
                ("canonical", channel / "attachments" / file_id),
            ]:
                if _safe_path(root, path).exists():
                    found.add(kind)
            for raw_root in [channel / "archive" / "raw", channel / "raw"]:
                _safe_path(root, raw_root)
                for raw in raw_root.glob("*.md"):
                    _safe_path(root, raw)
                    if file_id in raw.read_text(encoding="utf-8"):
                        found.add("raw-reference")
        for kind in ("staging", "objects"):
            path = (root.parent / kind / "workspaces" / workspace / "channels"
                    / channel_id / "attachments" / file_id)
            if _safe_path(root.parent, path).exists():
                found.add("legacy-" + kind)
    return sorted(found)


def _shared_file(client, channel_id: str, file_id: str, excluded_users: set[str]) -> dict:
    raw = client.files_info(file=file_id).get("file") or {}
    shared = set(raw.get("channels") or []) | set(raw.get("groups") or [])
    for visibility in ("public", "private"):
        shared.update((raw.get("shares") or {}).get(visibility) or {})
    if raw.get("id") != file_id or channel_id not in shared:
        raise PermissionError("current file sharing could not be confirmed")
    if (raw.get("user") in excluded_users or raw.get("bot_id")
            or channel_files._generated_canvas(raw)):
        raise PermissionError("bot-generated files cannot be recovered as evidence")
    uploader = str(raw.get("user") or "")
    if not re.fullmatch(r"[UW][A-Z0-9]+", uploader):
        raise PermissionError("a verified human uploader is required")
    user = client.users_info(user=uploader).get("user") or {}
    if (user.get("id") != uploader or user.get("is_bot") is not False
            or user.get("is_app_user") is True or uploader == "USLACKBOT"):
        raise PermissionError("bot or unknown uploader cannot become evidence")
    link = urlsplit(str(raw.get("permalink") or ""))
    if (link.scheme != "https" or not (link.hostname or "").endswith(".slack.com")
            or file_id not in link.path.split("/")):
        raise ValueError("a Slack file permalink is required")
    return raw


def _authorize(client, repo, owner, workspace: str, channel_id: str) -> dict:
    row = channel_membership.channel_row(repo, workspace, channel_id)
    if not channel_membership.is_collectible(row) or row["mode"] != "active":
        raise PermissionError("active collection is required")
    if not owner.archiver_may_write_live(channel_id, archiver_flag=owner.archiver_live_flag()):
        raise PermissionError("Archiver live ownership or global gate is disabled")
    info = client.conversations_info(channel=channel_id).get("channel") or {}
    if (info.get("id") != channel_id or not info.get("is_member")
            or info.get("is_im") or info.get("is_mpim") or info.get("is_archived")):
        raise PermissionError("current channel membership is required")
    return info


def _receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _original_verified(meta: dict, channel_root: Path) -> bool:
    if meta.get("original_state") != "retained" or not meta.get("object_path"):
        return False
    path = Path(meta["object_path"])
    if not path.is_absolute() or not path.resolve().is_relative_to(
        (channel_root / "objects").resolve()
    ):
        return False
    _safe_path(channel_root, path)
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == meta.get("sha256")


def recover(client, repo, owner, cfg, file_ids: list[str], *, live_root: Path,
            shadow_root: Path, channel_id: str, apply: bool, actor: str,
            indexer=None) -> dict:
    """Caller must hold the collector instance lock for the entire apply run.

    Existing unrelated artifacts are reported, not overwritten. A receipt written
    before staging permits resume after interruption without downloading again.
    """
    if not file_ids or len(file_ids) > 100 or any(
        not re.fullmatch(r"F[A-Z0-9]+", value) for value in file_ids
    ):
        raise ValueError("1..100 explicit Slack file IDs are required")
    if apply and not actor.strip():
        raise ValueError("an accountable actor is required")
    inventory = preview(client, repo, cfg.key, channel_id,
                        live_root=live_root, shadow_root=shadow_root)
    if not inventory["list_complete"]:
        raise ValueError("incomplete file inventory")
    info = _authorize(client, repo, owner, cfg.key, channel_id)
    name = str(info.get("name") or channel_id)
    base = shadow_paths.channel_root(live_root, cfg.key, channel_id, name)
    storage = attachment_storage(live_root, cfg.key, channel_id, channel_root=base)
    excluded = {value for value in [cfg.bot_user_id, cfg.master_bot_user_id] if value}
    results = []
    for file_id in dict.fromkeys(file_ids):
        item = {"file_id": file_id, "status": "failed"}
        results.append(item)
        try:
            info = _authorize(client, repo, owner, cfg.key, channel_id)
            raw = _shared_file(client, channel_id, file_id, excluded)
            receipt_path = _safe_path(live_root, base / "file-inventory" / f"{file_id}.json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
            if receipt is not None and any(receipt.get(k) != v for k, v in {
                "schema": "tybot.file-recovery.v1", "workspace": cfg.key,
                "channel_id": channel_id, "file_id": file_id,
            }.items()):
                raise ValueError("invalid recovery receipt")
            records = existing_records([live_root, shadow_root], cfg.key, channel_id, name, file_id)
            if records and receipt is None:
                item.update(status="existing-needs-review", records=records)
                continue
            if not apply:
                item["status"] = "resume" if receipt else "missing"
                continue
            receipt = receipt or {
                "schema": "tybot.file-recovery.v1", "workspace": cfg.key,
                "channel_id": channel_id, "file_id": file_id,
                "discovery": "slack-files-api", "permalink": raw["permalink"],
                "observed_at": datetime.now(UTC).isoformat(), "actor": actor,
            }
            receipt.update(status="started")
            _receipt(receipt_path, receipt)
            meta_path = _safe_path(live_root, storage.staging_dir / file_id / "metadata.json")
            _safe_path(live_root, storage.objects_dir / file_id)
            _safe_path(live_root, storage.staging_dir / file_id / "extracted.md")
            if not meta_path.exists():
                # Any artifacts without metadata need manual review; do not overwrite bytes.
                if records:
                    item["status"] = "incomplete-needs-review"
                    continue
                stage_attachments([raw], cfg.bot_token, storage,
                                  origin=AttachmentOrigin(workspace=cfg.key, channel_id=channel_id),
                                  separate_attachments=True)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("slack_file_id") != file_id or meta.get("origin_message_ts"):
                raise ValueError("staging provenance does not match file-only recovery")
            item["original_verified"] = _original_verified(meta, base)
            item["conversion_state"] = str(meta.get("conversion_state") or "unknown")
            if not item["original_verified"]:
                item["status"] = "original-unverified"
            elif item["conversion_state"] != "succeeded":
                item["status"] = "retained-not-searchable"
            else:
                # Recheck permissions after slow download/conversion, before publishing.
                info = _authorize(client, repo, owner, cfg.key, channel_id)
                if str(info.get("name") or channel_id) != name:
                    raise PermissionError("channel name changed during recovery; replan required")
                _shared_file(client, channel_id, file_id, excluded)
                _safe_path(live_root, base / "archive" / "attachments" / file_id)
                revision = revision_for(
                    sha256=str(meta.get("sha256") or ""),
                    converter_name=str(meta.get("converter_name") or ""),
                    converter_version=str(meta.get("converter_version") or ""),
                    config=meta.get("converter_config") or {},
                    staged_at=str(meta.get("staged_at") or ""),
                )
                _safe_path(live_root, base / "archive" / "attachments" / file_id / f"{revision}.md")
                docs = attachment_writer.write_docs(
                    live_root, [SimpleNamespace(metadata_path=meta_path)],
                    workspace=cfg.key, channel_id=channel_id, channel="#" + name,
                    visibility="private" if info.get("is_private", True) else "public",
                    acl=frozenset({"#" + name}), channel_archive=base / "archive",
                )
                if len(docs) != 1 or not docs[0].text.strip():
                    raise ValueError("canonical document was not confirmed")
                path = base / "archive" / "attachments" / file_id / f"{docs[0].revision}.md"
                _safe_path(live_root, path)
                doc = attachment_reader.load_checked(path, live_root)
                if doc is None:
                    raise ValueError("canonical document validation failed")
                if indexer is None:
                    item["status"] = "published-index-pending"
                else:
                    indexer([attachment_reader.as_archive_doc(doc, live_root)], live_root)
                    item["status"] = "recovered"
            receipt.update(item, updated_at=datetime.now(UTC).isoformat())
            _receipt(receipt_path, receipt)
        except Exception as exc:  # noqa: BLE001 - per-file failure, no sensitive SDK output
            item.update(status="failed", error=type(exc).__name__)
    return {"workspace": cfg.key, "channel_id": channel_id, "read_only": not apply,
            "results": results}


def index_recovered(root: Path, workspace: str, channel_id: str,
                    file_ids: list[str], *, indexer) -> dict:
    """Index only verified recovery documents, using the separate index-writer role."""
    if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace)
            or not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel_id)
            or not file_ids or len(file_ids) > 100
            or any(not re.fullmatch(r"F[A-Z0-9]+", value) for value in file_ids)):
        raise ValueError("invalid recovery coordinates")
    base = shadow_paths.existing_channel_root(root, workspace, channel_id)
    results = []
    for file_id in dict.fromkeys(file_ids):
        item = {"file_id": file_id, "status": "failed"}
        results.append(item)
        try:
            receipt_path = _safe_path(root, base / "file-inventory" / f"{file_id}.json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("schema") != "tybot.file-recovery.v1"
                    or receipt.get("workspace") != workspace
                    or receipt.get("channel_id") != channel_id
                    or receipt.get("file_id") != file_id):
                raise ValueError("invalid recovery receipt")
            meta_path = _safe_path(root, base / "staging" / file_id / "metadata.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (meta.get("slack_file_id") != file_id or meta.get("origin_message_ts")
                    or not _original_verified(meta, base)):
                raise ValueError("original is not verified")
            folder = _safe_path(root, base / "archive" / "attachments" / file_id)
            docs = []
            for path in folder.glob("*.md"):
                _safe_path(root, path)
                doc = attachment_reader.load_checked(path, root)
                if (doc and doc.conversion_state == "succeeded" and doc.text.strip()
                        and doc.sha256 == meta.get("sha256")):
                    docs.append(doc)
            docs = attachment_reader.current_by_file(docs)
            if len(docs) != 1:
                raise ValueError("one valid canonical document is required")
            indexer([attachment_reader.as_archive_doc(docs[0], root)], root)
            item["status"] = "recovered"
            receipt.update(status="recovered", indexed_at=datetime.now(UTC).isoformat())
            _receipt(receipt_path, receipt)
        except Exception as exc:  # noqa: BLE001 - no private metadata in result
            item["error"] = type(exc).__name__
    return {"workspace": workspace, "channel_id": channel_id, "read_only": False,
            "index_only": True, "results": results}
