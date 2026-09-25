"""Independent Slack archive collector in shadow mode.

This process never opens the TYBot Socket Mode token and never writes the live
archive. It is the first, comparison-only step of Archiving Bot extraction.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from slack_sdk.errors import SlackApiError

from .archive import writer
from .archive.attachment_writer import raw_lines_for
from .archive.attachment_writer import write_docs as write_attachment_docs
from .archive.revision_store import record_revision
from .archive.store import ArchiveStore
from .attachment_trace import confirm_archived
from .channels import should_collect
from .collect import _messages_from
from .lock import AlreadyRunning, instance_lock
from .workspaces import env_suffix

log = logging.getLogger("tybot.archiving_bot")


class ArchiverConfigError(RuntimeError):
    """A missing or unsafe archiver configuration must prevent startup."""


@dataclass(frozen=True)
class ArchiverWorkspace:
    key: str
    bot_token: str
    app_token: str
    team_id: str
    master_bot_user_id: str
    allowed_channels: frozenset[str]
    separate_attachments: bool = False


def validate_slack_identity(cfg: ArchiverWorkspace, identity: dict) -> None:
    if identity.get("team_id") != cfg.team_id:
        raise ArchiverConfigError(f"archiver token belongs to the wrong workspace: {cfg.key}")
    if identity.get("user_id") == cfg.master_bot_user_id:
        raise ArchiverConfigError(f"archiver app is the TYBot app in workspace {cfg.key}")


def load_archiver_workspaces(env: dict[str, str] | None = None) -> list[ArchiverWorkspace]:
    values = os.environ if env is None else env
    source = values.get("ARCHIVER_CONFIG_SOURCE", "db").strip().lower()
    if source == "db":
        key = values.get("ARCHIVER_WORKSPACE", "").strip().lower()
        if not key:
            raise ArchiverConfigError("ARCHIVER_WORKSPACE is required for DB configuration")
        from .archiver_runtime_store import load_runtime_config

        row = load_runtime_config(key)
        channel_ids = frozenset(str(value) for value in row["channel_ids"])
        if not channel_ids or len(channel_ids) > 5:
            raise ArchiverConfigError(f"archiver pilot needs 1-5 DB channel IDs for {key}")
        return [ArchiverWorkspace(
            key=key,
            bot_token=str(row["bot_token"]),
            app_token=str(row["app_token"]),
            team_id=str(row["team_id"]),
            master_bot_user_id=str(row["master_bot_user_id"]),
            allowed_channels=channel_ids,
            separate_attachments=bool(row.get("separate_attachments", False)),
        )]
    if source != "env":
        raise ArchiverConfigError("ARCHIVER_CONFIG_SOURCE must be db or env")
    keys = [key.strip() for key in values.get("ARCHIVER_WORKSPACES", "").split(",") if key.strip()]
    if len(keys) != 1:
        raise ArchiverConfigError("one archiver workspace is required per process")
    configs = []
    for key in keys:
        suffix = env_suffix(key)
        bot = values.get(f"ARCHIVER_BOT_TOKEN_{suffix}", "")
        app = values.get(f"ARCHIVER_APP_TOKEN_{suffix}", "")
        team_id = values.get(f"ARCHIVER_TEAM_ID_{suffix}", "")
        master_user_id = values.get(f"ARCHIVER_MASTER_BOT_USER_{suffix}", "")
        channel_ids = [
            item.strip()
            for item in values.get(f"ARCHIVER_CHANNEL_IDS_{suffix}", "").split(",")
            if item.strip()
        ]
        if not bot or not app or not team_id or not master_user_id:
            raise ArchiverConfigError(f"archiver identity or token pair missing for {key}")
        if not channel_ids or len(channel_ids) > 5 or any(
            re.fullmatch(r"[CG][A-Z0-9]{8,}", item) is None for item in channel_ids
        ):
            raise ArchiverConfigError(f"archiver pilot needs 1-5 channel IDs for {key}")
        master_bot = values.get(f"SLACK_BOT_TOKEN_{suffix}") or values.get("SLACK_BOT_TOKEN")
        master_app = values.get(f"SLACK_APP_TOKEN_{suffix}") or values.get("SLACK_APP_TOKEN")
        if bot == master_bot or app == master_app:
            raise ArchiverConfigError(f"archiver and TYBot tokens must differ for {key}")
        configs.append(
            ArchiverWorkspace(
                key,
                bot,
                app,
                team_id,
                master_user_id,
                frozenset(channel_ids),
                values.get("ARCHIVE_SEPARATE_ATTACHMENTS", "").strip().lower()
                in {"1", "true", "yes", "on"},
            )
        )
    return configs


def shadow_archive_dir(env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    supplied = values.get("ARCHIVER_SHADOW_DIR", "")
    if not supplied or not Path(supplied).is_absolute():
        raise ArchiverConfigError("ARCHIVER_SHADOW_DIR must be an explicit absolute path")
    live_supplied = values.get("ARCHIVE_DIR", "")
    if not live_supplied or not Path(live_supplied).is_absolute():
        raise ArchiverConfigError("ARCHIVE_DIR must be an explicit absolute path")
    shadow = Path(supplied).resolve()
    live = Path(live_supplied).resolve()
    if shadow == live or shadow in live.parents or live in shadow.parents:
        raise ArchiverConfigError("shadow and live archive paths must not overlap")
    return shadow


class ShadowCollector:
    def __init__(self, cfg: ArchiverWorkspace, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self._names: dict[str, str] = {}
        self._store = ArchiveStore(root)

    def ingest_event(self, client, event: dict) -> str:
        """Ingest a human channel event; skip unsupported events without guessing."""
        if event.get("channel_type") not in ("channel", "group"):
            return "skipped-scope"
        subtype = str(event.get("subtype") or "")
        channel_id = str(event.get("channel") or "")
        if not channel_id:
            return "skipped-channel"
        if channel_id not in self.cfg.allowed_channels:
            return "skipped-allowlist"
        info = client.conversations_info(channel=channel_id).get("channel") or {}
        if info.get("id") != channel_id or not info.get("is_member"):
            return "skipped-membership"
        channel = "#" + str(info.get("name") or "")
        if not should_collect(channel):
            return "skipped-rule"

        if subtype in ("message_changed", "message_deleted"):
            return self._ingest_revision(client, event, channel, channel_id)
        if event.get("bot_id") or subtype not in ("", "file_share"):
            return "skipped-bot-or-system"
        if not event.get("user") or not event.get("ts"):
            return "skipped-identity"

        from .archive.files import attachment_storage

        storage = attachment_storage(self.root, self.cfg.key, channel_id)
        staged: list = []
        messages = _messages_from(
            client, event, self.cfg.bot_token, self._names, storage,
            workspace=self.cfg.key, channel_id=channel_id, staged_out=staged,
            attachment_line_selector=lambda item: raw_lines_for(
                item, separate=self.cfg.separate_attachments
            ),
        )
        if not messages:
            return "skipped-empty"
        if staged:
            write_attachment_docs(
                self.root,
                staged,
                workspace=self.cfg.key,
                channel_id=channel_id,
                channel=channel,
                visibility="private" if info.get("is_private") else "public",
                acl=frozenset({channel}),
            )
        result = writer.ingest(
            self.root,
            workspace=self.cfg.key,
            channel=channel,
            channel_id=channel_id,
            messages=messages,
            acl=[channel],
        )
        if staged:
            try:
                confirm_archived(
                    self._store, staged, workspace=self.cfg.key, channel_id=channel_id
                )
            except Exception:
                log.exception("[%s] attachment archive confirmation failed", self.cfg.key)
                return "metadata-unconfirmed"
        if result.refused:
            return "partial" if result.written else "refused"
        if (
            (event.get("text") or "").strip()
            and not self._record_revision(
                channel_id=channel_id,
                message_ts=str(event["ts"]),
                kind="create",
                body=str(event.get("text") or "").strip(),
                author_id=str(event.get("user") or ""),
                doc_path=self._relative_doc_path(result.path),
            )
        ):
            return "metadata-unconfirmed"
        return "written" if result.written else "duplicate"

    def _speaker(self, client, user_id: str) -> str:
        if user_id not in self._names:
            try:
                user = client.users_info(user=user_id)["user"]
                self._names[user_id] = (
                    user.get("profile", {}).get("real_name")
                    or user.get("name")
                    or user_id
                )
            except (KeyError, SlackApiError):
                self._names[user_id] = user_id
        return self._names[user_id]

    def _ingest_revision(self, client, event: dict, channel: str, channel_id: str) -> str:
        subtype = str(event.get("subtype") or "")
        current = event.get("message") or {}
        previous = event.get("previous_message") or {}
        source = current if subtype == "message_changed" else previous
        if source.get("bot_id"):
            return "skipped-bot-or-system"
        message_ts = str(source.get("ts") or event.get("deleted_ts") or "")
        user_id = str(source.get("user") or previous.get("user") or "")
        if not message_ts or not user_id:
            return "skipped-identity"
        event_ts = str(event.get("event_ts") or event.get("ts") or message_ts)
        when = datetime.fromtimestamp(float(event_ts), tz=UTC)
        speaker = self._speaker(client, user_id)
        old_body = str(previous.get("text") or "").strip()

        if subtype == "message_changed":
            new_body = str(current.get("text") or "").strip()
            if not new_body:
                return "skipped-empty"
            messages = [
                writer.IncomingMessage(
                    when, speaker, f"[수정 전] {old_body}",
                    dedupe_key=f"revision:{message_ts}:{event_ts}:before",
                    source_ts=message_ts,
                ),
                writer.IncomingMessage(
                    when, speaker, f"[수정 후] {new_body}",
                    dedupe_key=f"revision:{message_ts}:{event_ts}:after",
                    source_ts=message_ts,
                ),
            ]
            kind = "change"
            body = new_body
            edited_ts = str((current.get("edited") or {}).get("ts") or event_ts)
        else:
            messages = [
                writer.IncomingMessage(
                    when, speaker, f"[삭제 전] {old_body}",
                    dedupe_key=f"revision:{message_ts}:{event_ts}:before-delete",
                    source_ts=message_ts,
                ),
                writer.IncomingMessage(
                    when, speaker, "[삭제됨] Slack에서 삭제된 메시지",
                    dedupe_key=f"revision:{message_ts}:{event_ts}:deleted",
                    source_ts=message_ts,
                ),
            ]
            kind = "delete"
            body = ""
            edited_ts = event_ts

        result = writer.ingest(
            self.root,
            workspace=self.cfg.key,
            channel=channel,
            channel_id=channel_id,
            messages=messages,
            acl=[channel],
        )
        if result.refused:
            return "partial" if result.written else "refused"
        if not self._record_revision(
            channel_id=channel_id,
            message_ts=message_ts,
            kind=kind,
            body=body,
            edited_ts=edited_ts,
            author_id=user_id,
            doc_path=self._relative_doc_path(result.path),
            previous_body=old_body,
        ):
            return "metadata-unconfirmed"
        return "revision-written" if result.written else "revision-duplicate"

    def _relative_doc_path(self, path: Path | str) -> str:
        candidate = Path(path)
        try:
            return candidate.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            raise ArchiverConfigError("revision document escaped the archive root") from None

    def _record_revision(self, **values) -> bool:
        if not os.getenv("DATABASE_URL"):
            return True
        try:
            record_revision(
                workspace=self.cfg.key,
                **values,
            )
        except Exception:
            log.exception("[%s] revision metadata write failed", self.cfg.key)
            return False
        return True


def _serve(cfg: ArchiverWorkspace, root: Path) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    collector = ShadowCollector(cfg, root)
    app = App(token=cfg.bot_token)
    validate_slack_identity(cfg, app.client.auth_test())

    @app.event("message")
    def on_message(event, client):
        try:
            outcome = collector.ingest_event(client, event)
            log.info("[%s] archive event outcome=%s channel=%s", cfg.key, outcome, event.get("channel"))
        except Exception:
            log.exception("[%s] archive event failed channel=%s", cfg.key, event.get("channel"))

    SocketModeHandler(app, cfg.app_token).start()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    env_file = os.getenv("TYBOT_ENV_FILE", "")
    if not env_file or not Path(env_file).is_absolute() or not Path(env_file).is_file():
        raise ArchiverConfigError("TYBOT_ENV_FILE must name the dedicated archiver env file")
    from dotenv import load_dotenv

    load_dotenv(env_file, override=False)
    log.info("archiver environment loaded from dedicated file")
    configs = load_archiver_workspaces()
    root = shadow_archive_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = instance_lock("archiving-bot-shadow")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise ArchiverConfigError("another archiving shadow collector is running") from exc
    try:
        _serve(configs[0], root)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
