"""One-shot, fail-closed B-81 image-description worker."""
from __future__ import annotations

import argparse
import logging
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tybot.config import state_dir
from tybot.gateway.base import ModelSpec
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.lock import AlreadyRunning, LockUnavailable, instance_lock

from .attachment_reader import all_revisions
from .image_description_coordinator import run_cleared_batch
from .image_description_notification import ImageDescriptionNotice, enqueue, load_recognition
from .image_description_runtime import (
    ImageDescriptionRuntimeStatus,
    failed_status,
    write_status,
)
from .image_description_secret import resolve_provider_key
from .image_description_store import ImageDescriptionStore
from .vision_sensitivity import request_sensitivity

_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,31}$")
_FILE_ID = re.compile(r"^F[A-Z0-9]{8,31}$")
log = logging.getLogger("tybot.archive.image_description_job")
_SAFE_CODES = {
    "runtime_busy",
    "runtime_config_invalid",
    "runtime_database_unavailable",
    "runtime_failed",
    "runtime_lock_unavailable",
    "runtime_provider_unsupported",
}
DISPATCH_TTL_SECONDS = 600
STALE_DISPATCH_SECONDS = 900


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=psycopg.rows.dict_row,
    )


def _provider(name: str, api_key: str):
    if name == "google":
        from tybot.gateway.providers.google_provider import GoogleProvider

        return GoogleProvider(api_key)
    if name == "anthropic":
        from tybot.gateway.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key)
    if name == "openai":
        from tybot.gateway.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key)
    raise RuntimeError("runtime_provider_unsupported")


def _router(policy, *, cost_path: Path, api_key: str) -> Router:
    provider = _provider(policy.provider, api_key)
    spec = ModelSpec(
        model=policy.model,
        provider=policy.provider,
        input_price_per_mtok=0.0,
        output_price_per_mtok=0.0,
        # 요청과 같은 계약 값(`vision_sensitivity.request_sensitivity`). 둘이 갈리면 Router 가 막는다.
        max_sensitivity=request_sensitivity(policy.sensitivity),
    )
    return Router(
        providers={policy.provider: provider},
        registry={policy.model: spec},
        cost_guard=CostGuard(0.01, state_path=cost_path),
        default_model=policy.model,
        fallback_models=(),
    )


def _summary(workspace: str, policy_revision: str, batch) -> ImageDescriptionRuntimeStatus:
    counts = Counter(item.state for item in batch.items)
    known = {
        "approval_required",
        "blocked",
        "closed_without_revision",
        "published",
        "unknown",
    }
    unknown = counts["unknown"] + sum(
        count for state, count in counts.items() if state not in known
    )
    blocked = counts["blocked"] + counts["closed_without_revision"]
    pending = counts["approval_required"]
    state = "partial" if unknown or blocked or pending else "ok"
    return ImageDescriptionRuntimeStatus(
        workspace=workspace,
        state=state,
        ran_at=datetime.now(UTC).isoformat(),
        policy_revision=policy_revision,
        selected=batch.selected,
        skipped=batch.skipped,
        published=counts["published"],
        blocked=blocked,
        pending_approval=pending,
        unknown=unknown,
    )


