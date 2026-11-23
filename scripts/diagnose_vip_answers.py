#!/usr/bin/env python3
"""VIPBot 같은 질문이 사람·회차마다 다르게 답하는지 **기록으로** 가른다 (읽기 전용 진단).

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_vip_answers.py \\
        --contains 재무목표 --days 14
    … --root /var/lib/tybot-vipbot/qa      # 기본값
    … --json

## 무엇을 보나

VIP 기록(`qa-YYYY-MM.jsonl`)에는 회차마다 **질문·사용자·reason·근거 좌표**가 남는다
(`vipbot_history_writer`). 답변 문장은 없다. 그래서 이 스크립트가 가를 수 있는 것은
「근거를 몇 줄 읽었나」 다.

| 근거 좌표 | reason | 뜻 |
|---|---|---|
| 0 | `no_hits` | **검색이 비었다.** 그 시점 색인·아카이브·검색어 문제 |
| 1 이상 | `answered` | 근거를 읽었다. 그런데도 「찾을 수 없다」 였다면 **모델 판단** 문제 |

같은 질문을 성공·실패 회차로 나눠, 근거 문서가 회차마다 같았는지도 비교한다. 문서가
갈렸으면 그 사이에 아카이브·색인이 바뀐 것이다.

## 안 하는 것

- 고치지 않는다. 아무것도 쓰지 않는다
- 사용자 ID 는 끝 4자만, 질문은 앞 40자만 보인다. 답변 본문은 원래 기록에 없다
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DEFAULT_ROOT = pathlib.Path("/var/lib/tybot-vipbot/qa")


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def load(
    root: pathlib.Path, *, contains: str, days: int, now: datetime | None = None
) -> list[dict]:
    now = now or datetime.now(KST)
    cutoff = now - timedelta(days=days)
    contains = (contains or "").replace(" ", "")
    rows: list[dict] = []
    for path in sorted(root.glob("qa-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _when(row.get("ts"))
            if when is None or when < cutoff:
                continue
            question = str(row.get("question") or "")
            if contains and contains not in question.replace(" ", ""):
                continue
            refs = row.get("evidence_refs") or []
            docs = sorted(
                {str(ref.get("document_path") or "") for ref in refs if isinstance(ref, dict)}
            )
            rows.append(
                {
                    "ts": when.isoformat(timespec="seconds"),
                    "user": "…" + str(row.get("user") or "")[-4:],
                    "question": question[:40],
                    "reason": str(row.get("intent_kind") or ""),
                    "refs": len(refs),
                    "docs": docs,
                }
            )
    rows.sort(key=lambda r: r["ts"])
    return rows


def summarize(rows: list[dict]) -> dict:
    reasons = Counter(row["reason"] or "-" for row in rows)
    empty = [row for row in rows if row["refs"] == 0]
    with_refs = [row for row in rows if row["refs"] > 0]
    doc_sets = Counter(tuple(row["docs"]) for row in with_refs)
    verdict = []
    if empty:
        verdict.append(f"근거 0건 회차 {len(empty)}건 — 검색이 빈 경우(색인·아카이브·검색어 쪽)")
    if len(doc_sets) > 1:
        verdict.append(f"근거 문서 묶음이 {len(doc_sets)}가지 — 회차 사이에 아카이브·색인이 바뀜")
    if with_refs and not empty and len(doc_sets) == 1:
        verdict.append("모든 회차가 같은 근거를 읽음 — 실패했다면 모델 판단 쪽")
    return {
        "total": len(rows),
        "reasons": dict(reasons),
        "docSets": len(doc_sets),
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--contains", required=True, help="질문에 든 낱말(띄어쓰기 무시)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = load(pathlib.Path(args.root), contains=args.contains.replace(" ", ""), days=args.days)
    summary = summarize(rows)
    if args.json:
        print(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False))
        return 0
    if not rows:
        print("해당 질문 기록이 없습니다. --root·--days·--contains 를 확인하세요.", file=sys.stderr)
        return 1
    for row in rows:
        print(
            f"{row['ts']}  user={row['user']}  reason={row['reason'] or '-':<10} "
            f"refs={row['refs']:<3} docs={len(row['docs'])}  {row['question']}"
        )
        for doc in row["docs"][:5]:
            print(f"      · {doc}")
    print(
        f"\n합계 {summary['total']}회 · reason {summary['reasons']} · 근거 묶음 {summary['docSets']}가지"
    )
    for line in summary["verdict"]:
        print(f"→ {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
