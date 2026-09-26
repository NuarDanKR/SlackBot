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
고르지 않은 줄을 뺀다. 두 곳에 규칙이 있으면 한 곳만 고치는 날이 오고, 그날
색인과 답변이 갈린다.

| | |
|---|---|
| 넣는다 | `succeeded` · 최신 revision · raw 중복 아님 |
| 뺀다 | **DB 에 있는데 지금 넣을 것이 아닌 줄 전부** |

뺄 것은 파일이 아니라 **DB 를 보고 정한다.** 파일만 보면 두 가지를 못 잡는다.

1. **현재 경로에 남은 옛 content hash** — 같은 revision 파일을 다시 쓴 적이 있으면
   같은 `(경로, 줄 번호)` 에 옛 해시 행이 남아 있다. 경로가 「넣을 것」 이라
   파일 기준 정리에서는 통째로 건너뛴다
2. **두 번째 실행의 정직한 건수** — 이미 지운 줄을 계속 「뺄 것」 으로 세면
   멱등성을 확인할 수 없다. DB 에 남아 있는 것만 세므로 두 번째 실행은 0 이다

## 안전

- `--dry-run` 이 기본 점검 수단이다. 무엇이 들어가고 무엇이 빠지는지 먼저 센다
- 원문 MD 와 정본 MD 는 **읽기만** 한다
- 넣는 것을 먼저 하고 빼는 것을 나중에 한다. 중간에 실패해도 근거가 비지 않는다
- **권한 칸이 깨진 정본이 남아 있으면 시작하지 않는다.** reader 가 그런 문서를
  거절하므로 색인하면 「없는 자료」 로 굳는다 — `repair_attachment_rights.py` 가 먼저다
- **설정된 운영 아카이브만** 운영 색인에 넣는다. 다른 `--archive` 는 격리 DB
  (`--index-dsn`)를 줘야 돈다. 남의 아카이브를 운영 색인에 부으면 권한 밖 본문이
  검색 후보가 되고, 되돌리려면 어느 행이 그것이었는지 다시 찾아야 한다
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import attachment_reader, attachment_repair  # noqa: E402
from tybot.archive.store import ArchiveStore  # noqa: E402


def _isolation() -> object:
    """격리 DB 판정은 `verify_schema_isolated` 것을 **그대로 쓴다.**

    표시 목록을 복사하면 한쪽만 고치는 날이 오고, 그날 둘 중 하나가 운영 DB 를
    격리 DB 로 본다.
    """
    path = ROOT / "scripts" / "verify_schema_isolated.py"
    spec = importlib.util.spec_from_file_location("_verify_schema_isolated", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def isolated_dsn_refusal(dsn: str) -> str:
    """이 DSN 을 격리 색인으로 써도 되나. 안 되면 사유.

    `verify_schema_isolated` 와 같은 기준이되 `DATABASE_URL` 유무는 보지 않는다 —
    여기서는 운영 DSN 이 설정돼 있는 것이 정상이고, 문제는 **어디에 쓰느냐** 다.
    """
    guard = _isolation()
    name = guard.dsn_database(dsn)  # type: ignore[attr-defined]
    if not name:
        return "--index-dsn 에서 DB 이름을 읽지 못했습니다"
    lowered = name.lower()
    if lowered in guard.configured_databases():  # type: ignore[attr-defined]
        return (
            f"「{name}」 은 설정 파일이 가리키는 운영 DB 입니다."
            " 운영 색인에는 설정된 운영 아카이브만 넣습니다"
        )
    reserved = [m for m in guard.RESERVED_DB_MARKERS if m in lowered]  # type: ignore[attr-defined]
    if reserved:
        return f"「{name}」 은 다른 작업이 쓰는 DB 로 보입니다({' · '.join(reserved)})"
    safe = guard.SAFE_DB_MARKERS  # type: ignore[attr-defined]
    if not any(marker in lowered for marker in safe):
        return (
            f"「{name}」 이 격리 DB 인지 이름으로 알 수 없습니다 —"
            f" 이름에 {' · '.join(safe)} 중 하나를 넣은 DB 를 쓰세요"
        )
    return ""


def archive_refusal(requested: str | None, configured: str | None, index_dsn: str) -> str:
    """이 아카이브를 이 색인에 넣어도 되나. 안 되면 사유.

    운영 색인에 들어갈 수 있는 것은 **설정된 운영 아카이브뿐**이다. 다른 트리를
    부으면 그 본문이 운영 검색 후보가 되고, 권한은 그 트리의 프론트매터가 말하는
    대로 붙는다 — 우리 워크스페이스 멤버십과 아무 관계가 없다(절대 원칙 3·4).
    """
    if not requested:
        return ""
    if configured and _same_path(requested, configured):
        return ""
    if not index_dsn:
        return (
            f"--archive 가 설정된 운영 아카이브가 아닙니다({requested}).\n"
            "  다른 아카이브는 격리 색인에만 넣습니다 — --index-dsn 을 주세요."
        )
    return isolated_dsn_refusal(index_dsn)


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


@contextlib.contextmanager
def use_dsn(dsn: str):
    """이 블록 동안만 색인 DSN 을 바꾼다. **밖으로 새지 않는다.**"""
    if not dsn:
        yield
        return
    before = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = before


Key = tuple[str, int, str]


def expected_keys(keep, root) -> set[Key]:
    """넣고 나면 색인에 있어야 하는 `(경로, 줄, 내용 hash)`.

    `search_index.reindex` 가 만드는 것과 **같은 값**이어야 한다. 다르면 방금 넣은
    줄을 바로 지운다.
    """
    from tybot import search_index

    out: set[Key] = set()
    for doc in keep:
        for line in doc.raw_lines:
            if not line.text.strip():
                continue
            out.add((
                search_index.rel_path(line.source_path or doc.path, root),
                line.lineno,
                search_index.content_sha(doc.channel, line.lineno, line.text),
            ))
    return out


def attachment_paths(store: ArchiveStore, keep) -> list[str]:
    """정본이 차지한 색인 경로 전부. 옛 revision 과 실패·부분 변환본까지."""
    from tybot import search_index

    paths = {search_index.rel_path(doc.path, store.root) for doc in keep}
    paths |= {
        search_index.rel_path(doc.path, store.root)
        for doc in attachment_reader.audit_archive_docs(store.root)
    }
    return sorted(paths)


def fetch_existing(paths: list[str]) -> set[Key]:
    """DB 에 실제로 있는 행의 좌표. 없으면 빈 집합."""
    if not paths:
        return set()
    from tybot.console.workspace_store import _connect

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT doc_path, line_no, content_sha FROM raw_line "
            "WHERE doc_path = ANY(%s)",
            (paths,),
        )
        return {(str(r[0]), int(r[1]), str(r[2])) for r in cur.fetchall()}


