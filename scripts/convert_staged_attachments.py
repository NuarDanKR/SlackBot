#!/usr/bin/env python3
"""이미 올라온 첨부를 **지금 코드로 다시 변환해** 아카이브에 채운다.

    python scripts/convert_staged_attachments.py            # 판정만(기본)
    python scripts/convert_staged_attachments.py --apply    # 실제로 채운다

## 왜 필요한가 (2026-09-08, 오너 결정)

> "모든 첨부 파일은 그냥 무조건 승인 없이 변환을 먼저 하고 그 요약 내용을
> 차라리 검토를 받는게 나아. 우선 미승인난 모든 문서를 변환해"

변환은 **수집 시점에만** 돌았다. 그때 상한이 낮았거나(400줄) 라이브러리가 없었거나
승인 대기로 남은 파일은, 코드를 고친 뒤에도 아카이브에 텍스트가 없다. 답변은
정상적으로 나가고 그 파일 내용만 없다 — 오류가 없어서 아무도 모른다.

## Slack 을 다시 부르지 않는다

원본 바이트는 이미 서버에 있다(`objects/`). rate limit(분당 1요청·요청당 15건)에
묶이는 재수집과 달리, 이건 디스크만 읽는다. 46건이 몇 초다.

## 원문을 고치지 않는다 (원칙 1)

기존 줄은 **한 글자도 건드리지 않는다.** 변환본을 `[첨부추출:파일명]` 줄로
**덧붙인다.** 쓰기는 `writer.ingest` 를 그대로 쓰므로 세 가지가 공짜로 따라온다.

- PII 검사(`writer.screen`) — 주민번호·등기부등본·계약자 명단은 거절된다(원칙 5)
- 줄 단위 중복 제거 — 두 번 돌려도 두 번 쌓이지 않는다
- 날짜별 파일 배치 — 원래 그 첨부가 올라온 날 파일로 들어간다

## 하지 않는 것

**이미지·스캔은 변환하지 않는다.** 텍스트 레이어가 없어 로컬 변환으로는 한 글자도
안 나온다. 그건 모델이 원본을 봐야 하는 일이고 비용과 벤더 전송이 걸린다 —
이 스크립트는 그 목록만 세어 보여 준다.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.archive import writer
from tybot.archive.convert import ConvertError, can_convert, convert
from tybot.archive.store import ArchiveStore
from tybot.archive.writer import KST, doc_path
from tybot.attachment_review import scan
from tybot.envfile import load_env_file
from tybot.paths import archive_dir

# `> [2026-09-07 09:00] 홍길동: [첨부:검수대기] 가정산서.xlsx (xlsx, 900KB)`
STAGED_RE = re.compile(r"^\[첨부:(?P<state>[^\]]*)\]\s*(?P<name>.+?)\s*\(")
EXTRACTED_RE = re.compile(r"^\[첨부(?:본문|추출):(?P<name>[^\]]+)\]")

# 한 파일에서 아카이브로 넣는 최대 줄 수. 수집 경로와 같은 값을 쓴다 —
# 갈리면 "재변환하면 더 많이 들어간다" 는 설명할 수 없는 차이가 생긴다.
from tybot.archive.files import MAX_TEXT_LINES  # noqa: E402


def _suffix(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


def _parse_ts(raw: str) -> datetime | None:
    """`2026-09-07 09:00` → KST datetime. 못 읽으면 `None`.

    `RawLine.ts` 는 **문자열**이고 `IncomingMessage.ts` 는 datetime 이다.
    그대로 넘기면 쓰기 직전에 `astimezone` 에서 터진다.
    """
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="이미 올라온 첨부를 지금 코드로 다시 변환해 아카이브에 채운다"
    )
    ap.add_argument("--apply", action="store_true",
                    help="실제로 아카이브에 넣는다. 없으면 판정만 한다")
    ap.add_argument("--workspace", default="", help="한 워크스페이스만")
    ap.add_argument("--limit", type=int, default=0, help="처리할 최대 파일 수(0=전부)")
    args = ap.parse_args(argv)

    load_env_file()
    root = Path(archive_dir())
    if not root.is_dir():
        print(f"아카이브 경로가 없습니다: {root}")
        return 1

    store = ArchiveStore(root)
    docs = store.docs()

    # 채널별로 (이미 변환본이 있는 파일 이름, 첨부 표시가 있던 줄)
    extracted: dict[str, set[str]] = {}
    staged_at: dict[tuple[str, str], tuple[datetime, str, str, str]] = {}
    for doc in docs:
        key = doc.channel_id or doc.channel
        for line in doc.raw_lines:
            text = (line.text or "").strip()
            got = EXTRACTED_RE.match(text)
            if got:
                extracted.setdefault(key, set()).add(got.group("name"))
                continue
            got = STAGED_RE.match(text)
            if got:
                # 그 첨부가 올라온 자리(시각·화자·채널)를 기억한다. 변환본을 같은
                # 날 파일에 같은 화자로 붙이려면 필요하다.
                staged_at.setdefault(
                    (key, got.group("name")),
                    (line.ts, line.speaker, doc.channel, doc.workspace),
                )

    items = scan(root)
    if args.workspace:
        items = [i for i in items if i.workspace == args.workspace]

    todo: list[tuple] = []
    counts: Counter[str] = Counter()
    for item in items:
        if item.name in extracted.get(item.channel_id, set()):
            counts["이미 변환됨"] += 1
            continue
        if not can_convert(_suffix(item.name)):
            counts["변환 대상 아님(이미지·스캔)"] += 1
            continue
        if not item.object_path or not Path(item.object_path).is_file():
            counts["원본 없음"] += 1
            continue
        where = staged_at.get((item.channel_id, item.name))
        if where is None:
            # 첨부 표시 줄을 못 찾으면 어느 날 파일에 붙일지 알 수 없다.
            counts["아카이브에 첨부 표시가 없음"] += 1
            continue
        raw_ts, speaker, channel, workspace = where
        when = _parse_ts(raw_ts)
        if when is None:
            counts["원문 시각을 읽지 못함"] += 1
            continue
        # **대상 날짜 파일이 이미 있어야 한다.** 없으면 `writer.ingest` 가 새 문서를
        # 기본값(비공개·빈 ACL)으로 만드는데, 그건 이 채널의 실제 권한이 아니다 -
        # 권한을 짐작해서 문서를 만드는 것이 가장 위험한 실수다.
        target = doc_path(
            root, workspace or item.workspace, channel,
            channel_id=item.channel_id, day=when.date(),
        )
        if not target.is_file():
            counts["그 날 원문 파일을 찾지 못함"] += 1
            continue
        todo.append((item, (when, speaker, channel, workspace)))

    print(f"아카이브: {root}")
    print(f"검수 폴더의 첨부 {len(items)}건")
    for label, n in sorted(counts.items()):
        print(f"  {label}: {n}건")
    print(f"  변환할 것: {len(todo)}건")

    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print()
        print("=== 판정")
        print("  로컬 변환으로 채울 수 있는 파일이 없다.")
        if counts["변환 대상 아님(이미지·스캔)"]:
            print(f"  이미지·스캔 {counts['변환 대상 아님(이미지·스캔)']}건은 모델이")
            print("  원본을 봐야 한다 — 로컬 변환으로는 한 글자도 안 나온다.")
        return 0

    if not args.apply:
        print()
        print("=== 변환하면 채워질 것 (판정만 함)")
        for item, (ts, _, channel, _) in todo[:40]:
            print(f"  - {item.name} [{_suffix(item.name)}] {channel} {ts}")
        if len(todo) > 40:
            print(f"  … 외 {len(todo) - 40}건")
        print()
        print("실제로 채우려면: --apply")
        return 0

    # --- 실제로 채운다 --------------------------------------------------------
    done = 0
    failed: list[tuple[str, str]] = []
    refused_total = 0
    # 채널별로 모아 한 번에 쓴다. 파일 잠금을 파일마다 잡으면 46번 잠근다.
    batches: dict[tuple[str, str, str], list[writer.IncomingMessage]] = {}

    for item, (ts, speaker, channel, workspace) in todo:
        try:
            data = Path(item.object_path).read_bytes()
            body = convert(_suffix(item.name), data)
        except (OSError, ConvertError) as exc:
            failed.append((item.name, f"{type(exc).__name__}: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001 - 한 파일 실패가 나머지를 막지 않는다
            failed.append((item.name, f"예상치 못한 오류 {type(exc).__name__}: {exc}"))
            continue

        rows = [line.strip() for line in body if line.strip()][:MAX_TEXT_LINES]
        if not rows:
            failed.append((item.name, "변환 결과가 비어 있다"))
            continue

        key = (workspace or item.workspace, channel, item.channel_id)
        bucket = batches.setdefault(key, [])
        # 변환 사실을 먼저 한 줄. 원래의 `[첨부:검수대기]` 줄은 **그대로 둔다** —
        # 원문은 고치지 않는다. 지금 무슨 일이 있었는지는 이 줄이 말한다.
        bucket.append(writer.IncomingMessage(
            ts=ts, speaker=speaker,
            text=f"[첨부:재변환] {item.name} ({_suffix(item.name)}, "
                 f"{max(1, item.size // 1024)}KB)",
        ))
        bucket.extend(
            writer.IncomingMessage(
                ts=ts, speaker=speaker, text=f"[첨부추출:{item.name}] {row}"
            )
            for row in rows
        )
        done += 1

    for (workspace, channel, channel_id), messages in sorted(batches.items()):
        result = writer.ingest(
            root,
            workspace=workspace,
            channel=channel,
            channel_id=channel_id,
            messages=messages,
        )
        refused_total += len(result.refused)
        print(f"  {channel}: {result.written}줄 추가"
              + (f" · PII 거절 {len(result.refused)}줄" if result.refused else ""))

    print()
    print("=== 결과")
    print(f"  변환한 파일 {done}건")
    if refused_total:
        print(f"  PII 로 거절된 줄 {refused_total}줄 — 주민번호·등기부등본·계약자 명단은")
        print("  아카이브에 넣지 않는다(원칙 5). 그 파일은 사람이 직접 봐야 한다.")
    if failed:
        print(f"  변환 실패 {len(failed)}건:")
        for name, why in failed[:20]:
            print(f"    - {name}: {why}")
        if len(failed) > 20:
            print(f"    … 외 {len(failed) - 20}건")
    print()
    print("  검색 색인을 다시 만들어야 새 줄이 검색된다:")
    print("    sudo systemctl start tybot-index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
