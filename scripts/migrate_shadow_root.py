"""워크스페이스별 shadow root 를 공통 root 하나로 모은다.

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §5

기본은 **계획만** 낸다. 실제 복사는 `--apply` 를 붙여야 한다. 어느 쪽이든 원본은
지우지 않는다 — 검증 기간 동안 옛 root 를 읽기 전용으로 남긴다(§5-6).

서버에서:

```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/migrate_shadow_root.py \\
    --source tyit=/var/lib/tybot/archiver-shadow/tyit \\
    --source mgmt=/var/lib/tybot/archiver-shadow/mgmt \\
    --destination /var/lib/tybot/archiver-shadow \\
    --live-archive /var/lib/tybot/archive
```
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tybot.archive import shadow_root


def parse_source(value: str) -> shadow_root.SourceRoot:
    workspace, _, path = value.partition("=")
    if not workspace.strip() or not path.strip():
        raise argparse.ArgumentTypeError("--source 는 `workspace=/절대/경로` 형식입니다")
    return shadow_root.SourceRoot(workspace.strip().lower(), Path(path.strip()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Archiver shadow root 공통화")
    parser.add_argument(
        "--source", action="append", required=True, type=parse_source,
        help="워크스페이스별 옛 root 의 부모. 예: tyit=/var/lib/tybot/archiver-shadow/tyit",
    )
    parser.add_argument("--destination", required=True, help="공통 root 의 부모 경로")
    parser.add_argument(
        "--live-archive", required=True,
        help="운영 ARCHIVE_DIR. 목적지가 이것과 섞이는지 판정하는 데만 쓴다",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="실제로 복사한다. 생략하면 계획만 출력하고 아무것도 바꾸지 않는다",
    )
    parser.add_argument("--json", action="store_true", help="계획을 JSON 으로 출력")
    args = parser.parse_args()

    planned = shadow_root.plan(
        args.source, Path(args.destination), live_archive=Path(args.live_archive),
    )
    if args.json:
        print(json.dumps(planned.as_json(), ensure_ascii=False, indent=2))
    else:
        counts = planned.as_json()["counts"]
        print(f"목적지 원문 root: {shadow_root.archive_root(planned.destination)}")
        print(
            f"복사 {counts['copy']}건 · 이미 같음 {counts['same']}건"
            f" · 내용 충돌 {counts['conflict']}건"
        )
        for refusal in planned.refusals:
            print(f"  거부: {refusal}")

    if planned.blocked:
        print("거부 사유가 있어 옮기지 않습니다.", file=sys.stderr)
        return 2
    if not args.apply:
        print("계획만 출력했습니다. 실제로 옮기려면 --apply 를 붙이세요.")
        return 0

    result = shadow_root.apply(planned)
    problems = shadow_root.verify(planned)
    print(f"복사 {result['copied']}건 · 건너뜀 {result['skippedSame']}건")
    if problems:
        for problem in problems:
            print(f"  검증 실패: {problem}", file=sys.stderr)
        return 3
    print("복사본의 SHA-256 이 원본과 모두 일치합니다. 옛 root 는 지우지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
