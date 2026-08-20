#!/usr/bin/env python3
"""아카이브 문서의 공개 여부를 바꾼다 - 크로스 워크스페이스 공유의 관문 ②.

    python scripts/share.py --list                          # 현재 공개 상태 일람
    python scripts/share.py <파일...> --public              # 공유 켜기
    python scripts/share.py <파일...> --private             # 공유 끄기
    python scripts/share.py <파일...> --public --dry-run    # 바뀔 내용만 확인

원문 블록은 절대 건드리지 않는다. 프론트매터의 `visibility` 한 줄만 바꾸고,
변경 전후로 스키마와 원문 라인 수를 검증한다. 하나라도 어긋나면 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tybot.archive.store import SchemaError, load_doc, validate  # noqa: E402

VISIBILITY_RE = re.compile(r"^visibility:\s*\S+\s*$", re.MULTILINE)


def archive_root() -> Path:
    return Path(os.getenv("ARCHIVE_DIR", "./archive"))


def set_visibility(path: Path, value: str, *, dry_run: bool = False) -> str:
    """프론트매터의 visibility 만 교체한다. 반환값은 사람이 읽는 결과 문자열."""
    text = path.read_text(encoding="utf-8")
    before = load_doc(path)  # 스키마 검증 + 원문 라인 수 확보

    if before.visibility == value:
        return f"[변경없음] {path.name}: 이미 {value}"

    if not VISIBILITY_RE.search(text):
        raise SchemaError(f"{path}: visibility 줄을 찾지 못했습니다")
    new_text = VISIBILITY_RE.sub(f"visibility: {value}", text, count=1)

    # 원문이 한 글자라도 바뀌면 중단한다.
    if new_text.split("## 원문", 1)[-1] != text.split("## 원문", 1)[-1]:
        raise SchemaError(f"{path}: 원문 블록이 변경되려 합니다 - 중단")
    validate(new_text, path=str(path))

    if dry_run:
        return f"[예정] {path.name}: {before.visibility} -> {value}"

    path.write_text(new_text, encoding="utf-8")
    after = load_doc(path)
    if len(after.raw_lines) != len(before.raw_lines):
        path.write_text(text, encoding="utf-8")  # 되돌린다
        raise SchemaError(f"{path}: 원문 라인 수가 변했습니다({len(before.raw_lines)} -> "
                          f"{len(after.raw_lines)}) - 되돌렸습니다")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"  변경기록 {stamp} {path} {before.visibility} -> {value}", file=sys.stderr)
    return f"[변경] {path.name}: {before.visibility} -> {value}"


def list_docs() -> int:
    root = archive_root() / "channels"
    if not root.is_dir():
        print(f"아카이브가 없습니다: {root}")
        return 1
    rows: list[tuple[str, str, str, int]] = []
    for p in sorted(root.rglob("*.md")):
        try:
            d = load_doc(p)
            rows.append((d.workspace, d.visibility, d.channel, len(d.raw_lines)))
        except SchemaError as e:
            rows.append(("?", "스키마오류", f"{p.name} ({e})", 0))
    if not rows:
        print("문서가 없습니다.")
        return 0
    print(f"{'워크스페이스':<14} {'공개':<10} {'원문':>6}  채널")
    for ws, vis, ch, n in rows:
        mark = "PUBLIC" if vis == "public" else vis
        print(f"{ws:<14} {mark:<10} {n:>6}  {ch}")
    pub = sum(1 for r in rows if r[1] == "public")
    print(f"\n총 {len(rows)}건 중 공개 {pub}건 - 공개 문서만 다른 워크스페이스에서 조회됩니다.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="아카이브 문서 공개 여부 변경")
    ap.add_argument("paths", nargs="*", help="대상 MD 파일 경로")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_true", help="다른 워크스페이스에서 조회 가능하게")
    g.add_argument("--private", action="store_true", help="자기 워크스페이스로 제한")
    ap.add_argument("--list", action="store_true", help="현재 공개 상태 일람")
    ap.add_argument("--dry-run", action="store_true", help="바뀔 내용만 출력")
    a = ap.parse_args()

    if a.list or not a.paths:
        return list_docs()
    if not (a.public or a.private):
        ap.error("--public 또는 --private 를 지정하세요")

    value = "public" if a.public else "private"
    rc = 0
    for raw in a.paths:
        p = Path(raw)
        if not p.is_file():
            print(f"[오류] 파일 없음: {p}")
            rc = 1
            continue
        try:
            print(set_visibility(p, value, dry_run=a.dry_run))
        except SchemaError as e:
            print(f"[오류] {e}")
            rc = 1
    if value == "public" and rc == 0 and not a.dry_run:
        print("\n공개로 바꿨습니다. CROSS_WS_READ 에 등록된 워크스페이스에서 조회됩니다.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
