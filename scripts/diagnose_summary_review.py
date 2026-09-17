#!/usr/bin/env python3
"""요약 검토가 실제로 작동하는지 잰다 (B-60 1단계 실측).

    sudo -u tybot /opt/tybot/.venv/bin/python \\
        /opt/tybot/scripts/diagnose_summary_review.py --days 30

## 왜 필요한가

생성 요약(`abstract`)을 허용한 값어치는 **후보 수가 줄고 승인률이 유지되는가**로만
판단할 수 있다. 설계 문서에 「이 수치 없이 2단계를 시작하지 않는다」 고 적어 뒀는데,
수치를 낼 수단이 없으면 그 문장은 교착이 된다. 이 스크립트가 그 수단이다.

재는 것은 셋이다.

| 수치 | 무엇을 말하는가 |
|---|---|
| `form` 별 후보 수 | 생성 요약이 실제로 묶어 주고 있는가 |
| `form` 별 승인률 | 묶은 문장을 사람이 받아들이는가 |
| 보여 준 뒤 결정까지 걸린 시간 | 검토자가 감당할 분량인가 |

## 판정만 한다

아무것도 승인하지 않고 아무 행도 바꾸지 않는다. 읽기 전용이다.

## 본문은 찍지 않는다

후보 문장과 근거 인용은 출력하지 않는다. 운영자가 보는 것은 **건수와 비율**이고,
문장이 필요하면 검토 Canvas 에서 본다.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot.envfile import load_env_file
from tybot.summary_review import (
    DECIDED_STATES,
    FORMS,
    Store,
    summarize_forms,
)

EXIT_OK = 0
EXIT_INPUT = 2


def _rate(value: float | None) -> str:
    """승인률. **결정이 없으면 `-`** — 0% 와 다르다."""
    return "-" if value is None else f"{value * 100:.0f}%"


def _duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "-"
    if seconds < 3600:
        return f"{seconds / 60:.0f}분"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}시간"
    return f"{seconds / 86400:.1f}일"


def report(stats: list[dict], latency: list[dict], *, since: date) -> list[str]:
    """사람이 읽을 줄. **수치를 해석해 주지 않는다** — 판단은 사람이 한다."""
    out = [f"=== 요약 검토 실측 ({since.isoformat()} 이후)"]
    if not stats:
        out.append("  이 기간에 만들어진 검토 후보가 없다.")
        return out

    folded = summarize_forms(stats)
    out.append("")
    out.append("--- 전체")
    out.append(f"{'형식':<10}{'후보':>7}{'승인':>7}{'반려':>7}{'대기':>7}"
               f"{'보류':>7}{'폐기':>7}{'승인률':>8}")
    for form in FORMS:
        found = folded.get(form)
        if not found:
            continue
        out.append(
            f"{form:<10}{found.total:>7}{found.approved:>7}{found.rejected:>7}"
            f"{found.pending:>7}{found.deferred:>7}{found.expired:>7}"
            f"{_rate(found.approval_rate):>8}"
        )

    by_channel: dict[tuple[str, str], list[dict]] = {}
    names: dict[tuple[str, str], str] = {}
    for row in stats:
        key = (str(row.get("workspace") or ""), str(row.get("channel_id") or ""))
        by_channel.setdefault(key, []).append(row)
        if row.get("channel_name"):
            names[key] = str(row["channel_name"])

    out.append("")
    out.append("--- 채널별")
    for key in sorted(by_channel):
        workspace, channel_id = key
        label = names.get(key) or channel_id
        parts = []
        for form, found in sorted(summarize_forms(by_channel[key]).items()):
            parts.append(
                f"{form} {found.total}건(승인 {found.approved}·"
                f"{_rate(found.approval_rate)})"
            )
        out.append(f"  {workspace}/{label}: " + " · ".join(parts))

    out.append("")
    out.append("--- 보여 준 뒤 결정까지 (자동 폐기 제외)")
    if not latency:
        out.append("  사람이 결정한 후보가 아직 없다.")
    for row in latency:
        out.append(
            f"  {row.get('workspace')}/{row.get('channel_id')} "
            f"{row.get('form')}: {int(row.get('decided') or 0)}건 · "
            f"평균 {_duration(row.get('avg_seconds'))} · "
            f"최장 {_duration(row.get('max_seconds'))}"
        )

    out.append("")
    out.append("--- 읽는 법")
    out.append("  후보 수가 줄고 abstract 승인률이 quote 와 비슷하면 묶기가 통한 것이다.")
    out.append("  abstract 승인률만 낮으면 생성 문장이 원문을 잘못 묶고 있다는 뜻이다.")
    out.append(f"  승인률 분모는 사람이 결정한 것뿐이다({', '.join(DECIDED_STATES)}).")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="요약 검토 실측 (읽기 전용)")
    ap.add_argument("--days", type=int, default=30, help="며칠치를 볼 것인가")
    ap.add_argument("--workspace", default="", help="이 워크스페이스만")
    ap.add_argument("--channel", default="", help="이 채널 ID 만")
    args = ap.parse_args(argv)

    load_env_file()
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL 이 없다. 검토 후보는 DB 에 있다.", file=sys.stderr)
        return EXIT_INPUT

    since = date.today() - timedelta(days=max(1, args.days))

    import psycopg

    with psycopg.connect(
        os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row
    ) as conn:
        store = Store(conn)
        stats = store.form_stats(
            since=since, workspace=args.workspace, channel_id=args.channel,
        )
        latency = store.review_latency(
            since=since, workspace=args.workspace, channel_id=args.channel,
        )

    for line in report(stats, latency, since=since):
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