def run_once(
    workspace: str,
    *,
    dsn: str,
    archive_root: Path,
    runtime_root: Path,
    connect=_connect,
    router_factory=_router,
    secret_resolver=resolve_provider_key,
    revisions=all_revisions,
    now: datetime | None = None,
    limit: int = 50,
) -> ImageDescriptionRuntimeStatus:
    if not _WORKSPACE.fullmatch(workspace) or not archive_root.is_absolute():
        raise RuntimeError("runtime_config_invalid")
    current = now or datetime.now(UTC)
    with connect(dsn) as connection:
        store = ImageDescriptionStore(connection)
        store.reconcile_stale_dispatches(older_than_seconds=STALE_DISPATCH_SECONDS)
        policy, _config, _sources = store.effective_policy(workspace)
        if policy.enabled is not True or policy.workspace_enabled is not True:
            return ImageDescriptionRuntimeStatus(
                workspace=workspace,
                state="disabled",
                ran_at=current.isoformat(),
                policy_revision=policy.revision,
            )
        api_key = secret_resolver(connection, policy.provider)
        router = router_factory(
            policy,
            cost_path=runtime_root / "image-description" / "cost-state.json",
            api_key=api_key,
        )
        def notify(item) -> None:
            # The worker owns no Slack token.  It queues the source coordinates
            # and the *published* recognition (guard-approved description, tags
            # and masked OCR) so the uploader can check it (owner 2026-11-20).
            # A failed lookup still sends the fixed completion text.
            try:
                recognition = load_recognition(connection, item.revision_id)
            except Exception:  # noqa: BLE001 - DB detail stays out of the notice
                log.warning(
                    "image recognition lookup failed ws=%s job=%s", item.workspace, item.job_id
                )
                recognition = {}
            queued = enqueue(ImageDescriptionNotice(
                job_id=item.job_id,
                workspace=item.workspace,
                channel_id=item.channel_id,
                message_ts=item.message_ts,
                **recognition,
            ), root=runtime_root)
            if not queued:
                log.warning("image completion notice was not queued ws=%s job=%s", item.workspace, item.job_id)
        batch = run_cleared_batch(
            revisions(archive_root),
            workspace=workspace,
            store=store,
            router=router,
            archive_root=archive_root,
            dispatch_expires_at=current + timedelta(seconds=DISPATCH_TTL_SECONDS),
            now=current,
            limit=limit,
            completion_notifier=notify,
        )
        code_counts = Counter(
            code
            for item in batch.items
            if (code := getattr(item, "code", ""))
        )
        if code_counts:
            log.warning(
                "image description batch blockers ws=%s codes=%s",
                workspace,
                ",".join(
                    f"{code}:{count}" for code, count in sorted(code_counts.items())
                ),
            )
    return _summary(workspace, policy.revision, batch)


def _target_revisions(channel_id: str, file_id: str):
    """Return an archive reader restricted to one exact Slack attachment."""
    if not _CHANNEL_ID.fullmatch(channel_id) or not _FILE_ID.fullmatch(file_id):
        raise RuntimeError("runtime_config_invalid")

    def selected(root: Path | str):
        return [
            revision
            for revision in all_revisions(root)
            if revision.channel_id == channel_id and revision.file_id == file_id
        ]

    return selected


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one B-81 image-description batch")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--channel-id", default="")
    parser.add_argument("--file-id", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    workspace = args.workspace.strip().lower()
    if not _WORKSPACE.fullmatch(workspace):
        return 2
    channel_id = args.channel_id.strip().upper()
    file_id = args.file_id.strip().upper()
    if bool(channel_id) != bool(file_id):
        return 2
    if channel_id and (
        not _CHANNEL_ID.fullmatch(channel_id) or not _FILE_ID.fullmatch(file_id)
    ):
        return 2
    root = state_dir()
    code = ""
    try:
        if not 1 <= args.limit <= 200:
            raise RuntimeError("runtime_config_invalid")
        from tybot.console.vision_dsn import resolve_vision_database_url

        dsn = resolve_vision_database_url()
        archive = Path(os.environ.get("ARCHIVE_DIR", ""))
        lock = instance_lock(f"image-description-{workspace}", dsn=dsn)
        lock.acquire()
        try:
            run_options = {}
            if channel_id:
                run_options["revisions"] = _target_revisions(channel_id, file_id)
            status = run_once(
                workspace,
                dsn=dsn,
                archive_root=archive,
                runtime_root=root,
                limit=args.limit,
                **run_options,
            )
        finally:
            lock.release()
    except AlreadyRunning:
        code = "runtime_busy"
    except LockUnavailable:
        code = "runtime_lock_unavailable"
    except Exception as exc:  # noqa: BLE001 - never expose provider/DB exception text
        candidate = exc.args[0] if exc.args else ""
        code = candidate if candidate in _SAFE_CODES else "runtime_failed"
    if code:
        status = failed_status(workspace, code)
    write_status(root, status)
    return 0 if status.state != "failed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
