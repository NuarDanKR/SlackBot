"""Local conversion spool. The worker receives bytes and a suffix, never commands."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from .external_convert import ExternalConversionError

JOB_RE = re.compile(r"[0-9a-f]{32}\.(?:json|bin)$")
ERROR_CODES = frozenset({
    "converter_timeout", "converter_crashed", "converter_missing",
    "converter_start_failed", "conversion_failed", "empty_output", "worker_expired",
})


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def request(root: Path, suffix: str, data: bytes, *, timeout: float = 150) -> list[str]:
    key = uuid.uuid4().hex
    inbox, outbox = root / "in", root / "out"
    if (root.is_symlink() or inbox.is_symlink() or outbox.is_symlink()
            or not inbox.is_dir() or not outbox.is_dir()):
        raise ExternalConversionError("변환 worker 폴더가 준비되지 않았습니다", code="worker_unavailable")
    source = inbox / f"{key}.bin"
    manifest = inbox / f"{key}.json"
    result = outbox / f"{key}.json"
    deadline = time.monotonic() + timeout
    try:
        _write(source, data)
        _write(manifest, json.dumps({"suffix": suffix, "expires": time.time() + timeout}).encode())
        while time.monotonic() < deadline:
            if result.is_file() and not result.is_symlink():
                payload = json.loads(result.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid worker response")
                lines = payload.get("lines")
                if (payload.get("status") == "success" and isinstance(lines, list)
                        and lines and all(isinstance(line, str) for line in lines)
                        and any(line.strip() for line in lines)):
                    return lines
                code = payload.get("code", "conversion_failed")
                if not isinstance(code, str) or code not in ERROR_CODES:
                    code = "conversion_failed"
                raise ExternalConversionError(
                    f"변환 worker 실패 code={code}", code=code,
                    retryable=code in {"converter_timeout", "worker_expired"},
                )
            time.sleep(0.2)
        raise ExternalConversionError("변환 worker 응답 시간 초과", code="converter_timeout", retryable=True)
    except (OSError, ValueError) as exc:
        raise ExternalConversionError("Conversion worker I/O failure", code="worker_unavailable") from exc
    finally:
        for path in (manifest, source):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        # The output directory is owned by the worker; cleanup belongs to it.


def process_one(root: Path, manifest: Path) -> bool:
    from .convert import can_convert, convert_local

    if (manifest.parent != root / "in" or manifest.suffix != ".json"
            or not JOB_RE.fullmatch(manifest.name)
            or manifest.is_symlink()):
        return False
    source = manifest.with_suffix(".bin")
    if source.is_symlink() or not source.is_file():
        return False
    result = root / "out" / manifest.name
    if result.exists():
        return False
    code = "conversion_failed"
    try:
        job = json.loads(manifest.read_text(encoding="utf-8"))
        if float(job["expires"]) <= time.time():
            code = "worker_expired"
            raise ValueError("expired")
        suffix = str(job["suffix"])
        if not can_convert(suffix):
            raise ValueError("unsupported suffix")
        lines = convert_local(suffix, source.read_bytes())
        payload = (
            {"status": "success", "lines": lines}
            if lines and any(line.strip() for line in lines)
            else {"status": "failed", "code": "empty_output"}
        )
    except Exception as exc:  # noqa: BLE001 - one document must not kill the worker
        cause = exc
        seen = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if getattr(cause, "code", None):
                code = cause.code if cause.code in ERROR_CODES else "conversion_failed"
                break
            cause = cause.__cause__
        payload = {"status": "failed", "code": code}
    _write(result, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return True


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/var/lib/tybot-convert"))
    args = parser.parse_args()
    # Never recurse back into the client even if a deployment exports this variable.
    os.environ.pop("TYBOT_CONVERT_SPOOL", None)
    # A single local worker owns this spool, including after systemd restarts.
    lock = (args.root / "out" / ".worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        for manifest in sorted((args.root / "in").glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                process_one(args.root, manifest)
        for result in (args.root / "out").glob("*.json"):
            with contextlib.suppress(OSError):
                if result.is_file() and time.time() - result.stat().st_mtime > 3600:
                    result.unlink()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
