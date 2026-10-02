"""변환이 끝난 뒤 수집 ACK 를 **다시 센다.**

결정: 2026-10-02 오너 지시(첨부 ACK 는 변환 완료 후 갱신한다).

## 왜 필요했나

`ingest_ack.advance` 를 부르는 자리는 수집 순간 하나뿐이었다. 그런데 변환은 큐를
지나므로 그 순간에는 아직 `pending` 이다. 그래서 **첨부가 있는 메시지는 전부
`partial` 로 시작해 영원히 `partial`** 이었다 — 변환이 성공해도 그대로였다.

`partial → ready` 간선은 처음부터 열려 있었다(`archiving_state._INGEST_EDGES`).
그 길로 가는 코드가 없었을 뿐이다.

## 무엇을 고치고 무엇을 안 고치나

고치는 것은 **변환 사실**뿐이다. 정본을 다시 읽어 지금 상태로 결말을 다시 센다.

원본 저장 실패(`attachment-original-missing`)는 **건드리지 않는다.** 변환이
끝났다고 없던 원본이 생기지 않는다. 여기서 덮으면 사람이 다시 올려야 한다는
사실이 조용히 지워지고, 그건 되돌릴 수 없는 쪽이다.

끝난 상태(`ready`·`refused`·`failed`)도 안 본다 — 앞으로만 간다.

## 실패하면 멈춘다

상태를 못 읽으면 **「갱신할 것이 없다」 로 보고하지 않는다**(`AckUnavailable`).
빈 결과를 성공으로 읽으면, 장애 중에 「전부 최신입니다」 가 쌓인다.

## 쓰는 법

```bash
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.archive.ack_reconcile --dry-run
```

콘솔도 같은 함수(`run`)를 부른다. CLI 는 자동화·복구용으로 남긴다.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from . import attachment_ack, attachment_reader, ingest_ack
from .archiving_state import (
    ATTACHMENT_LOST_CODES,
    IngestState,
)
from .attachment_doc import _safe

log = logging.getLogger("tybot.archive.ack_reconcile")


@dataclass(frozen=True)
class Change:
    """한 메시지의 갱신 한 건. **적용 전에 보여 줄 수 있는 모양**이다."""

    channel_id: str
    message_ts: str
    before: str
    after: str
    outcome: attachment_ack.Outcome
    #: 어디에 쓰인 자료인가. 다시 밀 때 **이 값 그대로** 쓴다 — 비우면
    #: `written_to` 가 지워져 그림자 기록이 운영으로 보인다.
    written_to: str = ""

    @property
    def line(self) -> str:
        return (
            f"{self.channel_id} {self.message_ts}: {self.before} -> {self.after} "
            f"({self.outcome.ready}/{self.outcome.total}"
            f"{' · ' + self.outcome.error_code if self.outcome.error_code else ''})"
        )


def docs_by_message(root: Path | str) -> dict[tuple[str, str, str], list]:
    """`(workspace, channel_id, message_ts)` → 그 메시지의 **현재** 정본들.

    `load_checked` 를 쓴다. 검증을 통과하지 못한 정본은 reader 가 근거로 안
    쓰므로 검색되지도 않는다 — 그걸 「됐다」 로 세면 ACK 가 검색 결과보다 앞서
    간다. 지금까지 이 파일이 막으려는 거짓말은 전부 그 모양이었다.
    """
    found: dict[tuple[str, str, str], list] = {}
    loaded = [
        doc
        for path in attachment_reader.source_files(root)
        if (doc := attachment_reader.load_checked(path, root)) is not None
    ]
    for doc in attachment_reader.current_by_file(loaded):
        if not doc.message_ts:
            # 좌표가 없으면 어느 메시지의 첨부인지 모른다. 추측해서 붙이면
            # 남의 메시지가 `ready` 가 된다.
            continue
        found.setdefault((doc.workspace, doc.channel_id, doc.message_ts), []).append(doc)
    return found


def original_retained(root: Path | str, doc) -> bool | None:
    """첨부 **원본 바이트**가 보관돼 있나. 모르면 `None`.

    **정본 MD 가 있다고 원본이 있는 것이 아니다.** 원본은 내려받자마자 쓰고
    (`files.stage_attachments`), 그 쓰기가 실패해도 digest 는 이미 계산돼 있어서
    `sha256` 칸이 찬 정본이 나온다. 그러니 정본만 보고 보관을 단정하면,
    **없는 원본을 있다고 세고** 그 메시지를 `ready` 로 올리게 된다.

    `ready` 는 「전부 검색된다」 이고, 사람은 그걸 「내 파일이 안전하다」 로
    읽는다. 그 상태에서 Slack 원본을 지우면 되살릴 자료가 어디에도 없다.

    보는 것은 staging metadata 의 `original_state` 다 — 원본을 쓴 그 자리에서
    같이 적은 값이다. 못 읽으면 objects 아래 파일이 있는지로 내려가고, 그것도
    못 보면 **`None`**(모른다)이다. `False` 로 만들지 않는다 — 「없다」 와
    「모른다」 는 사람이 할 일이 다르다.
    """
    storage = _storage_for(root, doc)
    if storage is None:
        return None
    file_id = _safe(str(getattr(doc, "file_id", "") or ""))
    meta_path = storage.staging_dir / file_id / "metadata.json"
    if meta_path.is_file():
        return attachment_ack.retained_from_metadata(meta_path)
    objects = storage.objects_dir / file_id
    if not objects.is_dir():
        # staging 도 objects 도 없다. 오래된 자료일 수도, 지워졌을 수도 있다 —
        # 구분할 근거가 없으므로 모른다고 한다.
        return None
    return any(c.is_file() and c.stat().st_size > 0 for c in objects.iterdir())


def _storage_for(root: Path | str, doc):
    """그 정본의 원본이 있어야 할 자리. 경로를 못 되짚으면 `None`.

    규칙을 여기서 다시 쓰지 않고 `files.attachment_storage` 를 부른다 — 두 벌이
    되면 한쪽만 고치는 날 원본을 엉뚱한 곳에서 찾고, 못 찾은 것을 「없다」 로
    읽는다.
    """
    from .files import attachment_storage

    base = Path(root)
    source = getattr(doc, "source_path", None)
    if source is None:
        return None
    try:
        path = Path(source).resolve()
        # 옛 배치: <root>/workspaces/<ws>/channels/<id>/attachments/<file>/<rev>.md
        # 새 배치: <root>/<ws>/<id>__<이름>/attachments/<file>/<rev>.md
        legacy = path.is_relative_to((base / "workspaces").resolve())
        channel_root = None if legacy else path.parents[2]
        return attachment_storage(
            base, doc.workspace, doc.channel_id, channel_root=channel_root
        )
    except (OSError, ValueError, IndexError):
        log.warning("첨부 저장 위치를 되짚지 못했다: %s", source)
        return None


def plan(
    rows, docs: dict[tuple[str, str, str], list], *, root: Path | str
) -> list[Change]:
    """무엇이 바뀌어야 하나. **아무것도 쓰지 않는다.**"""
    changes: list[Change] = []
    for row in rows:
        workspace = str(row["workspace"])
        channel_id = str(row["channel_id"])
        message_ts = str(row["message_ts"])
        total = int(row["attachment_total"] or 0)
        if total <= 0:
            # 첨부가 없는 메시지는 변환을 기다리지 않는다. 여기서 손대면 본문
            # 경로가 정한 상태를 첨부 규칙으로 덮는다.
            continue
        if str(row["error_code"] or "") in ATTACHMENT_LOST_CODES:
            continue

        current = docs.get((workspace, channel_id, message_ts), [])
        outcome = _recount(current, total, root=root)
        after = IngestState.READY if outcome.all_ready else IngestState.PARTIAL
        before = IngestState(str(row["state"]))
        if (
            after == before
            and outcome.ready == int(row["attachment_ready"] or 0)
            and outcome.error_code == str(row["error_code"] or "")
        ):
            continue
        changes.append(
            Change(
                channel_id, message_ts, str(before), str(after), outcome,
                written_to=str(row["written_to"] or ""),
            )
        )
    return changes


def _recount(current: list, total: int, *, root: Path | str) -> attachment_ack.Outcome:
    """정본 상태로 결말을 다시 센다. **모르는 첨부는 「아직」 이다.**

    표가 말하는 첨부 수보다 정본이 적을 수 있다 — 정본 쓰기가 실패했거나 아직
    안 썼거나 검증에서 거절됐다. 그 차이를 메우지 않고 `ready` 를 내면, 문서가
    없는 첨부를 검색 가능하다고 말하게 된다. 그래서 **모자란 수만큼 자리를
    채워** 아직으로 센다.

    원본 보관은 **확인한다**(`original_retained`). 정본이 있다는 이유로 보관됐다고
    세면 없는 원본을 있다고 하는 것이고, 그 메시지는 `ready` 가 된다.
    """
    kept = list(current)[:total]
    file_ids = [doc.file_id for doc in kept]
    # 모르면 **보관됐다고 하지 않는다.** 그래야 `ready` 로 안 올라간다.
    stored = {doc.file_id: original_retained(root, doc) is True for doc in kept}
    conversion = {doc.file_id: doc.conversion_state for doc in kept}
    has_text = {doc.file_id: bool(doc.text.strip()) for doc in kept}
    for index in range(total - len(file_ids)):
        placeholder = f"\x00unknown-{index}"
        file_ids.append(placeholder)
        # 정본조차 없는 자리다. 원본을 물을 대상이 아니라 **변환이 아직**인
        # 것으로 센다(`conversion` 에 없으면 pending).
        stored[placeholder] = True
    return attachment_ack.classify(
        file_ids, stored=stored, conversion=conversion, has_text=has_text
    )


def roots_from_env() -> dict[str, Path]:
    """`written_to` 값마다 그 자료가 있는 루트.

    두 루트는 **다른 곳**이다. 그림자 수집은 `ARCHIVER_SHADOW_DIR` 에, 운영은
    `ARCHIVE_DIR` 에 쌓인다. 한 루트로만 다시 세면 다른 쪽 행은 정본을 못 찾고,
    못 찾은 것이 「아직 변환 중」 으로 읽혀 **끝난 일이 영원히 아직**이 된다.
    반대 방향은 더 나쁘다 — 엉뚱한 루트에서 같은 file ID 를 찾으면 남의 자료로
    남의 메시지를 `ready` 로 올린다.
    """
    import os

    found: dict[str, Path] = {}
    live = os.getenv("ARCHIVE_DIR", "").strip()
    shadow = os.getenv("ARCHIVER_SHADOW_DIR", "").strip()
    if live:
        found[ingest_ack.LIVE] = Path(live)
    if shadow:
        found[ingest_ack.SHADOW] = Path(shadow)
    return found


def label_for(roots: dict[str, Path], path: Path | str) -> str:
    """이 경로가 어느 쪽 루트인가. 모르면 빈 문자열.

    짐작하지 않는다. 짐작해서 틀리면 그림자 자료로 운영 ACK 를 올린다.
    """
    try:
        target = Path(path).resolve()
    except OSError:  # pragma: no cover - 경로가 이상하면 모른다고 한다
        return ""
    for label, candidate in roots.items():
        try:
            if Path(candidate).resolve() == target:
                return label
        except OSError:  # pragma: no cover
            continue
    return ""


def run(
    workspace: str,
    *,
    roots: dict[str, Path],
    apply: bool = False,
    limit: int = 500,
    max_rows: int = 20_000,
) -> list[Change]:
    """미완료 ACK 를 다시 세고, `apply` 면 민다.

    기본이 **안 쓰는 쪽**이다. 파괴적이지는 않지만 사람에게 하는 말을 바꾸므로,
    무엇이 바뀌는지 먼저 보여 준다(운영 기능은 범위를 명시하게 만든다).

    **끝까지 읽는다.** 한 번에 `limit` 건씩 이어 읽는 이유는, 바뀌지 않는 행은
    `updated_at` 도 안 바뀌어 **앞자리를 영원히 차지하기** 때문이다. 한 쪽만
    읽으면 그 뒤의 행은 변환이 끝나도 갱신되지 않고, 잡은 매번 정상 종료한다.
    """
    cached: dict[str, dict] = {}
    changes: list[Change] = []
    after: tuple | None = None
    seen = 0
    while seen < max_rows:
        rows = ingest_ack.unfinished(workspace, limit=limit, after=after)
        if not rows:
            break
        seen += len(rows)
        last = rows[-1]
        after = (last["updated_at"], last["channel_id"], last["message_ts"])
        for label, group in _by_destination(rows).items():
            root = roots.get(label)
            if root is None:
                # 어느 루트인지 모른다. **짐작해서 다시 세지 않는다.**
                log.info("루트를 모르는 행 %d 건을 건너뛴다 written_to=%r", len(group), label)
                continue
            if label not in cached:
                cached[label] = docs_by_message(root)
            changes.extend(plan(group, cached[label], root=root))
        if len(rows) < limit:
            break
    if apply:
        for change in changes:
            _push(workspace, change)
    return changes


def _by_destination(rows) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row["written_to"] or ""), []).append(row)
    return grouped


def _push(workspace: str, change: Change) -> None:
    ingest_ack.advance(
        workspace=workspace,
        channel_id=change.channel_id,
        message_ts=change.message_ts,
        target=IngestState(change.after),
        attachment_total=change.outcome.total,
        attachment_ready=change.outcome.ready,
        written_to=change.written_to,
        error_code=change.outcome.error_code,
    )


def refresh_one(
    workspace: str,
    channel_id: str,
    message_ts: str,
    *,
    roots: dict[str, Path],
    apply: bool = True,
) -> Change | None:
    """메시지 하나를 다시 센다. **변환이 끝난 직후 부르는 자리.**

    루트는 그 행의 `written_to` 가 고른다 — 부른 쪽이 들고 있는 경로가 아니라.
    변환 잡은 자기가 돌린 아카이브만 알고, 그 아카이브가 그 행의 목적지라는
    보장은 없다.
    """
    if not message_ts:
        return None
    status = ingest_ack.read(workspace, channel_id, message_ts)
    if status is None or status.state not in (
        IngestState.ATTACHMENT_PENDING, IngestState.PARTIAL, IngestState.RAW_WRITTEN,
    ):
        return None
    root = roots.get(status.written_to)
    if root is None:
        log.info(
            "루트를 몰라 ACK 를 다시 세지 않는다 ws=%s ts=%s written_to=%r",
            workspace, message_ts, status.written_to,
        )
        return None
    row = {
        "workspace": workspace, "channel_id": channel_id, "message_ts": message_ts,
        "state": str(status.state), "attachment_total": status.progress.attachment_total,
        "attachment_ready": status.progress.attachment_ready,
        "written_to": status.written_to, "error_code": status.progress.error_code,
    }
    changes = plan([row], docs_by_message(root), root=root)
    if not changes:
        return None
    if apply:
        _push(workspace, changes[0])
    return changes[0]


def main(argv: list[str] | None = None) -> int:
    from ..envfile import load_env_file

    parser = argparse.ArgumentParser(description="변환 완료 뒤 수집 ACK 를 다시 센다")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply", action="store_true", help="실제로 민다(기본은 보여 주기만)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env_file()
    # 루트를 손으로 받지 않는다. `written_to` 가 고르는 값이라 하나만 받으면
    # 그림자 행을 운영 루트에서 찾거나 그 반대를 하게 된다.
    roots = roots_from_env()
    if not roots:
        print("ARCHIVE_DIR 도 ARCHIVER_SHADOW_DIR 도 없습니다.")
        return 2
    try:
        changes = run(args.workspace, roots=roots, apply=args.apply, limit=args.limit)
    except ingest_ack.AckUnavailable as exc:
        print(f"수집 상태를 읽지 못했습니다: {exc}")
        return 2
    if not changes:
        print("갱신할 것이 없습니다.")
        return 0
    for change in changes:
        print(("적용 " if args.apply else "예정 ") + change.line)
    print(f"{'적용' if args.apply else '예정'} {len(changes)} 건")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
