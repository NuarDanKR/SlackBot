"""콘솔이 배포를 '요청'하고 결과를 읽는 통로.

## 왜 이런 구조인가
배포는 `git pull` + `install.sh` + `systemctl restart` 이고 전부 root 권한이다.
콘솔(`User=tybot`)에게 그 권한을 주면 **웹 취약점 하나가 서버 장악으로 이어진다.**
그래서 콘솔은 요청 파일만 쓰고, root 로 도는 systemd path 유닛이 그걸 보고 배포한다.

## 주입이 불가능한 이유
요청 파일의 내용은 **감사기록용일 뿐 명령 인자로 쓰이지 않는다.** 브랜치·경로·옵션은
유닛 파일에 고정돼 있다. 웹에서 올 수 있는 값이 실행에 영향을 주는 경로가 없다.

## 배포는 되돌리기 어려운 동작이다
`update.sh` 의 테스트 게이트가 유일한 안전장치다(테스트 실패 시 운영 프로세스 미변경).
그래서 요청은 관리자만, 쿨다운을 두고, 누가 눌렀는지 남긴다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .heartbeat import state_dir

# 연속 클릭으로 배포가 겹치지 않게 한다. 실제 직렬화는 러너의 flock 이 담당하고,
# 이 값은 사용자에게 즉시 거절 이유를 알려주기 위한 것이다.
COOLDOWN_SEC = 60
CONSOLE_STATES = frozenset({"idle", "queued", "running", "ok", "failed", "skipped"})
MAX_DETAIL_LINES = 30
MAX_DETAIL_CHARS = 8_000
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_PATTERNS = (
    (re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]+"), r"\1***"),
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_-]+)"), "***"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]+)"), "***"),
    (re.compile(r"\b(postgresql(?:\+\w+)?://[^\s]+)", re.IGNORECASE), "***"),
    (re.compile(r"\b(password=)[^\s]+", re.IGNORECASE), r"\1***"),
)


def request_path() -> Path:
    return state_dir() / "deploy-request.json"


def status_path() -> Path:
    return state_dir() / "deploy-status.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _write_atomic(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _read(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "손상된 파일"}
    return payload if isinstance(payload, dict) else {"error": "형식 오류"}


def read_status() -> dict | None:
    """마지막 배포 상태. 콘솔이 화면에 그대로 보여줄 수 있는 형태."""
    return _read(status_path())


def sanitize_deploy_detail(text: str) -> str:
    """배포 출력의 마지막 부분만 남기고 콘솔에 노출하면 안 되는 값을 지운다."""
    cleaned = _ANSI_RE.sub("", text.replace("\x00", ""))
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    lines = [line[:500] for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines[-MAX_DETAIL_LINES:])[-MAX_DETAIL_CHARS:]


def console_status() -> dict:
    """콘솔용 배포 상태. 내부 파일 경로는 노출하지 않는다."""
    status = read_status() or {}
    pending = request_path().exists()
    request = _read(request_path()) if pending else None
    state = str(status.get("state") or "idle")
    if pending and state != "running":
        state = "queued"
    if state not in CONSOLE_STATES:
        state = "failed"
    queued = state == "queued"
    return {
        "state": state,
        "pending": pending,
        "actor": str((request or {}).get("actor") or status.get("actor") or ""),
        "before": "" if queued else str(status.get("before") or ""),
        "after": "" if queued else str(status.get("after") or ""),
        "beforeTitle": "" if queued else str(status.get("before_title") or ""),
        "afterTitle": "" if queued else str(status.get("after_title") or ""),
        "message": "배포 요청을 처리 대기 중입니다." if queued else str(status.get("message") or ""),
        "detail": "" if queued else str(status.get("detail") or ""),
        "requestedAt": (request or {}).get("requested_at"),
        "startedAt": None if queued else status.get("started_at"),
        "finishedAt": None if queued else status.get("finished_at"),
    }


def is_running() -> bool:
    st = read_status() or {}
    return st.get("state") == "running"


def seconds_since_last() -> float | None:
    st = read_status() or {}
    stamp = st.get("finished_at") or st.get("started_at")
    if not stamp:
        return None
    try:
        return (_now() - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return None


def request_deploy(actor: str, *, note: str = "") -> dict:
    """배포를 요청한다. 거절되면 `{"ok": False, "reason": ...}` 를 돌려준다.

    거절 사유를 예외가 아니라 값으로 주는 이유: 콘솔이 화면에 그대로 띄우면 되고,
    '요청이 실패했는지 배포가 실패했는지' 를 사용자가 구분할 수 있다.
    """
    if is_running():
        return {"ok": False, "reason": "배포가 이미 진행 중입니다."}
    if request_path().exists():
        return {"ok": False, "reason": "직전 요청이 아직 처리되지 않았습니다."}
    since = seconds_since_last()
    if since is not None and since < COOLDOWN_SEC:
        wait = int(COOLDOWN_SEC - since)
        return {"ok": False, "reason": f"직전 배포 직후입니다. {wait}초 뒤 다시 시도하세요."}

    payload = {
        "requested_at": _now().isoformat(timespec="seconds"),
        "actor": actor,
        # 자유 입력은 기록만 한다 - 실행에 쓰이지 않는다. 길이를 잘라 로그 오염을 막는다.
        "note": (note or "")[:200],
    }
    _write_atomic(request_path(), payload)
    return {"ok": True, "requested_at": payload["requested_at"]}


def consume_request() -> dict | None:
    """요청을 한 번만 소비한다(러너가 호출). 손상된 요청도 지워 반복 실행을 막는다."""
    path = request_path()
    payload = _read(path)
    if payload is None:
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return payload


def write_status(
    state: str,
    *,
    actor: str = "",
    before: str = "",
    after: str = "",
    before_title: str = "",
    after_title: str = "",
    message: str = "",
    detail: str = "",
    started_at: str | None = None,
) -> dict:
    """배포 진행 상태를 남긴다. 콘솔이 읽으므로 0644 로 둔다(시크릿 없음)."""
    if started_at is None and state != "running":
        started_at = str((read_status() or {}).get("started_at") or "") or None
    payload = {
        "state": state,  # running | ok | failed | skipped
        "actor": actor,
        "before": before,
        "after": after,
        "before_title": before_title,
        "after_title": after_title,
        "message": message,
        "detail": sanitize_deploy_detail(detail),
        "started_at": started_at or _now().isoformat(timespec="seconds"),
    }
    if state != "running":
        payload["finished_at"] = _now().isoformat(timespec="seconds")
    _write_atomic(status_path(), payload, mode=0o644)
    return payload
