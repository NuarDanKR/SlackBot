#!/usr/bin/env python3
"""채널×일 다이제스트 만들기 (B-93 2단계). 기본은 **세기만** 한다(dry-run).

서버(운영):
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/build_channel_digests.py
    # 분량을 본 뒤 실제로 만들기
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
        /opt/tybot/.venv/bin/python /opt/tybot/scripts/build_channel_digests.py --apply

범위:
    기본은 최근 3일(오늘 제외). 소급은 --since/--until 로 정하고, 먼저 dry-run 으로
    호출 수·추정 토큰을 본 뒤 --apply 한다. --max-calls 가 한 번 실행의 상한이다.

대상은 `bot_source_channel` 의 그 봇 근거 채널이다(기본 vipbot). --channel 로 좁힐 수
있지만 근거 채널 밖으로 넓힐 수는 없다.

종료 코드: 0 정상 · 2 입력/설정 오류 · 3 일부 날짜 거절(출력 검사 탈락)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot import channel_digest_batch as batch
from tybot.envfile import load_env_file

EXIT_OK, EXIT_INPUT, EXIT_REJECTED = 0, 2, 3


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="채널×일 다이제스트 (기본 dry-run)")
    ap.add_argument("--bot", default="vipbot", help="근거 채널을 가져올 봇 (기본 vipbot)")
    ap.add_argument("--channel", action="append", default=[], help="채널 ID 로 좁히기(여러 번)")
    ap.add_argument("--since", type=_date, help="시작일 YYYY-MM-DD (KST)")
    ap.add_argument("--until", type=_date, help="종료일 YYYY-MM-DD (KST, 포함)")
    ap.add_argument("--apply", action="store_true", help="실제로 만든다(LLM 비용 발생)")
    ap.add_argument("--max-calls", type=int, default=batch.DEFAULT_MAX_CALLS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    load_env_file()
    archive = os.getenv("ARCHIVE_DIR", "").strip()
    if not archive or not Path(archive).is_dir():
        print("ARCHIVE_DIR 이 없거나 디렉터리가 아닙니다.", file=sys.stderr)
        return EXIT_INPUT
    since, until = batch.nightly_window(batch.today_kst())
    since = args.since or since
    until = args.until or until
    if since > until:
        print("--since 가 --until 보다 늦습니다.", file=sys.stderr)
        return EXIT_INPUT
    if not 1 <= args.max_calls <= 5000:
        print("--max-calls 는 1~5000 입니다.", file=sys.stderr)
        return EXIT_INPUT

    try:
        dsn = batch.resolve_dsn()
    except batch.DigestDsnError as exc:
        print(f"다이제스트 DB 접속 정보 문제: {exc.code}", file=sys.stderr)
        return EXIT_INPUT

    import psycopg

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        channels = batch.source_channels(conn, args.bot)
        if args.channel:
            wanted = set(args.channel)
            channels = [c for c in channels if c[1] in wanted]
        the_plan = batch.plan(conn, archive, channels, since=since, until=until)
        report = {"since": since.isoformat(), "until": until.isoformat(), **the_plan.to_json()}
        stats = None
        if args.apply and the_plan.calls:
            state = os.getenv("STATE_DIR", "/var/lib/tybot")
            router = batch.build_router(
                cost_state_path=str(Path(state) / "channel-digest-cost.json")
            )
            stats = batch.run(conn, the_plan, router, max_calls=args.max_calls)
            report["run"] = stats.to_json()

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"범위 {report['since']} ~ {report['until']} · 채널 {report['channels']}개")
        print(f"  원문 있는 날 {report['days_seen']} · 변경 없음 {report['unchanged']}")
        print(
            f"  만들 날(LLM 호출) {report['calls']} · 추정 입력 토큰 {report['est_input_tokens']:,}"
        )
        if stats is None:
            print("  (dry-run — 만들려면 --apply)" if not args.apply else "  만들 것이 없습니다")
        else:
            run = report["run"]
            print(
                f"  만듦 {run['made']} · 거절 {run['rejected'] or 0} · "
                f"상한으로 미룸 {run['skipped_over_cap']} · 비용 ${run['cost_usd']}"
            )
    return EXIT_REJECTED if stats is not None and stats.rejected else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
