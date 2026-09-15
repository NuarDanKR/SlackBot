"""콘솔에서 시작한 채널 파일·과거 원문 소급 수집 작업.

웹 요청 안에서 수집을 실행하지 않는다. 문서 변환과 Slack 페이지네이션은 오래 걸리므로
고정된 작업 runner를 별도 프로세스로 띄우고 STATE_DIR 아래 비민감 진행 상태만 남긴다.
임의 명령, 경로 또는 환경변수는 API 입력으로 받지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..heartbeat import state_dir

logger = logging.getLogger("tybot.console.collection_jobs")

MODES = {"files", "history"}
ACTIVE = {"queued", "running"}
_START_LOCK = threading.Lock()
_WORKSPACE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_CHANNEL_RE = re.compile(r"^[A-Z][A-Z0-9]+$")


class CollectionJobError(RuntimeError):
    """작업 요청 또는 상태 저장 실패."""


def jobs_dir() -> Path:
    return state_dir() / "collection-jobs"


def _job_path(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef" for ch in job_id):
        raise CollectionJobError("올바르지 않은 작업 ID입니다.")
    return jobs_dir() / f"{job_id}.json"


def _write(job: dict) -> None:
    path = _job_path(str(job["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("수집 작업 상태를 읽지 못했습니다: %s", path)
        return None
    return value if isinstance(value, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _refresh(job: dict) -> dict:
    if job.get("status") in ACTIVE and job.get("pid") and not _pid_alive(int(job["pid"])):
        job["status"] = "failed"
        job["errorCode"] = "worker-stopped"
        job["finishedAt"] = datetime.now(UTC).isoformat()
        _write(job)
    return job


def list_jobs(limit: int = 20) -> list[dict]:
    rows = [_read(path) for path in jobs_dir().glob("*.json")] if jobs_dir().is_dir() else []
    jobs = [_refresh(row) for row in rows if row]
    return sorted(jobs, key=lambda row: str(row.get("createdAt") or ""), reverse=True)[:limit]


def latest() -> dict | None:
    rows = list_jobs(1)
    if not rows:
        return None
    job = dict(rows[0])
    log_path = jobs_dir() / f"{job['id']}.log"
    if log_path.is_file():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            job["logTail"] = lines[-30:]
        except OSError:
            job["logTail"] = []
    else:
        job["logTail"] = []
    return job


def start(mode: str, targets: list[tuple[str, str]], *, actor: str) -> dict:
    if mode not in MODES:
        raise CollectionJobError("지원하지 않는 수집 작업입니다.")
    normalized = sorted(set(targets))
    if not normalized:
        raise CollectionJobError("수집할 채널을 선택하세요.")
    if any(
        not _WORKSPACE_RE.fullmatch(workspace) or not _CHANNEL_RE.fullmatch(channel_id)
        for workspace, channel_id in normalized
    ):
        raise CollectionJobError("워크스페이스 또는 채널 ID 형식이 올바르지 않습니다.")
    if len(normalized) > 500:
        raise CollectionJobError("한 번에 처리할 수 있는 채널은 500개까지입니다.")

    with _START_LOCK:
        active = next((row for row in list_jobs() if row.get("status") in ACTIVE), None)
        if active:
            raise CollectionJobError(f"수집 작업 #{active['id'][:8]}이 이미 실행 중입니다.")
        job_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        job = {
            "id": job_id,
            "mode": mode,
            "status": "queued",
            "actor": actor,
            "targets": [
                {"workspace": workspace, "channelId": channel_id}
                for workspace, channel_id in normalized
            ],
            "targetCount": len(normalized),
            "createdAt": now,
            "startedAt": None,
            "finishedAt": None,
            "pid": None,
            "exitCode": None,
            "errorCode": None,
        }
        _write(job)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "tybot.console.collection_jobs", "worker", job_id],
                cwd=str(Path(__file__).resolve().parents[3]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            job["status"] = "failed"
            job["errorCode"] = "worker-start-failed"
            job["finishedAt"] = datetime.now(UTC).isoformat()
            _write(job)
            raise CollectionJobError(f"수집 작업을 시작하지 못했습니다: {exc}") from exc
        current = _read(_job_path(job_id)) or job
        if current.get("status") == "queued":
            current["pid"] = process.pid
            _write(current)
        return current


def _command(job: dict) -> list[str]:
    root = Path(__file__).resolve().parents[3]
    script_name = (
        "sync_channel_files.py" if job.get("mode") == "files" else "backfill_channel_history.py"
    )
    command = [sys.executable, "-u", str(root / "scripts" / script_name)]
    workspaces: list[str] = []
    channels: list[str] = []
    for target in job.get("targets") or []:
        workspace = str(target.get("workspace") or "")
        channel_id = str(target.get("channelId") or "")
        if not _WORKSPACE_RE.fullmatch(workspace) or not _CHANNEL_RE.fullmatch(channel_id):
            raise CollectionJobError("저장된 수집 대상 형식이 올바르지 않습니다.")
        if workspace and workspace not in workspaces:
            workspaces.append(workspace)
        if channel_id and channel_id not in channels:
            channels.append(channel_id)
    for workspace in workspaces:
        command.extend(("--workspace", workspace))
    for channel_id in channels:
        command.extend(("--channel", channel_id))
    command.append("--apply")
    return command


def run_worker(job_id: str) -> int:
    path = _job_path(job_id)
    job = _read(path)
    if not job or job.get("mode") not in MODES:
        return 2
    job["status"] = "running"
    job["pid"] = os.getpid()
    job["startedAt"] = datetime.now(UTC).isoformat()
    _write(job)
    log_path = jobs_dir() / f"{job_id}.log"
    try:
        with log_path.open("a", encoding="utf-8") as output:
            result = subprocess.run(
                _command(job),
                cwd=str(Path(__file__).resolve().parents[3]),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        job["exitCode"] = result.returncode
        job["status"] = "completed" if result.returncode == 0 else "failed"
        job["errorCode"] = None if result.returncode == 0 else "collector-failed"
    except Exception:
        logger.exception("콘솔 수집 작업 실패 job=%s", job_id)
        job["status"] = "failed"
        job["errorCode"] = "worker-error"
        job["exitCode"] = 1
    job["finishedAt"] = datetime.now(UTC).isoformat()
    _write(job)
    return int(job.get("exitCode") or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "worker":
        print("이 모듈은 콘솔의 고정된 수집 작업에서만 실행합니다.", file=sys.stderr)
        return 2
    return run_worker(args[1])


if __name__ == "__main__":
    raise SystemExit(main())
