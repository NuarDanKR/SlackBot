"""콘솔에서 시작한 채널별 요약 검토 DM 작업."""
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

logger = logging.getLogger("tybot.console.review_jobs")
ACTIVE = {"queued", "running"}
_START_LOCK = threading.Lock()
_WORKSPACE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
_CHANNEL_RE = re.compile(r"^[A-Z][A-Z0-9]+$")
# 한 번에 도는 채널 수. Canvas 생성과 LLM 호출이 채널마다 붙으므로 무제한이면
# 작업 하나가 몇 시간을 잡는다.
MAX_TARGETS = 20


class ReviewJobError(RuntimeError):
    """요약 검토 DM 작업 요청 또는 상태 저장 실패."""


def jobs_dir() -> Path:
    return state_dir() / "review-jobs"


def _job_path(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef" for ch in job_id):
        raise ReviewJobError("올바르지 않은 작업 ID입니다.")
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
        logger.warning("요약 검토 작업 상태를 읽지 못했습니다: %s", path)
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
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    job["logTail"] = lines[-30:]
    return job


def _targets(raw) -> list[tuple[str, str]]:
    """저장된 대상 목록을 검증해 돌려준다. 옛 단일 채널 작업도 읽는다."""
    out: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            workspace = str(item.get("workspace") or "")
            channel_id = str(item.get("channelId") or "")
        else:
            workspace, _, channel_id = str(item).partition(":")
        if not _WORKSPACE_RE.fullmatch(workspace) or not _CHANNEL_RE.fullmatch(channel_id):
            raise ReviewJobError("워크스페이스 또는 채널 ID 형식이 올바르지 않습니다.")
        out.append((workspace, channel_id))
    if not out:
        raise ReviewJobError("실행할 채널이 없습니다.")
    # 같은 채널을 두 번 넣으면 재발송이 두 번 간다.
    return list(dict.fromkeys(out))


def start(targets: list[tuple[str, str]], *, actor: str, resend: bool = False) -> dict:
    pairs = _targets([{"workspace": ws, "channelId": ch} for ws, ch in targets])
    if len(pairs) > MAX_TARGETS:
        raise ReviewJobError(f"한 번에 최대 {MAX_TARGETS}개 채널까지 실행합니다.")
    with _START_LOCK:
        active = next((row for row in list_jobs() if row.get("status") in ACTIVE), None)
        if active:
            raise ReviewJobError(f"요약 검토 작업 #{active['id'][:8]}이 이미 실행 중입니다.")
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "queued",
            "actor": actor,
            "targets": [{"workspace": ws, "channelId": ch} for ws, ch in pairs],
            # 옛 화면과 옛 작업 파일 호환. 대상 수는 `targets` 가 알고 있다.
            "workspace": pairs[0][0],
            "channelId": pairs[0][1],
            "resend": bool(resend),
            "createdAt": datetime.now(UTC).isoformat(),
            "startedAt": None,
            "finishedAt": None,
            "pid": None,
            "exitCode": None,
            "errorCode": None,
            "outcome": None,
            "result": None,
        }
        _write(job)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "tybot.console.review_jobs", "worker", job_id],
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
            raise ReviewJobError(f"요약 검토 작업을 시작하지 못했습니다: {exc}") from exc
        current = _read(_job_path(job_id)) or job
        if current.get("status") == "queued":
            current["pid"] = process.pid
            _write(current)
        return current


def _command(job: dict, result_path: Path) -> list[str]:
    raw = job.get("targets")
    if not raw:
        raw = [{"workspace": job.get("workspace"), "channelId": job.get("channelId")}]
    command = [sys.executable, "-u", "-m", "tybot.daily_review", "--force-now"]
    for workspace, channel_id in _targets(raw):
        command += ["--target", f"{workspace}:{channel_id}"]
    if job.get("resend"):
        command.append("--resend")
    command += ["--result-json", str(result_path)]
    return command


def _outcome(result: dict | None) -> str:
    """무엇이 실제로 일어났는가. **종료 코드로는 알 수 없다.**

    「보낼 것이 없었다」 를 성공과 같은 글자로 보이면 운영자는 DM 이 간 줄 안다.
    """
    if not result:
        return "unknown"
    if int(result.get("sent") or 0) <= 0:
        return "nothing-sent"
    if int(result.get("failed") or 0) > 0:
        return "partial"
    return "sent"


def run_worker(job_id: str) -> int:
    job = _read(_job_path(job_id))
    if not job:
        return 2
    job["status"] = "running"
    job["pid"] = os.getpid()
    job["startedAt"] = datetime.now(UTC).isoformat()
    _write(job)
    log_path = jobs_dir() / f"{job_id}.log"
    result_path = jobs_dir() / f"{job_id}.result"
    try:
        with log_path.open("a", encoding="utf-8") as output:
            result = subprocess.run(
                _command(job, result_path),
                cwd=str(Path(__file__).resolve().parents[3]),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        job["exitCode"] = result.returncode
        job["result"] = _read(result_path)
        job["outcome"] = _outcome(job["result"])
        # 결과 파일이 없으면 **성공으로 치지 않는다.** 실행기는 보낼 것이 없어도
        # 0 으로 끝나므로, 종료 코드만으로는 「안 보냈다」 와 구별할 수 없다.
        if result.returncode != 0:
            job["status"] = "failed"
            job["errorCode"] = "review-runner-failed"
        elif job["result"] is None:
            job["status"] = "failed"
            job["errorCode"] = "result-missing"
        else:
            job["status"] = "completed"
            job["errorCode"] = None
    except Exception:
        logger.exception("콘솔 요약 검토 작업 실패 job=%s", job_id)
        job["status"] = "failed"
        job["errorCode"] = "worker-error"
        job["exitCode"] = 1
    job["finishedAt"] = datetime.now(UTC).isoformat()
    _write(job)
    return int(job.get("exitCode") or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "worker":
        print("이 모듈은 콘솔의 고정된 요약 검토 작업에서만 실행합니다.", file=sys.stderr)
        return 2
    return run_worker(args[1])


if __name__ == "__main__":
    raise SystemExit(main())
