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

log = logging.getLogger("tybot.archive.ack_reconcile")


@dataclass(frozen=True)
class Change:
    """한 메시지의 갱신 한 건. **적용 전에 보여 줄 수 있는 모양**이다."""

    channel_id: str
    message_ts: str
    before: str
    after: str
    outcome: attachment_ack.Outcome

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


def plan(rows, docs: dict[tuple[str, str, str], list]) -> list[Change]:
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
        outcome = _recount(current, total)
        after = IngestState.READY if outcome.all_ready else IngestState.PARTIAL
        before = IngestState(str(row["state"]))
        if (
            after == before
            and outcome.ready == int(row["attachment_ready"] or 0)
            and outcome.error_code == str(row["error_code"] or "")
        ):
            continue
        changes.append(
            Change(channel_id, message_ts, str(before), str(after), outcome)
        )
    return changes


def _recount(current: list, total: int) -> attachment_ack.Outcome:
    """정본 상태로 결말을 다시 센다. **모르는 첨부는 「아직」 이다.**

    표가 말하는 첨부 수보다 정본이 적을 수 있다 — 정본 쓰기가 실패했거나 아직
    안 썼거나 검증에서 거절됐다. 그 차이를 메우지 않고 `ready` 를 내면, 문서가
    없는 첨부를 검색 가능하다고 말하게 된다. 그래서 **모자란 수만큼 자리를
    채워** 아직으로 센다.
    """
    file_ids = [doc.file_id for doc in current][:total]
    stored = dict.fromkeys(file_ids, True)
    conversion = {doc.file_id: doc.conversion_state for doc in current}
    has_text = {doc.file_id: bool(doc.text.strip()) for doc in current}
    for index in range(total - len(file_ids)):
        placeholder = f"\x00unknown-{index}"
        file_ids.append(placeholder)
        stored[placeholder] = True
    return attachment_ack.classify(
        file_ids, stored=stored, conversion=conversion, has_text=has_text
    )


def run(
    root: Path | str, workspace: str, *, apply: bool = False, limit: int = 500
) -> list[Change]:
    """미완료 ACK 를 다시 세고, `apply` 면 민다.

    기본이 **안 쓰는 쪽**이다. 파괴적이지는 않지만 사람에게 하는 말을 바꾸므로,
    무엇이 바뀌는지 먼저 보여 준다(운영 기능은 범위를 명시하게 만든다).
    """
    rows = ingest_ack.unfinished(workspace, limit=limit)
    if not rows:
        return []
    changes = plan(rows, docs_by_message(root))
    if not apply:
        return changes
    for change in changes:
        ingest_ack.advance(
            workspace=workspace,
            channel_id=change.channel_id,
            message_ts=change.message_ts,
            target=IngestState(change.after),
            attachment_total=change.outcome.total,
            attachment_ready=change.outcome.ready,
            error_code=change.outcome.error_code,
        )
    return changes


def main(argv: list[str] | None = None) -> int:
    from ..envfile import load_env_file
    from ..paths import archive_dir

    parser = argparse.ArgumentParser(description="변환 완료 뒤 수집 ACK 를 다시 센다")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply", action="store_true", help="실제로 민다(기본은 보여 주기만)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env_file()
    root = Path(args.archive or archive_dir())
    try:
        changes = run(root, args.workspace, apply=args.apply, limit=args.limit)
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
