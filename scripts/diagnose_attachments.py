#!/usr/bin/env python3
"""첨부가 답변에 쓰이고 있는지 진단한다.

    python scripts/diagnose_attachments.py

## 왜 필요한가

「봇이 첨부파일을 못 읽는다」 는 피드백에는 **서로 다른 원인 넷**이 섞여 있다.
겉으로는 다 같아 보이고, 넷 다 오류를 내지 않는다.

| 원인 | 무슨 일이 나는가 | 사람이 할 일 |
|---|---|---|
| 변환 대상이 아니다 | 이미지·도면 등은 애초에 텍스트가 안 나온다 | 원본 확인·변환기 검토 |
| 변환됐지만 잘렸다 | 상한을 넘으면 가운데를 접는다. 그전에는 뒤를 잘랐다 | 재수집 판단 |
| 수집 당시 상한이 낮았다 | 상한을 올려도 이미 쌓인 문서는 그대로다 | 그 채널 재수집 |
| 변환이 실패했다 | 라이브러리 없음·파일 손상 | 설치·원본 확인 |

첨부는 명령이나 승인을 기다리지 않고 수집 시 자동 변환된다. 변환된 텍스트만 답변
근거가 되며, 실패하거나 지원하지 않는 원본은 외부 모델에 전송하지 않는다. 처리
결과는 채널 검토자와 담당자에게 정해진 시각에 DM으로 간다.

이 스크립트는 **판정만 한다.** 아무것도 승인하지 않고 아무 파일도 바꾸지 않는다.

## 파일 이름을 그대로 찍는다

파일명에는 사업장·문서 종류가 들어 있다. 이건 운영자가 서버에서 보는 진단이라
그대로 보이는 것이 맞다 — 어떤 변환 환경을 조치해야 하는지 알아야 하기 때문이다.
**본문은 찍지 않는다.**
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.archive.convert import MAX_LINES
from tybot.archive.store import ArchiveStore
from tybot.attachment_review import (
    CONVERTED,
    UNSUPPORTED,
    scan,
)
from tybot.envfile import load_env_file
from tybot.paths import archive_dir

# `[첨부:상태] 이름 (형식, 크기)` / `[첨부추출:이름]` / `[첨부본문:이름]`
STAGED_RE = re.compile(r"^\[첨부:(?P<state>[^\]]*)\]\s*(?P<name>.+?)\s*\(")
EXTRACTED_RE = re.compile(r"^\[첨부(?:본문|추출):(?P<name>[^\]]+)\]")
TRUNCATED = "이하 생략"


def main() -> int:
    load_env_file()
    root = Path(archive_dir())
    if not root.is_dir():
        print(f"아카이브 경로가 없습니다: {root}")
        return 1

    store = ArchiveStore(root)
    docs = store.docs()

    extracted: set[str] = set()
    staged: Counter[str] = Counter()
    staged_names: dict[str, list[str]] = {}
    truncated: list[str] = []

    for doc in docs:
        for line in doc.raw_lines:
            text = (line.text or "").strip()
            got = EXTRACTED_RE.match(text)
            if got:
                extracted.add(got.group("name"))
                continue
            got = STAGED_RE.match(text)
            if got:
                state = got.group("state")
                staged[state] += 1
                staged_names.setdefault(state, []).append(got.group("name"))
        # 잘린 변환본은 원문 줄에 그대로 남는다.
        truncated += [
            str(line.source_path.name if line.source_path else doc.path.name)
            for line in doc.raw_lines
            if TRUNCATED in (line.text or "")
        ]

    print(f"아카이브: {root}")
    print(f"문서 {len(docs)}건")
    print()
    print("=== 원문에 남은 첨부 표시")
    if not staged and not extracted:
        print("  (없음) — 첨부가 수집된 적이 없습니다")
    for state, count in sorted(staged.items()):
        print(f"  [첨부:{state}] {count}건")
    print(f"  변환본이 들어간 파일: {len(extracted)}건")

    if truncated:
        print()
        print(f"=== 변환본이 잘린 문서 {len(truncated)}건")
        print("  표는 뒤에 합계가 있다. 잘리면 그 값을 답변이 볼 수 없다.")
        print(f"  지금 상한은 {MAX_LINES:,}줄이다. 그런데 이 표시는 **수집 당시**")
        print("  상한으로 잘린 흔적이라, 상한을 올려도 이미 쌓인 문서는 그대로다 —")
        print("  그 채널을 다시 수집해야 새 상한이 적용된다.")
        for name in sorted(set(truncated))[:10]:
            print(f"  - {name}")

    print()
    print("=== 자동 변환 상태")
    items = scan(root)
    converted = [item for item in items if item.status == CONVERTED or item.extracted]
    failed = [item for item in items if item.conversion_failed]
    unsupported = [item for item in items if item.status == UNSUPPORTED]
    historical = [
        item for item in items
        if item not in converted and item not in failed and item not in unsupported
    ]
    print(
        f"  완료 {len(converted)} · 실패 {len(failed) - len(unsupported)} · "
        f"미지원 {len(unsupported)} · 과거 상태 {len(historical)}"
    )

    if failed:
        print()
        print("=== 조치가 필요한 첨부")
        print("  변환하지 못한 원본은 답변 모델에 전송되지 않는다.")
        for item in failed[:30]:
            kind = "미지원" if item.status == UNSUPPORTED else "변환실패"
            print(f"  - {item.name} [{kind}] {item.workspace}/{item.channel_id}")
        if len(failed) > 30:
            print(f"  … 외 {len(failed) - 30}건")

    print()
    print("=== 판정")
    if converted:
        print(f"  자동 변환되어 답변에 쓰이는 파일 {len(converted)}건.")
    if failed:
        print(f"  변환 환경 또는 원본 확인이 필요한 파일 {len(failed)}건.")
        print("  채널 검토자와 담당자에게 정해진 시각에 DM으로 알린다.")
        print("  콘솔의 수집 > 아카이브 진단에서도 확인할 수 있다.")
    if not failed:
        print("  자동 변환 조치가 필요한 파일은 없다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
