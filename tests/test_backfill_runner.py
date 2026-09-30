from __future__ import annotations

from tybot.archive.backfill_runner import BackfillRunner


class Jobs:
    def __init__(self, job):
        self.queued = job
        self.finished = None
        self.saved = []
        self.interrupted = 0

    def claim_backfill(self, workspace):
        if self.queued is None or self.queued["workspace"] != workspace:
            return None
        job, self.queued = self.queued, None
        return job

    def cursors(self, workspace):
        return []

    def save_history_cursor(self, workspace, channel_id, ts):
        self.saved.append((workspace, channel_id, ts))

    def finish_backfill(self, job_id, workspace, **result):
        self.finished = (job_id, workspace, result)

    def fail_interrupted_backfills(self, workspace):
        self.interrupted += 1
        return 2


class Membership:
    def __init__(self, rows):
        self.rows = rows

    def channels(self, workspace):
        return [dict(row) for row in self.rows]


class Slack:
    def __init__(self, messages=None, denied=False):
        self.messages = messages or []
        self.denied = denied

    def conversations_history(self, **kwargs):
        if self.denied:
            raise RuntimeError("channel_not_found")
        return {"messages": self.messages, "response_metadata": {"next_cursor": ""}}


class Collector:
    def __init__(self, result="written"):
        self.result = result
        self.received = []

    def _collection_channel(self, client, channel_id):
        return "#test", False

    def ingest_message(self, client, event, *, channel, channel_id, is_private):
        self.received.append(event["ts"])
        return self.result


CHANNEL = {"channel_id": "C1", "channel_name": "test", "membership": "joined",
           "operator_hold": False, "mode": "shadow"}
MESSAGE = {"ts": "100.000100", "user": "U1", "text": "source"}


def make_runner(*, dry_run=True, rows=None, messages=None, denied=False,
                result="written", channel_id="C1"):
    job = {"id": 7, "workspace": "tyit", "channel_id": channel_id,
           "dry_run": dry_run, "from_ts": "", "to_ts": ""}
    jobs = Jobs(job)
    collector = Collector(result)
    runner = BackfillRunner("tyit", Slack(messages, denied), collector,
                            jobs, Membership([CHANNEL] if rows is None else rows))
    return runner, jobs, collector


def test_preview_counts_without_writing_or_moving_cursor():
    runner, jobs, collector = make_runner(messages=[MESSAGE])

    assert runner.run_once() is True
    assert jobs.finished[2]["state"] == "succeeded"
    assert jobs.finished[2]["found"] == 1
    assert collector.received == []
    assert jobs.saved == []
    assert runner.run_once() is False


def test_execution_uses_collector_and_advances_cursor_after_write():
    runner, jobs, collector = make_runner(dry_run=False, messages=[MESSAGE])

    runner.run_once()

    assert collector.received == ["100.000100"]
    assert jobs.saved == [("tyit", "C1", "100.000100")]
    assert jobs.finished[2]["written"] == 1
    assert jobs.finished[2]["state"] == "succeeded"


def test_partial_write_does_not_advance_cursor():
    runner, jobs, _ = make_runner(dry_run=False, messages=[MESSAGE], result="partial")

    runner.run_once()

    assert jobs.saved == []
    assert jobs.finished[2]["state"] == "failed"
    assert jobs.finished[2]["failed"] == 1


def test_denied_channel_is_not_reported_as_zero_message_success():
    runner, jobs, _ = make_runner(denied=True)

    runner.run_once()

    assert jobs.finished[2]["state"] == "failed"
    assert jobs.finished[2]["error_code"] == "channel_denied"


def test_unjoined_or_held_channel_is_not_read():
    runner, jobs, _ = make_runner(rows=[{**CHANNEL, "operator_hold": True}])

    runner.run_once()

    assert jobs.finished[2]["state"] == "failed"
    assert jobs.finished[2]["error_code"] == "no_collectible_channels"


def test_job_claim_is_scoped_to_the_workspace():
    runner, jobs, _ = make_runner()
    jobs.queued["workspace"] = "mgmt"

    assert runner.run_once() is False
    assert jobs.finished is None


def test_restart_marks_interrupted_job_failed():
    runner, jobs, _ = make_runner()

    assert runner.recover_interrupted() == 2
    assert jobs.interrupted == 1