def plan(store: ArchiveStore, fetch=fetch_existing) -> tuple[list, list[Key]]:
    """`(넣을 문서, 뺄 색인 좌표)`.

    뺄 것은 **정본 경로에 있는 DB 행 중 지금 넣을 것이 아닌 것** 전부다. 옛
    revision 뿐 아니라 같은 경로에 남은 옛 content hash 도 여기 들어온다.
    """
    # 중복 방지 다리는 **채널 원문만** 봐야 한다. 첨부 문서를 같이 넘기면
    # 자기 자신을 「raw 에 이미 있다」 로 읽는다.
    channels = [
        doc for doc in store.docs(dm_scope="*")
        if not attachment_reader.is_attachment_doc(doc)
    ]
    keep = attachment_reader.archive_docs(store.root, channels)
    expected = expected_keys(keep, store.root)
    existing = fetch(attachment_paths(store, keep))
    return keep, sorted(existing - expected)


def delete(keys: list[Key]) -> int:
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
    parser.add_argument(
        "--index-dsn", default="",
        help="격리 색인 DSN. 운영 아카이브가 아닌 --archive 를 넣을 때만",
    )
    parser.add_argument("--dry-run", action="store_true", help="무엇이 바뀔지만 센다")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.envfile import load_env_file
    from tybot.paths import archive_dir

    load_env_file()
    configured = str(archive_dir())
    refusal = archive_refusal(args.archive, configured, args.index_dsn)
    if refusal:
        print(refusal)
        return 4

    with use_dsn(args.index_dsn):
        if not os.getenv("DATABASE_URL"):
            print("DATABASE_URL 이 없습니다. 색인을 볼 수 없어 아무것도 하지 않습니다.")
            return 2

        store = ArchiveStore(args.archive or configured)

        # 권한 칸이 깨진 정본이 남아 있으면 **여기서 멈춘다.** reader 가 거절하는
        # 문서를 색인하면 「없는 자료」 로 굳고, 나중에 고쳐도 그 사실이 안 보인다.
        channels = [
            doc for doc in store.audit_docs(dm_scope="*")
            if not attachment_reader.is_attachment_doc(doc)
        ]
        blocked = attachment_repair.blocking_reason(
            attachment_repair.survey(store.root, channels)
        )
        if blocked:
            print(blocked)
            return 3

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
