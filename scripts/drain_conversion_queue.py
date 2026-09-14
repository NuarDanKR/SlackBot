#!/usr/bin/env python3
"""첨부 변환 재처리 큐를 비운다 (B-45 §5).

    python scripts/drain_conversion_queue.py            # 판정만(무엇을 할지 보여준다)
    python scripts/drain_conversion_queue.py --apply    # 실제로 다시 변환한다
    python scripts/drain_conversion_queue.py --status   # 큐·회로 상태만

## 무엇을 하나

1. **임대가 끝난 작업을 회수한다.** 잡고 있던 프로세스가 죽으면 그 작업은
   아무도 손대지 않은 채 남는다 — 큐의 가장 흔한 고장이다. 재시작 뒤 첫 할 일이
   이것이라, `--status` 로도 먼저 돈다.
2. 실행할 작업을 잡는다(`claim`). 같은 행을 두 프로세스가 집지 않는다.
3. 좌표로 staging 메타데이터를 열어 **그 파일 하나만** 다시 변환한다.
4. 성공하면 산출물을 원자적으로 바꾸고 작업을 닫는다. 실패하면 다음 시각을 잡는다.

## 하지 않는 것

- **원본을 지우지 않는다.** 재변환이 실패해도 다음 사람이 볼 것이 남아야 한다.
- **이전 유효 산출물을 미리 지우지 않는다.** 새 결과가 검증을 통과한 뒤에만
  바꾼다. 먼저 지우면 실패했을 때 있던 것까지 사라진다.
- **아카이브 원문(`## 원문`)을 고치지 않는다.** 재변환 결과의 아카이브 반영은
  `convert_staged_attachments.py --apply` 가 기존 중복 방지 경로로 한다.
- `pii_refused` 는 건드리지 않는다. 정책 제외를 기술 실패처럼 자동 해제하지 않는다.

종료 코드: `0` 정상 · `1` 처리 중 실패한 작업 있음 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot import conversion_queue as queue
from tybot.envfile import load_env_file

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2


def owner_name() -> str:
    """누가 잡았는지. 회수할 때 누구 것이었는지 알아야 원인을 되짚는다."""
    return f"{socket.gethostname()}:{os.getpid()}"


def staging_meta(archive_dir: str, job: queue.Job) -> pathlib.Path:
    """작업 좌표 → metadata 경로. **큐에서 온 값을 경로로 쓰기 전에 검사한다.**

    큐는 DB 다. DB 에서 읽은 값을 그대로 경로에 붙이면 그 자리가 곧 경로 탈출이다.
    """
    from tybot.archive.files import _safe_component

    root = pathlib.Path(archive_dir).parent / "staging" / "workspaces"
    return (
        root
        / _safe_component(job.workspace)
        / "channels"
        / _safe_component(job.channel_id)
        / "attachments"
        / _safe_component(job.file_id)
        / "metadata.json"
    )


def reconvert(meta_path: pathlib.Path) -> tuple[bool, str, bool]:
    """파일 하나를 다시 변환한다. `(성공, 오류코드, 재시도가능)`.

    산출물은 **검증을 통과한 뒤에** 바꾼다. 먼저 지우면 실패했을 때 있던 것까지
    사라지고, 그건 재처리가 자료를 늘리는 게 아니라 줄이는 일이 된다.
    """
    from tybot.archive.convert import ConvertError, convert
    from tybot.archive.external_convert import failure_details
    from tybot.archive.writer import screen

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "metadata_unreadable", False

    if str(meta.get("status") or "") == "pii_refused":
        # 정책 제외는 기술 실패가 아니다. 자동으로 풀지 않는다.
        return False, "pii_refused", False

    raw_path = meta.get("object_path")
    if not raw_path or not pathlib.Path(raw_path).is_file():
        return False, "original_missing", False

    try:
        raw = pathlib.Path(raw_path).read_bytes()
        body = convert(str(meta.get("filetype") or ""), raw)
    except (ConvertError, OSError) as exc:
        code, retryable = failure_details(exc)
        return False, code, retryable

    text = "\n".join(body)
    refused = next((reason for line in text.splitlines() if (reason := screen(line))), None)
    if refused:
        _write_meta(meta_path, meta, status="pii_refused", code="pii_refused", body=None)
        return False, "pii_refused", False
    if not text.strip():
        return False, "empty_output", False

    _write_meta(meta_path, meta, status="converted", code="", body=body)
    return True, "", False


def _write_meta(meta_path: pathlib.Path, meta: dict, *, status: str, code: str, body) -> None:
    """메타데이터와 미리보기를 **원자적으로** 바꾼다."""
    meta.update({
        "status": status,
        "conversion_state": "blocked" if status == "pii_refused" else
                            "succeeded" if body is not None else "failed",
        "error_code": code,
        "retryable": False,
        "error": None if body is not None else meta.get("error"),
        "extracted": body is not None,
        "reprocessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(meta_path)

    preview = meta_path.parent / "extracted.md"
    if body is None:
        return
    tmp_preview = preview.with_suffix(".tmp")
    tmp_preview.write_text(
        "<!-- 로컬 재변환본. 아카이브 기록 시 PII 검사 적용 -->\n"
        f"# {meta.get('name') or ''}\n\n" + "\n".join(body).rstrip() + "\n",
        encoding="utf-8",
    )
    tmp_preview.replace(preview)


def show_status() -> int:
    reclaimed = queue.reclaim_expired()
    counts = queue.summary()
    print(f"임대 회수: {reclaimed}건")
    if not counts:
        print("큐가 비어 있습니다.")
        return EXIT_OK
    for state in queue.STATES:
        if counts.get(state):
            print(f"  {state}: {counts[state]}건")
    held = counts.get(queue.HELD, 0)
    if held:
        print(
            f"\n※ {held}건이 `held` 입니다 — **파일이 아니라 환경 문제**입니다. "
            "변환기 설치·권한을 확인한 뒤 풀어 주세요."
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="실제로 다시 변환한다")
    ap.add_argument("--status", action="store_true", help="큐 상태만 보여준다")
    ap.add_argument("--limit", type=int, default=20, help="한 번에 처리할 작업 수")
    ap.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    ap.add_argument("--env", default=os.getenv("TYBOT_ENV_FILE", ""))
    args = ap.parse_args(argv)

    if args.env:
        load_env_file(args.env)

    try:
        if args.status:
            return show_status()

        queue.reclaim_expired()
        if not args.apply:
            counts = queue.summary()
            ready = counts.get(queue.QUEUED, 0)
            print(f"대기 중인 작업 {ready}건. 실제로 돌리려면 `--apply` 를 붙이세요.")
            return EXIT_OK

        jobs = queue.claim(owner_name(), limit=args.limit)
    except queue.QueueUnavailable as exc:
        print(f"큐를 쓸 수 없습니다: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if not jobs:
        print("처리할 작업이 없습니다.")
        return EXIT_OK

    failed = 0
    for job in jobs:
        meta_path = staging_meta(args.archive, job)
        if not meta_path.is_file():
            # 좌표는 있는데 파일이 없다. **되풀이하지 않는다** — 같은 결과가 나온다.
            queue.fail(job.id, error_code="staging_missing", retryable=False)
            print(f"  건너뜀(메타데이터 없음): {job.log_line()}")
            failed += 1
            continue
        ok, code, retryable = reconvert(meta_path)
        if ok:
            queue.succeed(job.id)
            print(f"  변환 성공: {job.log_line()}")
            continue
        state = queue.fail(job.id, error_code=code, retryable=retryable)
        failed += 1
        print(f"  실패({code}) -> {state}: {job.log_line()}")

    print(f"\n처리 {len(jobs)}건 · 실패 {failed}건")
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
