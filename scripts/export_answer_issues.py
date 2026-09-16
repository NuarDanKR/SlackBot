#!/usr/bin/env python3
"""Export problematic QA records and linked feedback as a Markdown review packet.

The packet contains internal questions and answers. Write it to a protected server
directory and never commit it to this repository.

The report itself lives in `tybot.answer_issues` so that this CLI and the console's
download button produce **the same file**. Two copies would drift, and then "the file
I downloaded differs from the one the server made" has no arbiter.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.answer_issues import (  # noqa: E402
    KST,
    PROBLEM_FEEDBACK,
    SLOW_MS,
    active_feedback,
    build_report,
    feedback_by_record,
    issue_codes,
    load_feedback,
    load_records,
    read_jsonl,
    render_report,
    report_filename,
)
from tybot.envfile import load_env_file  # noqa: E402

__all__ = [
    "KST",
    "PROBLEM_FEEDBACK",
    "SLOW_MS",
    "active_feedback",
    "build_report",
    "feedback_by_record",
    "issue_codes",
    "load_feedback",
    "load_records",
    "read_jsonl",
    "render_report",
    "report_filename",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TYBot 답변 문제와 피드백을 Markdown으로 내보냅니다.")
    parser.add_argument("--days", type=int, default=7, help="조회 일수(기본 7일)")
    parser.add_argument("--workspace", default="", help="워크스페이스 키 필터")
    parser.add_argument("--limit", type=int, default=200, help="상세 문제 건수 상한")
    parser.add_argument("--qa-log-dir", default="", help="기본값은 QA_LOG_DIR")
    parser.add_argument("--env-file", default=os.getenv("TYBOT_ENV_FILE", ""))
    parser.add_argument("--output", default="", help="미지정 시 표준 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.days < 1 or args.limit < 1:
        raise SystemExit("--days와 --limit은 1 이상이어야 합니다.")
    if args.env_file:
        os.environ["TYBOT_ENV_FILE"] = args.env_file
    load_env_file()
    root = Path(args.qa_log_dir or os.getenv("QA_LOG_DIR", "./qa-log")).expanduser()
    report = build_report(
        root,
        days=args.days,
        workspace=args.workspace,
        limit=args.limit,
        now=datetime.now(KST),
    )
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"문제 후보 보고서를 생성했습니다: {output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
