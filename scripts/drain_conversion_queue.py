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
4. 기존 첨부 표시가 있는 원문에 변환본을 멱등하게 추가하고 반영을 확인한다.
5. 해당 원문 문서를 검색 색인에 넣은 뒤에만 작업을 닫는다.

## 하지 않는 것

- **원본을 지우지 않는다.** 재변환이 실패해도 다음 사람이 볼 것이 남아야 한다.
- **이전 유효 산출물을 미리 지우지 않는다.** 새 결과가 검증을 통과한 뒤에만
  바꾼다. 먼저 지우면 실패했을 때 있던 것까지 사라진다.
- **아카이브의 기존 원문 줄을 고치지 않는다.** 재변환 결과는 기존 첨부 표시와
  같은 시각·화자로 새 줄만 덧붙이며 `writer.ingest`의 중복 방지를 거친다.
- `pii_refused` 는 건드리지 않는다. 정책 제외를 기술 실패처럼 자동 해제하지 않는다.

종료 코드: `0` 정상 · `1` 처리 중 실패한 작업 있음 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import socket
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

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


def reconvert(meta_path: pathlib.Path, *, expected_sha256: str = "") -> tuple[bool, str, bool]:
    """파일 하나를 다시 변환한다. `(성공, 오류코드, 재시도가능)`.

    산출물은 **검증을 통과한 뒤에** 바꾼다. 먼저 지우면 실패했을 때 있던 것까지
    사라지고, 그건 재처리가 자료를 늘리는 게 아니라 줄이는 일이 된다.
    """
    from tybot.archive.convert import ConvertError, convert_with_coverage
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
        _write_failure_meta(meta_path, meta, "original_missing", retryable=False)
        return False, "original_missing", False

    coverage = None
    try:
        raw = pathlib.Path(raw_path).read_bytes()
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            _write_failure_meta(meta_path, meta, "original_changed", retryable=False)
            return False, "original_changed", False
        body, coverage = convert_with_coverage(str(meta.get("filetype") or ""), raw)
    except (ConvertError, OSError) as exc:
        code, retryable = failure_details(exc)
        _write_failure_meta(meta_path, meta, code, retryable=retryable)
        return False, code, retryable

    text = "\n".join(body)
    refused = next((reason for line in text.splitlines() if (reason := screen(line))), None)
    if refused:
        _write_meta(meta_path, meta, status="pii_refused", code="pii_refused", body=None)
        return False, "pii_refused", False
    if not text.strip():
        _write_failure_meta(meta_path, meta, "empty_output", retryable=False)
        return False, "empty_output", False

    _write_meta(meta_path, meta, status="converted", code="", body=body,
                coverage=coverage)
    return True, "", False


