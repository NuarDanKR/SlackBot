#!/usr/bin/env python3
"""첨부가 답변에 쓰이고 있는지 진단한다.

    python scripts/diagnose_attachments.py

## 왜 필요한가

「봇이 첨부파일을 못 읽는다」 는 피드백에는 **서로 다른 원인 넷**이 섞여 있다.
겉으로는 다 같아 보이고, 넷 다 오류를 내지 않는다.

| 원인 | 무슨 일이 나는가 | 사람이 할 일 |
|---|---|---|
| 변환 대상이 아니다 | 이미지·스캔·구형 hwp 는 애초에 텍스트가 안 나온다 | 원본 검수 승인 |
| 변환됐지만 잘렸다 | 400줄을 넘으면 뒤가 없다. 표는 뒤에 합계가 있다 | 상한 조정 판단 |
| 원본이 검수 대기다 | 승인 전에는 모델에 안 간다(설계) | 검수 승인 |
| 변환이 실패했다 | 라이브러리 없음·파일 손상 | 설치·원본 확인 |

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
from tybot.attachment_review import APPROVED, PENDING, REJECTED, scan
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
        print(f"=== 변환본이 잘린 문서 {len(truncated)}건 (상한 {MAX_LINES}줄)")
        print("  표는 뒤에 합계가 있다. 잘리면 그 값을 답변이 볼 수 없다.")
        for name in sorted(set(truncated))[:10]:
            print(f"  - {name}")

    print()
    print("=== 검수 상태")
    pending = scan(root, status=PENDING)
    approved = scan(root, status=APPROVED)
    rejected = scan(root, status=REJECTED)
    print(f"  대기 {len(pending)} · 승인 {len(approved)} · 반려 {len(rejected)}")

    if pending:
        print()
        print("=== 승인해야 원본을 읽는 것")
        print("  변환이 안 되는 형식은 **승인 없이는 내용을 알 방법이 없다.**")
        for item in pending[:30]:
            kind = "변환됨" if item.name in extracted else "변환 안 됨"
            able = "변환가능" if can_convert(_suffix(item.name)) else "변환대상아님"
            mark = "  ← 승인 필요" if kind == "변환 안 됨" else ""
            print(f"  - {item.name} [{able}/{kind}]{mark}")
        if len(pending) > 30:
            print(f"  … 외 {len(pending) - 30}건")

    print()
    print("=== 판정")
    blind = [
        item.name
        for item in pending
        if item.name not in extracted and not can_convert(_suffix(item.name))
    ]
    if blind:
        print(f"  승인 없이는 내용을 알 수 없는 파일 {len(blind)}건.")
        print("  이미지·스캔 PDF·구형 hwp 는 변환하지 않는다(OCR 오류가 사실처럼 굳는다).")
        print("  → 콘솔 첨부 검수에서 승인하면 원본이 모델에 전달된다.")
        print("  → 표·스캔을 읽으려면 상위 모델이 필요하다(PDF 페이지 상한도 커진다).")
    else:
        print("  변환으로 내용이 확보되지 않은 대기 파일은 없다.")
    return 0


def _suffix(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


if __name__ == "__main__":
    raise SystemExit(main())
