"""Slack 멤버 ↔ 사번 매핑을 한 번에 이어 준다.

## 왜 필요한가
`identity.ensure` 는 **사람이 봇을 쓸 때** 불린다. 그래서 봇을 써 볼 이유가 없던 사람은
매핑이 없고, 매핑이 없으면 일정 DM 같은 자동 발송에서 조용히 빠진다 — **이메일이 맞아도
그렇다.** 실제로 8명 팀에서 4명만 잡혀 있었다(2026-09-11). 그 4명은 봇 명령을 써 본
사람들이었다.

기동 시에도 훑지만(`WorkspaceBot.identity_sweep`), 재시작을 기다리지 않고 지금 이어야
할 때 쓴다.

## 이메일은 저장하지 않는다
사번을 찾는 데만 쓰고 버린다. `user_identity` 에는 워크스페이스·Slack ID·사번만 남는다.

## 쓰기

    python scripts/backfill_identity.py                # 설정된 전 워크스페이스
    python scripts/backfill_identity.py --workspace tyit
    python scripts/backfill_identity.py --dry-run      # 무엇이 바뀔지만 본다
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

UNMATCHED_HINT = (
    "사번을 못 찾은 멤버는 둘 중 하나다 — Slack 프로필에 이메일이 없거나, 그 이메일이"
    " 인사 데이터(employee.email)와 다르다. 외부·게스트 계정은 안 이어지는 것이 맞다."
)


def _connect(*, read_only: bool):
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 이 없다.")
        raise SystemExit(2)
    import psycopg

    conn = psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    if read_only:
        # 드라이런은 **쓰지 않는다** 를 코드로 보장한다. 잊고 커밋하는 실수를 막는다.
        with conn.cursor() as cur:
            cur.execute("set transaction read only")
    return conn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Slack 멤버 ↔ 사번 매핑 훑기")
    ap.add_argument("--workspace", help="이 워크스페이스만. 생략하면 설정된 전체")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 결과만 본다")
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(message)s")
    load_env_file()

    from slack_sdk import WebClient

    from tybot.identity import backfill
    from tybot.workspaces import load_workspaces

    configs = [
        c for c in load_workspaces()
        if not args.workspace or c.key == args.workspace
    ]
    if not configs:
        print(f"워크스페이스를 찾지 못했다: {args.workspace!r}")
        return 2

    total_unmatched: list[tuple[str, str]] = []
    for cfg in configs:
        print(f"=== {cfg.key} ({cfg.label})")
        conn = _connect(read_only=args.dry_run)
        try:
            result = backfill(
                conn, WebClient(token=cfg.bot_token),
                workspace=cfg.key, dry_run=args.dry_run,
            )
        finally:
            conn.close()
        print("   " + result.summary())
        total_unmatched += [(cfg.key, u) for u in result.unmatched]

    if total_unmatched:
        print()
        print(f"사번을 못 찾은 멤버 {len(total_unmatched)}명:")
        for workspace, slack_user in total_unmatched[:50]:
            print(f"   {workspace}  {slack_user}")
        if len(total_unmatched) > 50:
            print(f"   … 그 외 {len(total_unmatched) - 50}명")
        print()
        print(UNMATCHED_HINT)
    if args.dry_run:
        print()
        print("드라이런이었다. 실제로 이으려면 --dry-run 없이 다시 실행한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