def _write_failure_meta(
    meta_path: pathlib.Path, meta: dict, code: str, *, retryable: bool
) -> None:
    """현재 실패 상태만 기록한다. 원본과 이전 유효 미리보기는 건드리지 않는다."""
    meta.update({
        "status": "download_or_extract_failed",
        "conversion_state": "failed",
        "error_code": code,
        "retryable": retryable,
        "reprocessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(meta_path)


def _write_meta(meta_path: pathlib.Path, meta: dict, *, status: str, code: str, body,
                coverage=None) -> None:
    """메타데이터와 미리보기를 **원자적으로** 바꾼다."""
    from tybot.archive.convert import PARTIAL

    converted_state = "succeeded"
    if coverage is not None and coverage.state == PARTIAL:
        # 다 읽지 못했다. **성공으로 닫으면 그 답에 우리 출처가 붙는다.**
        converted_state = "partial"
    meta.update(coverage.to_json() if coverage is not None else {})
    meta.update({
        "status": status,
        "conversion_state": "blocked" if status == "pii_refused" else
                            converted_state if body is not None else "failed",
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


def publish_reconversion(
    archive_dir: str,
    meta_path: pathlib.Path,
    job: queue.Job,
) -> tuple[bool, str, bool]:
    """검증된 재변환본을 기존 원문에 추가하고 검색 색인까지 확인한다.

    변환 미리보기만 생긴 상태는 답변 가능한 상태가 아니다. 원래 첨부 표시의
    시각·화자를 찾아 같은 채널 원문에 추가하며, 좌표가 모호하면 추측하지 않는다.
    """
    from tybot import search_index
    from tybot.archive import writer
    from tybot.archive.store import ArchiveStore
    from tybot.attachment_trace import confirm_archived, line_hash

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        preview = (meta_path.parent / "extracted.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return False, "converted_output_unreadable", False

    name = str(meta.get("name") or "").strip()
    if not name:
        return False, "attachment_name_missing", False
    body = preview.splitlines()
    if body and body[0].startswith("<!--"):
        body.pop(0)
    while body and not body[0].strip():
        body.pop(0)
    if body and body[0].startswith("# "):
        body.pop(0)
    rows = [line.strip() for line in body if line.strip()]
    if not rows:
        return False, "empty_output", False

    store = ArchiveStore(pathlib.Path(archive_dir))
    expected_ts = ""
    raw_origin = str(meta.get("origin_message_ts") or "")
    if raw_origin:
        try:
            expected_ts = datetime.fromtimestamp(float(raw_origin), tz=UTC).astimezone(
                writer.KST
            ).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return False, "origin_timestamp_invalid", False

    candidates = []
    for doc in store.source_docs():
        if doc.workspace != job.workspace or (doc.channel_id or "") != job.channel_id:
            continue
        for line in doc.raw_lines:
            if (
                name not in (line.text or "")
                or not (line.text or "").startswith("[첨부:")
                or (line.text or "").startswith("[첨부:재변환]")
            ):
                continue
            if expected_ts and line.ts != expected_ts:
                continue
            candidates.append((doc, line))
    coordinates = {
        (str(doc.path), line.ts, line.speaker, doc.channel)
        for doc, line in candidates
    }
    if not coordinates:
        return False, "archive_origin_missing", False
    if len(coordinates) != 1:
        return False, "archive_origin_ambiguous", False
    doc, source = candidates[0]
    when = datetime.strptime(source.ts, "%Y-%m-%d %H:%M").replace(tzinfo=writer.KST)
    filetype = str(meta.get("filetype") or pathlib.Path(name).suffix.lstrip(".")).lower()
    size = max(1, int(meta.get("declared_size") or 0) // 1024)
    texts = [f"[첨부:재변환] {name} ({filetype or '?'}, {size}KB)"]
    texts.extend(f"[첨부추출:{name}] {row}" for row in rows)
    messages = [
        writer.IncomingMessage(ts=when, speaker=source.speaker, text=text)
        for text in texts
    ]
    result = writer.ingest(
        archive_dir,
        workspace=job.workspace,
        channel=doc.channel,
        channel_id=job.channel_id,
        messages=messages,
    )
    if result.refused:
        return False, "pii_refused", False

    staged = SimpleNamespace(
        file_id=job.file_id,
        line_hashes=[line_hash(text) for text in texts],
        metadata_path=meta_path,
    )
    states = confirm_archived(
        ArchiveStore(pathlib.Path(archive_dir)),
        [staged],
        workspace=job.workspace,
        channel_id=job.channel_id,
    )
    if states.get(job.file_id) != "archived":
        return False, "archive_write_unconfirmed", True

    try:
        refreshed = ArchiveStore(pathlib.Path(archive_dir))
        changed_paths = {doc.path.resolve()}
        changed_paths.update(pathlib.Path(path).resolve() for path in result.paths)
        docs = [doc for doc in refreshed.docs() if doc.path.resolve() in changed_paths]
        if not docs:
            return False, "archive_document_missing", True
        search_index.reindex(docs, refreshed.root)
    except search_index.IndexError_:
        _update_publish_meta(meta_path, index_state="failed", index_error_code="index_failed")
        return False, "index_failed", True
    _update_publish_meta(
        meta_path,
        index_state="succeeded",
        index_error_code=None,
        indexed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return True, "", False


def _update_publish_meta(meta_path: pathlib.Path, **fields) -> None:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(fields)
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(meta_path)


def backfill(archive_dir: str, *, apply: bool) -> int:
    """이미 쌓여 있는 실패 첨부를 큐에 올린다.

    **이것이 없으면 이번 작업이 아무것도 고치지 못한다.** `enqueue` 는 앞으로
    들어올 실패에만 걸리므로, 이미 staging 에 있는 실패는 큐에 없다 — 큐는 비어
    있고 타이머는 도는데 재처리되는 것이 하나도 없는 상태가 된다.

    **`force` 로 올린다.** 그때의 자동 판정은 그때의 변환기를 기준으로 한 것이고,
    변환기를 고친 뒤에는 결과가 달라질 수 있다. 다만 `pii_refused` 는 올리지
    않는다 — 정책 제외는 변환기와 무관하다.

    기본은 판정만. `--apply` 가 있어야 실제로 넣는다.
    """
    from tybot.attachment_review import PII_REFUSED, scan

    items = [
        item for item in scan(archive_dir)
        if (item.conversion_failed or item.status == PII_REFUSED)
    ]
    policy_excluded = [item for item in items if item.status == PII_REFUSED]
    technical = [item for item in items if item.status != PII_REFUSED]
    unknown_channel = [
        item for item in technical if not item.channel_id or item.channel_id == "unknown"
    ]
    located = [item for item in technical if item not in unknown_channel]
    original_missing = [
        item for item in located
        if item.object_path is None or not item.object_path.is_file()
    ]
    candidates = [item for item in located if item not in original_missing]

    print(
        f"실패 첨부 {len(items)}건 · 큐 대상 {len(candidates)}건 · "
        f"정책 제외 {len(policy_excluded)}건 · 원본 없음 {len(original_missing)}건 · "
        f"채널 미확인 {len(unknown_channel)}건"
    )
    if not candidates:
        return EXIT_OK
    if not apply:
        for item in candidates[:20]:
            print(f"  {item.workspace}/{item.channel_id}/{item.file_id} "
                  f"{item.name} ({item.error_code or item.status})")
        if len(candidates) > 20:
            print(f"  … 외 {len(candidates) - 20}건")
        print("\n실제로 넣으려면 `--apply` 를 붙이세요.")
        return EXIT_OK

    added = 0
    for item in candidates:
        digest = item.sha256 or ""
        if not digest:
            # 위에서 존재를 확인했지만 검사와 읽기 사이에 파일이 사라질 수 있다.
            try:
                digest = hashlib.sha256(item.object_path.read_bytes()).hexdigest()
            except OSError:
                print(
                    f"  건너뜀(원본 소실): {item.workspace}/{item.channel_id}/{item.file_id}"
                )
                continue
        try:
            job_id = queue.enqueue(
                workspace=item.workspace,
                channel_id=item.channel_id,
                file_id=item.file_id,
                original_sha256=digest,
                error_code=item.error_code or "reprocess_requested",
                retryable=True,
                force=True,
            )
        except queue.QueueUnavailable as exc:
            print(f"큐를 쓸 수 없습니다: {exc}", file=sys.stderr)
            return EXIT_INPUT
        if job_id:
            added += 1
    print(
        f"큐에 올린 작업 {added}건. 재처리는 `drain_conversion_queue.py --apply`를 "
        "별도로 실행하거나 타이머를 기다리세요."
    )
    return EXIT_OK


def nightly_backfill(archive_dir: str, *, apply: bool) -> int:
    """재시도 가능한 실패만 야간 큐에 다시 올린다.

    사람의 ``--backfill`` 은 변환기를 고친 뒤 과거 판정을 다시 시험하는 강제
    작업이다. 야간 배치는 무인 작업이므로 범위가 더 좁다. 수집 당시 변환기가
    ``retryable=true`` 로 남겼고 현재 오류 코드 정책도 허용하는 파일만 다룬다.
    """
    from tybot.attachment_review import scan

    failed = [item for item in scan(archive_dir) if item.conversion_failed]
    retryable = [
        item for item in failed
        if queue.is_retryable(item.error_code, item.retryable)
    ]
    excluded = [item for item in failed if item not in retryable]
    unknown_channel = [
        item for item in retryable
        if not item.channel_id or item.channel_id == "unknown"
    ]
    located = [item for item in retryable if item not in unknown_channel]
    original_missing = [
        item for item in located
        if item.object_path is None or not item.object_path.is_file()
    ]
    candidates = [item for item in located if item not in original_missing]

    print(
        f"야간 실패 첨부 {len(failed)}건 · 재시도 대상 {len(candidates)}건 · "
        f"영구 제외 {len(excluded)}건 · 원본 없음 {len(original_missing)}건 · "
        f"채널 미확인 {len(unknown_channel)}건"
    )
    if not candidates:
        return EXIT_OK
    if not apply:
        for item in candidates[:20]:
            print(
                f"  {item.workspace}/{item.channel_id}/{item.file_id} "
                f"{item.name} ({item.error_code or item.status})"
            )
        if len(candidates) > 20:
            print(f"  … 외 {len(candidates) - 20}건")
        print("\n실제로 재시도하려면 `--nightly --apply` 를 사용하세요.")
        return EXIT_OK

    added = 0
    for item in candidates:
        digest = item.sha256 or ""
        if not digest:
            try:
                digest = hashlib.sha256(item.object_path.read_bytes()).hexdigest()
            except OSError:
                print(
                    f"  건너뜀(원본 소실): "
                    f"{item.workspace}/{item.channel_id}/{item.file_id}"
                )
                continue
        try:
            job_id = queue.enqueue(
                workspace=item.workspace,
                channel_id=item.channel_id,
                file_id=item.file_id,
                original_sha256=digest,
                error_code=item.error_code or "reprocess_requested",
                retryable=True,
                force=False,
            )
        except queue.QueueUnavailable as exc:
            print(f"큐를 쓸 수 없습니다: {exc}", file=sys.stderr)
            return EXIT_INPUT
        if job_id:
            added += 1
    print(f"야간 재시도 큐 등록 {added}건. 이어서 즉시 변환합니다.")
    return EXIT_OK


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
    ap.add_argument("--backfill", action="store_true",
                    help="이미 쌓인 실패 첨부를 큐에 올린다(사람이 요청한 재처리)")
    ap.add_argument(
        "--nightly",
        action="store_true",
        help="재시도 가능한 실패 첨부를 큐에 올리고 즉시 처리한다(야간 배치)",
    )
    ap.add_argument("--limit", type=int, default=20, help="한 번에 처리할 작업 수")
    ap.add_argument("--archive", default="")
    args = ap.parse_args(argv)

    # **설정 파일을 먼저 읽는다.** 다른 운영 스크립트와 같은 순서다
    # (`TYBOT_ENV_FILE` → `/etc/tybot/tybot.env` → 저장소 `.env`).
    #
    # 안 읽으면 손으로 돌릴 때 `DATABASE_URL 이 없습니다` 가 나온다. systemd 는
    # unit 에 경로를 박아 두니 거기서만 돌고, 사람이 같은 명령을 쳤을 때는
    # 실패한다 — **서비스는 되는데 사람은 안 되는** 가장 헷갈리는 모양이다.
    load_env_file()
    # 설정을 읽은 **뒤에** 기본값을 정한다. 먼저 정하면 `.env` 의 ARCHIVE_DIR 이
    # 무시되고 현재 디렉터리 밑을 본다.
    archive = args.archive or os.getenv("ARCHIVE_DIR", "./archive")

    try:
        if args.backfill and args.nightly:
            ap.error("--backfill 과 --nightly 는 함께 사용할 수 없습니다")
        if args.status:
            return show_status()
        if args.backfill:
            return backfill(archive, apply=args.apply)
        if args.nightly:
            result = nightly_backfill(archive, apply=args.apply)
            if result != EXIT_OK or not args.apply:
                return result

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
        meta_path = staging_meta(archive, job)
        if not meta_path.is_file():
            # 좌표는 있는데 파일이 없다. **되풀이하지 않는다** — 같은 결과가 나온다.
            queue.fail(job.id, error_code="staging_missing", retryable=False)
            print(f"  건너뜀(메타데이터 없음): {job.log_line()}")
            failed += 1
            continue
        ok, code, retryable = reconvert(meta_path, expected_sha256=job.original_sha256)
        if ok:
            try:
                ok, code, retryable = publish_reconversion(archive, meta_path, job)
            except Exception as exc:  # noqa: BLE001 - 한 작업이 큐 전체를 멈추면 안 된다
                print(
                    f"  원문 반영 중 예외({type(exc).__name__}): {job.log_line()}",
                    file=sys.stderr,
                )
                ok, code, retryable = False, "publish_failed", True
            if ok:
                queue.succeed(job.id)
                print(f"  변환·원문 반영·색인 성공: {job.log_line()}")
                continue
        state = queue.fail(job.id, error_code=code, retryable=retryable)
        failed += 1
        print(
            f"  실패({code}) -> {state}: job={job.id} ws={job.workspace} "
            f"ch={job.channel_id} file={job.file_id} attempt={job.attempt_count}"
        )

    print(f"\n처리 {len(jobs)}건 · 실패 {failed}건")
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
