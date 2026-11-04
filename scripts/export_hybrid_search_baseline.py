#!/usr/bin/env python3
"""Export a body-free QA baseline for B-12 shadow retrieval evaluation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.answer_issues import (  # noqa: E402
    KST,
    feedback_by_record,
    load_feedback,
    load_records,
)
from tybot.hybrid_search_eval import classify_baseline  # noqa: E402

SCHEMA = "tybot.hybrid-search-baseline.v1"


def build_baseline(
    root: Path,
    *,
    since: date,
    workspace: str = "",
    generated_at: datetime,
) -> dict:
    records = load_records(root, since=since, workspace=workspace)
    feedback = feedback_by_record(
        load_feedback(root, since=since, workspace=workspace)
    )
    classified = [
        classify_baseline(
            row, feedback.get(str(row.get("record_id") or ""), [])
        )
        for row in records
    ]
    states = Counter(item.state for item in classified)
    eligible = Counter(item.state for item in classified if item.eligible)
    return {
        "schema": SCHEMA,
        "generatedAt": generated_at.astimezone(KST).isoformat(),
        "since": since.isoformat(),
        "workspace": workspace or "all",
        "records": len(records),
        "states": dict(sorted(states.items())),
        "eligible": dict(sorted(eligible.items())),
        "eligibleTotal": sum(eligible.values()),
        "unknownNoEvidence": states.get("unknown_no_evidence", 0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="질문·답변 본문 없이 B-12 shadow 평가 기준선 건수만 내보냅니다."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--qa-log-dir", default="")
    parser.add_argument("--output", default="", help="미지정 시 표준 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.days < 1:
        raise SystemExit("--days는 1 이상이어야 합니다.")
    now = datetime.now(KST)
    root = Path(args.qa_log_dir or os.getenv("QA_LOG_DIR", "./qa-log")).expanduser()
    payload = build_baseline(
        root,
        since=(now - timedelta(days=args.days)).date(),
        workspace=args.workspace,
        generated_at=now,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"본문 없는 shadow 기준선을 생성했습니다: {output}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
