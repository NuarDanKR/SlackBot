"""Bounded, read-only inventory job with separate latest-run/last-success state."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tybot.envfile import load_env_file
from tybot.lock import AlreadyRunning, FileLock

log = logging.getLogger(__name__)
SCHEMA = "tybot.archiver.file-audit.status.v1"
SCOPE = "joined-collectible-channel-files-not-folders"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError("symlinked status file")
    fd, name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _load(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("symlinked status file")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "last_run": None, "last_success": None}
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise ValueError("unknown audit state schema")
    return state


def read_status(directory: Path, *, now: datetime | None = None,
                stale_after: timedelta = timedelta(hours=36)) -> dict:
    """Admin-only consumers must show freshness independently of findings."""
    state = _load(directory / "status.json")
    success = state.get("last_success")
    now = now or datetime.now(UTC)
    stamp = datetime.fromisoformat(success["finished_at"]) if success else None
    state["fresh"] = bool(stamp and timedelta(0) <= now - stamp <= stale_after)
    return state


def _summary(report: dict) -> dict:
    if (report.get("read_only") is not True or report.get("scope") != SCOPE
            or type(report.get("complete")) is not bool
            or not isinstance(report.get("workspaces"), list)):
        raise ValueError("invalid audit report")
    workspaces = []
    for row in report["workspaces"]:
        channels = []
        for channel in row["channels"]:
            counts = {
                "missing": len(channel.get("missing_after_record_check", [])),
                "existing_needs_review": len(channel.get("existing_needs_review", [])),
                "shadow_only": len(channel.get("shadow_only_ids", [])),
                "files_seen": channel.get("files_seen", 0),
                "warnings": channel.get("warning_count", 0),
            }
            if any(type(n) is not int or n < 0 for n in counts.values()):
                raise ValueError("invalid audit counts")
            if channel["status"] == "failed":
                counts = dict.fromkeys(counts)
            channels.append({"channel_id": channel["channel_id"],
                             "status": channel["status"], **counts,
                             "list_complete": channel.get("list_complete"),
                             "live_metadata_records": channel.get("live_metadata_records")})
        target = row.get("target_channels")
        if target is not None and (type(target) is not int or target < len(channels)):
            raise ValueError("invalid audit scope count")
        workspaces.append({
            "workspace": row["workspace"], "status": row["status"],
            "channels": channels,
            "membership_mismatches": len(row.get("db_joined_not_in_slack", [])),
            "skipped_channels": row.get("skipped_channels", 0),
            "target_channels": target,
        })
        workspaces[-1]["coverage"] = _coverage([workspaces[-1]])
    complete = bool(workspaces) and all(
        row["status"] == "ok" and all(ch["status"] == "ok" for ch in row["channels"])
        and all(ch["list_complete"] is True for ch in row["channels"])
        and (row["target_channels"] is None or row["target_channels"] == len(row["channels"]))
        and not row["membership_mismatches"] for row in workspaces
    )
    if report["complete"] != complete:
        raise ValueError("inconsistent audit completion")
    counts = {}
    for key in ("missing", "existing_needs_review", "shadow_only", "files_seen", "warnings"):
        values = [ch[key] for ws in workspaces for ch in ws["channels"] if ch[key] is not None]
        counts[key] = sum(values) if values or complete else None
    return {"complete": complete, "counts": counts, "workspaces": workspaces,
            "coverage": _coverage(workspaces)}


def _coverage(workspaces: list[dict]) -> dict:
    targets = [row["target_channels"] for row in workspaces]
    target = sum(targets) if targets and all(n is not None for n in targets) else None
    channels = [channel for row in workspaces for channel in row["channels"]]
    complete = sum(ch["status"] == "ok" and ch["list_complete"] is True for ch in channels)
    return {"target_channels": target, "attempted_channels": len(channels),
            "complete_channels": complete,
            "unconfirmed_channels": target - complete if target is not None else None,
            "unknown_workspaces": sum(n is None for n in targets)}


def run(directory: Path, *, live_root: Path, shadow_root: Path,
        timeout: float = 3600, pause: float = 3.0) -> int:
    """Only the child does Slack/DB reads. Kill and reap it on timeout."""
    if (not math.isfinite(timeout) or timeout <= 0
            or not math.isfinite(pause) or pause < 0):
        raise ValueError("invalid audit budget")
    if not all(path.is_absolute() for path in (directory, live_root, shadow_root)):
        raise ValueError("absolute paths required")
    directory = directory.resolve()
    for root in (live_root.resolve(), shadow_root.resolve()):
        if directory.is_relative_to(root) or root.is_relative_to(directory):
            raise ValueError("audit state and archives must be disjoint")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory / ".run.lock"
    if lock_path.is_symlink():
        raise ValueError("symlinked audit lock")
    lock = FileLock(lock_path, label="archiver file audit")
    try:
        lock.acquire()
    except AlreadyRunning:
        log.info("file audit already running; state unchanged")
        return 3
    try:
        path = directory / "status.json"
        state = _load(path)
        current = {"id": uuid.uuid4().hex, "started_at": _now(),
                   "finished_at": None, "status": "running", "error_code": ""}
        state["last_run"] = current
        _write(path, state)
        code = 1
        try:
            with tempfile.TemporaryDirectory(prefix=".audit-", dir=directory) as temporary:
                output = Path(temporary) / "report.json"
                script = Path(__file__).resolve().parents[3] / "scripts/audit_archiver_files.py"
                result = subprocess.run(
                    [sys.executable, str(script), "--all-workspaces", "--pause", str(pause),
                     "--result-json", str(output)],
                    stdout=subprocess.DEVNULL, check=False, timeout=timeout,
                )
                if result.returncode not in (0, 2):
                    current.update(status="failed", error_code="audit_process_failed")
                else:
                    summary = _summary(json.loads(output.read_text(encoding="utf-8")))
                    if (result.returncode == 0) != summary["complete"]:
                        raise ValueError("inconsistent audit exit code")
                    current.update(summary)
                    current.update(status="completed" if summary["complete"] else "partial",
                                   error_code="" if summary["complete"] else "audit_incomplete")
                    code = 0 if summary["complete"] else 2
        except subprocess.TimeoutExpired:
            current.update(status="failed", error_code="audit_timeout")
        except Exception as exc:  # noqa: BLE001 - never persist SDK bodies or exception text
            code = 1
            current.update(status="failed", error_code="audit_result_invalid")
            log.warning("file audit failed type=%s", type(exc).__name__)
        current["finished_at"] = _now()
        if current["status"] == "completed":
            state["last_success"] = current.copy()
        _write(path, state)
        log.info("file audit status=%s error=%s", current["status"], current["error_code"] or "-")
        return code
    finally:
        lock.release()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--pause", type=float, default=3.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        load_env_file()
        return run(Path(os.environ.get("STATE_DIR", "/var/lib/tybot")) / "file-audit",
                   live_root=Path(os.environ["ARCHIVE_DIR"]),
                   shadow_root=Path(os.environ["ARCHIVER_SHADOW_DIR"]),
                   timeout=args.timeout, pause=args.pause)
    except Exception as exc:  # noqa: BLE001 - credentials must not reach journald
        log.error("file audit setup failed type=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
