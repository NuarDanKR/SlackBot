#!/usr/bin/env python3
"""첨부 정본을 검색 색인에 넣고, **근거가 아닌 판을 뺀다.**

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/reindex_attachments.py --dry-run
    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/reindex_attachments.py

## 왜 필요한가

정본 경로(`channels/<id>/attachments/<file-id>/<revision>.md`)는 원문 글롭 밖이라
**색인에 한 줄도 없다.** reader 가 붙어도 색인은 후보를 못 내므로, 그 자료는
파일 스캔으로만 잡히고 대개 상한에 걸려 사라진다.

반대로 한 번 넣고 나면 **옛 판이 계속 남는다.** 같은 파일을 더 나은 변환기로 다시
읽으면 새 revision 이 생기는데, `raw_line` 에는 지우는 경로가 없다. 그대로 두면
낡은 본문이 후보로 올라왔다가 파일 쪽에서 떨어진다 — 결과가 틀리지는 않지만
히트 수가 부풀고, 직접 조회와 색인의 후보 집합이 달라진다.

## 무엇을 넣고 무엇을 빼나

**판정을 여기서 다시 쓰지 않는다.** `attachment_reader` 가 고른 것만 넣고, 그가
고르지 않은 판을 뺀다. 두 곳에 규칙이 있으면 한 곳만 고치는 날이 오고, 그날
색인과 답변이 갈린다.

| | |
|---|---|
| 넣는다 | `succeeded` · 최신 revision · raw 중복 아님 |
| 뺀다 | 옛 revision · 실패·부분·대기 · raw 에 이미 있는 것 |

## 안전

- `--dry-run` 이 기본 점검 수단이다. 무엇이 들어가고 무엇이 빠지는지 먼저 센다
- 원문 MD 와 정본 MD 는 **읽기만** 한다
- 넣는 것을 먼저 하고 빼는 것을 나중에 한다. 중간에 실패해도 근거가 비지 않는다
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import attachment_reader  # noqa: E402
from tybot.archive.store import ArchiveStore  # noqa: E402


def plan(store: ArchiveStore) -> tuple[list, list[tuple[str, int, str]]]:
    """`(넣을 문서, 뺄 색인 좌표)`.

    뺄 것은 **정본 경로 중 reader 가 고르지 않은 것** 전부다. 파일별로 최신
    하나만 남으므로, 나머지 판의 좌표가 여기 들어온다.
    """
    from tybot import search_index

    # 중복 방지 다리는 **채널 원문만** 봐야 한다. 첨부 문서를 같이 넘기면
    # 자기 자신을 「raw 에 이미 있다」 로 읽는다.
    channels = [
        doc for doc in store.docs(dm_scope="*")
        if not attachment_reader.is_attachment_doc(doc)
    ]
    keep = attachment_reader.archive_docs(store.root, channels)
    kept_paths = {str(doc.path) for doc in keep}

    stale: list[tuple[str, int, str]] = []
    for doc in attachment_reader.audit_archive_docs(store.root):
        if str(doc.path) in kept_paths:
            continue
        rel = search_index.rel_path(doc.path, store.root)
        for line in doc.raw_lines:
            stale.append(
                (rel, line.lineno, search_index.content_sha(doc.channel, line.lineno, line.text))
            )
    return keep, sorted(set(stale))


def delete(keys: list[tuple[str, int, str]]) -> int:
    from tybot.console.workspace_store import _connect

    if not keys:
        return 0
    with _connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "DELETE FROM raw_line "
            "WHERE doc_path = %s AND line_no = %s AND content_sha = %s",
            keys,
        )
        return int(cur.rowcount or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    parser.add_argument("--dry-run", action="store_true", help="무엇이 바뀔지만 센다")
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
    found = attachment_reader.source_files(store.root)
    keep, stale = plan(store)

    print(f"정본 파일: {len(found)}개")
    print(f"근거로 넣을 문서: {len(keep)}개")
    print(f"색인에서 뺄 줄: {len(stale)}개")
    for path, line_no, _digest in stale[:10]:
        print(f"  · {path}:{line_no}")
    if len(stale) > 10:
        print(f"  · … 외 {len(stale) - 10}건")

    if args.dry_run:
        print("\ndry-run 이었습니다. 실제 반영은 --dry-run 없이 다시 실행하세요.")
        return 0

    from tybot import search_index

    # 넣는 것이 먼저다. 중간에 실패해도 근거가 비지 않는다.
    indexed = search_index.reindex(keep, store.root)
    removed = delete(stale)
    print(f"\n색인한 줄: {indexed['lines']}개")
    print(f"뺀 과거 색인 행: {removed}개")
    print("원문 MD 와 정본 MD 는 건드리지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
