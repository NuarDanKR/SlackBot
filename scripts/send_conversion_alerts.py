#!/usr/bin/env python3
"""첨부 변환 실패·부분 누락을 채널 담당자에게 알린다 (B-45 §6).

    .venv/bin/python scripts/send_conversion_alerts.py            # 판정만
    .venv/bin/python scripts/send_conversion_alerts.py --apply    # 실제 발송

## 언제 알리나

**검토를 막을 때만.** 큐에서 아직 재시도가 남아 있으면 알리지 않는다 — 몇 분 뒤
성공할 수도 있는 일로 사람을 부르면, 곧 그 DM 을 아무도 읽지 않게 된다.

## 무엇을 담나

파일명, Slack 원본 링크, 비민감 오류 분류, 다음 조치, 확인 범위. 그뿐이다.
본문·질문·요약은 담지도 저장하지도 않는다.

종료 코드: `0` 정상 · `1` 일부 발송 실패 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot import conversion_alerts as alerts
from tybot.envfile import load_env_file

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2


def _queue_state():
    """큐 상태 조회 함수. 큐를 못 쓰면 `None` — **알림은 계속된다.**"""
    try:
        from tybot.conversion_queue import pending_for
    except Exception:  # noqa: BLE001
        return None

    def lookup(workspace: str, channel_id: str, file_id: str) -> str | None:
        row = pending_for(workspace, channel_id, file_id)
        return str(row.get("state")) if row else None

    return lookup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="실제로 DM 을 보낸다")
    ap.add_argument("--archive", default="")
    args = ap.parse_args(argv)

    load_env_file()
    archive = args.archive or os.getenv("ARCHIVE_DIR", "./archive")

    queue_state = _queue_state()
    try:
        pending = alerts.collect(archive, queue_state=queue_state)
    except Exception as exc:  # noqa: BLE001
        print(f"알림 대상을 읽지 못했습니다: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if not pending:
        print("알릴 것이 없습니다.")
        return EXIT_OK

    if not args.apply:
        print(f"알릴 채널 {len(pending)}곳:")
        for alert in pending:
            print(f"  {alert.workspace}/{alert.channel_id} — 파일 {len(alert.items)}건")
        print("\n실제로 보내려면 `--apply` 를 붙이세요.")
        return EXIT_OK

    from tybot.slack.pilot import build_bots

    bots = build_bots()
    clients = {bot.workspace: bot.app.client for bot in bots}
    names = {
        (bot.workspace, cid): name
        for bot in bots
        for cid, name in getattr(bot, "_chan_cache", {}).items()
    }
    result = alerts.run(
        clients, archive_dir=archive, channel_names=names, queue_state=queue_state
    )
    print(
        f"발송 {result.sent}건 · 건너뜀 {result.skipped}건 · "
        f"실패 {result.failed}건 · 수신자 없음 {result.no_recipient}건"
    )
    return EXIT_FAILED if result.failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
