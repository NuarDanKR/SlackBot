"""Execute console backfill requests with the active shadow collector's Slack client."""

from __future__ import annotations

import logging

from . import backfill, backfill_adapter, channel_membership
from .supervisor_state import JobState

log = logging.getLogger("tybot.archive.backfill_runner")


class BackfillRunner:
    def __init__(self, workspace: str, client, collector, job_repo, membership_repo):
        self.workspace = workspace
        self.client = client
        self.collector = collector
        self.jobs = job_repo
        self.membership = membership_repo

    def recover_interrupted(self) -> int:
        """Only the holder of the workspace process lock may do this."""
        return self.jobs.fail_interrupted_backfills(self.workspace)

    def run_once(self) -> bool:
        job = self.jobs.claim_backfill(self.workspace)
        if job is None:
            return False
        job_id = int(job["id"])
        try:
            targets = self._targets(str(job.get("channel_id") or ""))
            if not targets:
                self.jobs.finish_backfill(
                    job_id, self.workspace, state=JobState.FAILED,
                    failed=1, error_code="no_collectible_channels",
                )
                return True

            oldest = str(job.get("from_ts") or "")
            latest = str(job.get("to_ts") or "")
            if job["dry_run"]:
                preview = backfill.plan(
                    self.client, targets, workspace=self.workspace,
                    oldest=oldest, latest=latest,
                )
                state = (
                    JobState.FAILED if not preview.channels and preview.denied
                    else JobState.PARTIAL if preview.denied or not preview.exhausted
                    else JobState.SUCCEEDED
                )
                self.jobs.finish_backfill(
                    job_id, self.workspace, state=state,
                    found=preview.found, failed=len(preview.denied),
                    error_code="channel_denied" if preview.denied else "",
                )
            else:
                counts, state = backfill.run(
                    self.client, targets, workspace=self.workspace,
                    ingest=backfill_adapter.make_ingest(self.collector, self.client),
                    save_cursor=lambda target, ts: self.jobs.save_history_cursor(
                        self.workspace, target.channel_id, ts,
                    ),
                    oldest=oldest, latest=latest,
                )
                self.jobs.finish_backfill(
                    job_id, self.workspace, state=state,
                    found=counts.found, written=counts.written,
                    duplicate=counts.duplicate, refused=counts.refused,
                    failed=counts.failed,
                    error_code="incomplete" if state != JobState.SUCCEEDED else "",
                )
        except Exception as exc:
            log.exception("[%s] backfill job %s failed", self.workspace, job_id)
            self.jobs.finish_backfill(
                job_id, self.workspace, state=JobState.FAILED,
                failed=1, error_code=type(exc).__name__,
            )
        return True

    def _targets(self, channel_id: str) -> list[backfill.Target]:
        rows = [
            row for row in self.membership.channels(self.workspace)
            if channel_membership.is_collectible(row)
            and (not channel_id or str(row["channel_id"]) == channel_id)
        ]
        cursors = {
            str(row["channel_id"]): str(row.get("last_history_ts") or "")
            for row in self.jobs.cursors(self.workspace)
        }
        for row in rows:
            row["last_history_ts"] = cursors.get(str(row["channel_id"]), "")
        return backfill_adapter.targets_from(rows, self.workspace)
