#!/usr/bin/env python3
"""Export a body-free QA baseline for B-12 shadow retrieval evaluation."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.answer_issues import (  # noqa: E402
    KST,
    failure_code,
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
    breakdown = _breakdown(records, classified)
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
        "breakdown": breakdown,
    }


#: 근거를 찾은 답(`grounded_sample`)과 해당 없음은 쪼개 보지 않는다. 고칠 대상은 나머지다.
BREAKDOWN_SKIP = frozenset({"grounded_sample", "not_applicable"})
_CODE = re.compile(r"^[a-z0-9_.:-]{1,60}$")


def _code(value: object) -> str:
    """고정 낱말만 그대로 둔다. 문장·경로가 섞인 값은 `other` 로 묶는다."""
    text = str(value or "").strip().casefold()
    if not text:
        return "none"
    return text if _CODE.fullmatch(text) else "other"


def _breakdown(records: list[dict], classified) -> dict:
    """근거 없는 답의 상태마다 **왜** 그렇게 분류됐는지를 건수로.

    상태만으로는 고칠 자리가 안 보인다. `search_failed` 18건이 시간 초과인지
    전문 봇 실패인지, `unknown_no_evidence` 가 어떤 reason 을 달고 있는지가
    다음 수정의 대상을 정한다. 본문은 없고 고정 코드와 건수뿐이다.
    """
    out: dict[str, dict[str, Counter]] = {}
    for row, item in zip(records, classified, strict=True):
        if item.state in BREAKDOWN_SKIP:
            continue
        slot = out.setdefault(item.state, {
            "reasons": Counter(), "issueCodes": Counter(), "failureCodes": Counter(),
        })
        slot["reasons"][_code(row.get("reason"))] += 1
        for code in item.issue_codes:
            slot["issueCodes"][_code(code)] += 1
        slot["failureCodes"][_code(failure_code(row))] += 1
    return {
        state: {name: dict(counter.most_common()) for name, counter in parts.items()}
        for state, parts in sorted(out.items())
    }


def resolve_qa_log_dir(explicit: str) -> Path:
    """QA 기록 위치. env 파일을 **먼저** 읽고, 없는 경로면 멈춘다.

    서버에서 `QA_LOG_DIR` 는 `/etc/tybot/tybot.env` 에만 있다. env 파일을 안 읽으면
    `./qa-log` 로 떨어지고, 그 경로가 없으면 기록 0건을 **정상 결과처럼** 낸다 —
    「무근거 답변이 없다」 와 「기록을 못 봤다」 가 같은 출력이 된다.
    """
    from tybot.envfile import load_env_file

    load_env_file()
    root = Path(explicit or os.getenv("QA_LOG_DIR", "./qa-log")).expanduser()
    if not root.is_dir():
        raise SystemExit(f"QA 기록 폴더가 없습니다: {root} (--qa-log-dir 또는 QA_LOG_DIR 확인)")
    return root


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
    root = resolve_qa_log_dir(args.qa_log_dir)
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
