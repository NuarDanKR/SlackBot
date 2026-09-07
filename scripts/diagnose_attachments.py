#!/usr/bin/env python3
"""첨부가 답변에 쓰이고 있는지 진단한다.

    python scripts/diagnose_attachments.py

## 왜 필요한가

「봇이 첨부파일을 못 읽는다」 는 피드백에는 **서로 다른 원인 넷**이 섞여 있다.
겉으로는 다 같아 보이고, 넷 다 오류를 내지 않는다.

| 원인 | 무슨 일이 나는가 | 사람이 할 일 |
|---|---|---|
| 변환 대상이 아니다 | 이미지·스캔·구형 hwp 는 애초에 텍스트가 안 나온다 | 검토자 확인 |
| 변환됐지만 잘렸다 | 상한을 넘으면 가운데를 접는다. 그전에는 뒤를 잘랐다 | 재수집 판단 |
| 수집 당시 상한이 낮았다 | 상한을 올려도 이미 쌓인 문서는 그대로다 | 그 채널 재수집 |
| 변환이 실패했다 | 라이브러리 없음·파일 손상 | 설치·원본 확인 |

검수 대기는 더 이상 원인이 아니다. 변환된 파일은 사람을 기다리지 않는다
(2026-09-08). 남는 것은 **글자가 없어 PII 검사가 돌지 않은 파일**뿐이고, 그건
채널 검토자에게 정해진 시각에 DM 으로 간다.

이 스크립트는 **판정만 한다.** 아무것도 승인하지 않고 아무 파일도 바꾸지 않는다.

## 파일 이름을 그대로 찍는다

파일명에는 사업장·문서 종류가 들어 있다. 이건 운영자가 서버에서 보는 진단이라
그대로 보이는 것이 맞다 — 무엇을 승인해야 하는지 알아야 하기 때문이다.
**본문은 찍지 않는다.**
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.archive.convert import MAX_LINES, can_convert
from tybot.archive.store import ArchiveStore
from tybot.attachment_review import (
    APPROVED,
    PENDING,
    REJECTED,
    find_sendable,
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
    print("=== 검수 상태")
    pending = scan(root, status=PENDING)
    approved = scan(root, status=APPROVED)
    rejected = scan(root, status=REJECTED)
    print(f"  대기 {len(pending)} · 승인 {len(approved)} · 반려 {len(rejected)}")

    # **답변 경로와 같은 판정을 쓴다.** 여기서 따로 세면 진단이 "승인 필요 15건" 이라
    # 말하는데 봇은 이미 그 파일들을 쓰고 있는, 서로 어긋난 두 사실이 생긴다.
    waiting = [
        item
        for item in pending
        if not find_sendable(
            root,
            workspace=item.workspace,
            channel_id=item.channel_id,
            name=item.name,
            text_extracted=item.name in extracted,
        )
    ]

    if waiting:
        print()
        print("=== 사람이 봐야 원본을 읽는 것")
        print("  글자가 없어 수집 단계 PII 검사가 **아예 돌지 않은** 파일이다.")
        for item in waiting[:30]:
            able = "변환가능" if can_convert(_suffix(item.name)) else "변환대상아님"
            print(f"  - {item.name} [{able}] {item.workspace}/{item.channel_id}")
        if len(waiting) > 30:
            print(f"  … 외 {len(waiting) - 30}건")

    print()
    print("=== 판정")
    used = len(pending) - len(waiting)
    if used:
        print(f"  대기 상태지만 **이미 답변에 쓰이는** 파일 {used}건.")
        print("  변환본이 아카이브에 들어갔다는 것은 PII 검사를 통과했다는 뜻이다 —")
        print("  이런 파일은 사람을 기다리지 않는다(2026-09-08).")
    if waiting:
        print(f"  사람이 봐야 하는 파일 {len(waiting)}건.")
        print("  이미지·스캔 PDF·구형 hwp 는 변환하지 않는다(OCR 오류가 사실처럼 굳는다).")
        print("")
        print("  **찾아가서 승인할 필요는 없다.** 채널 검토자에게 정해진 시각에")
        print("  그날 몫이 DM 으로 간다(`tybot-review-dm.timer`). 검토자를 안 정한")
        print("  채널은 개설자에게 간다.")
        print("    - 검토자 지정: Slack 에서 `/채널 검토자`")
        print("    - 지금 보이기: 그 채널에서 `/첨부`")
        print("    - 서버에서: python -m tybot.attachment_review list")
        print("  → 표·스캔을 읽으려면 상위 모델이 필요하다(PDF 페이지 상한도 커진다).")
    if not waiting:
        print("  사람을 기다리는 파일은 없다.")
    return 0


def _suffix(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


if __name__ == "__main__":
    raise SystemExit(main())
