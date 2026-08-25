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

from tybot.archive.store import SchemaError, load_doc, validate

VISIBILITY_RE = re.compile(r"^visibility:\s*\S+\s*$", re.MULTILINE)
SHARE_WITH_RE = re.compile(r"^share_with:.*$", re.MULTILINE)


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


def set_share_with(path: Path, targets: set[str], *, dry_run: bool = False) -> str:
    """`share_with` 목록을 교체한다. 프론트매터만 건드리고 원문은 검증으로 보호한다.

    이 목록이 **동등(peer) 워크스페이스로 자료가 넘어가는 유일한 경로**다.
    상위(root) 워크스페이스는 설정으로 열람하므로 여기에 넣지 않아도 된다.
    """
    text = path.read_text(encoding="utf-8")
    before = load_doc(path)
    if before.share_with == frozenset(targets):
        return f"[변경없음] {path.name}: 이미 {sorted(targets) or '없음'}"

    line = f"share_with: [{', '.join(sorted(targets))}]"
    if SHARE_WITH_RE.search(text):
        new_text = SHARE_WITH_RE.sub(line, text, count=1)
    else:
        # visibility 줄 바로 뒤에 넣는다(프론트매터 안이어야 한다).
        if not VISIBILITY_RE.search(text):
            raise SchemaError(f"{path}: visibility 줄을 찾지 못해 share_with 를 넣을 수 없습니다")
        new_text = VISIBILITY_RE.sub(lambda m: m.group(0) + "\n" + line, text, count=1)

    if new_text.split("## 원문", 1)[-1] != text.split("## 원문", 1)[-1]:
        raise SchemaError(f"{path}: 원문 블록이 변경되려 합니다 - 중단")
    validate(new_text, path=str(path))
    if dry_run:
        return f"[예정] {path.name}: share_with {sorted(before.share_with)} -> {sorted(targets)}"

    path.write_text(new_text, encoding="utf-8")
    after = load_doc(path)
    if len(after.raw_lines) != len(before.raw_lines):
        path.write_text(text, encoding="utf-8")
        raise SchemaError(f"{path}: 원문 라인 수가 변했습니다 - 되돌렸습니다")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"  변경기록 {stamp} {path} share_with -> {sorted(targets)}", file=sys.stderr)
    return f"[변경] {path.name}: share_with {sorted(before.share_with)} -> {sorted(targets)}"


def list_docs() -> int:
    root = archive_root() / "channels"
    if not root.is_dir():
        print(f"아카이브가 없습니다: {root}")
        return 1
    rows: list[tuple[str, str, str, str, int]] = []
    for p in sorted(root.rglob("*.md")):
        try:
            d = load_doc(p)
            rows.append(
                (d.workspace, d.visibility, ",".join(sorted(d.share_with)) or "-",
                 d.channel, len(d.raw_lines))
            )
        except SchemaError as e:
            rows.append(("?", "스키마오류", "-", f"{p.name} ({e})", 0))
    if not rows:
        print("문서가 없습니다.")
        return 0
    print(f"{'워크스페이스':<12} {'내부공개':<9} {'공유대상':<14} {'원문':>5}  채널")
    for ws, vis, sw, ch, n in rows:
        mark = "WS전체" if vis == "public" else vis
        print(f"{ws:<12} {mark:<9} {sw:<14} {n:>5}  {ch}")
    pub = sum(1 for r in rows if r[1] == "public")
    shared = sum(1 for r in rows if r[2] != "-")
    print(
        f"\n총 {len(rows)}건 | 워크스페이스 전체공개 {pub}건 | 타 워크스페이스 공유 {shared}건"
        "\n- '내부공개'(visibility) 는 자기 워크스페이스 안에서만 멤버십을 면제한다."
        "\n- '공유대상'(share_with) 이 동등 워크스페이스로 넘기는 유일한 경로다."
        "\n- 상위(root) 워크스페이스는 설정(ROOT_WORKSPACES)으로 산하 자료를 전량 열람한다."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="아카이브 문서 공개 여부 변경")
    ap.add_argument("paths", nargs="*", help="대상 MD 파일 경로")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_true", help="다른 워크스페이스에서 조회 가능하게")
    g.add_argument("--private", action="store_true", help="자기 워크스페이스로 제한")
    ap.add_argument("--share-with", metavar="KEY", action="append", default=None,
                    help="이 워크스페이스 키로 공유(여러 번 지정 가능)")
    ap.add_argument("--unshare", action="store_true", help="타 워크스페이스 공유 전체 해제")
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
