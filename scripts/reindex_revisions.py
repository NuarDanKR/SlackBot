#!/usr/bin/env python3
"""검색 색인에서 **옛 revision 줄을 뺀다.**

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/reindex_revisions.py
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/reindex_revisions.py --dry-run

## 왜 필요한가

reader 는 **읽을 때** 거른다(`revision_reader`). 그런데 검색 색인(`raw_line`)에는
그 전에 넣은 줄이 그대로 있다 — `[수정 전]`·`[삭제 전]`·`[삭제됨]` 과, 고치기 전
본문까지.

색인은 「어느 줄이 맞나」 를 고르고 파일이 「그 줄을 볼 수 있나」 를 답한다. 그래서
색인에만 남은 옛 줄은 **후보로 올라왔다가 파일 쪽에서 떨어진다.** 결과가 틀리지는
않지만 두 가지가 생긴다.

1. 히트 수가 실제보다 많게 세어져 상한(`MAX_SEARCH_HITS`)이 엉뚱한 줄을 자른다
2. 직접 조회와 색인 조회의 후보 집합이 달라, 「같은 결과」 를 증명할 수 없다

`raw_line` 에는 **지우는 경로가 원래 없다**(원문을 편집하지 않는 것이 계약이라
옛 행은 쌓이기만 한다). 그래서 이 스크립트가 그 하나의 예외다 — 사람이 지웠거나
고친 줄만, 좌표를 찍어서 지운다.

## 무엇을 지우나

`revision_reader` 가 **감춘 줄**의 `(doc_path, line_no)` 뿐이다. 판정을 여기서
다시 쓰지 않는다 — 두 곳에 있으면 한 곳만 고치는 날이 오고, 그날 색인과 답변이
갈린다.

## 안전

- `--dry-run` 이 기본 점검 수단이다. 무엇을 지울지 먼저 센다
- 원문 MD 는 **읽기만** 한다. 한 글자도 안 고친다
- DB 를 못 보면 아무것도 지우지 않는다. reader 가 그때는 통째로 감추므로
  색인을 건드릴 이유도 없다
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import revision_reader  # noqa: E402
from tybot.archive.store import ArchiveStore  # noqa: E402


def stale_keys(store: ArchiveStore) -> list[tuple[str, int]]:
    """색인에서 빼야 할 좌표. **판정은 `revision_reader` 가 한다.**"""
    from tybot import search_index

    out: list[tuple[str, int]] = []
    for doc in store.audit_docs(dm_scope="*"):
        lines = list(doc.raw_lines)
        if not any(
            revision_reader.strip_marker(str(line.text or ""))[0] for line in lines
        ):
            continue
        keys = revision_reader.index_excluded_keys(
            lines,
            workspace=str(doc.workspace or ""),
            channel_id=str(doc.channel_id or ""),
        )
        for source_path, line_no in keys:
            path = search_index.rel_path(source_path or doc.path, store.root)
            out.append((path, line_no))
    return sorted(set(out))


def delete(keys: list[tuple[str, int]]) -> int:
    from tybot.console.workspace_store import _connect

    if not keys:
        return 0
    with _connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "DELETE FROM raw_line WHERE doc_path = %s AND line_no = %s", keys
        )
        return int(cur.rowcount or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    parser.add_argument("--dry-run", action="store_true", help="무엇을 지울지만 센다")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.envfile import load_env_file
    from tybot.paths import archive_dir

    load_env_file()
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL 이 없습니다. 색인을 볼 수 없어 아무것도 하지 않습니다.")
        return 2

    store = ArchiveStore(args.archive or archive_dir())
    keys = stale_keys(store)
    print(f"색인에서 뺄 줄: {len(keys)}개")
    for path, line_no in keys[:10]:
        print(f"  · {path}:{line_no}")
    if len(keys) > 10:
        print(f"  · … 외 {len(keys) - 10}건")

    if args.dry_run:
        print("\ndry-run 이었습니다. 실제 삭제는 --dry-run 없이 다시 실행하세요.")
        return 0
    removed = delete(keys)
    print(f"\n지운 색인 행: {removed}개")
    print("원문 MD 는 건드리지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
