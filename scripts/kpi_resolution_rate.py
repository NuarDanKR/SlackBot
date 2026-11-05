"""KPI「질문 해결률」을 **감사 기록에서 다시 센다.** 읽기 전용이다.

    해결률 = 출처가 붙은 업무 답변 ÷ 사람이 던진 업무 질문 전부

콘솔의 `groundedRate` 와 **일부러 다르게** 센다. 둘은 쓰임이 다르다.

| | 분자 | 분모 |
|---|---|---|
| 콘솔 `groundedRate` (진단) | `hits > 0` | 업무 질문 − 권한 미확인 |
| 이 KPI (성과) | `citations` 가 있음 | 업무 질문 **전부** |

- **분자를 `citations` 로 세는 이유**: `hits > 0` 은 「검색이 뭔가 걸렸다」 일 뿐
  답에 출처를 붙였다는 뜻이 아니다. 인용은 이미 기록돼 있는데 아무 지표도 안 썼다.
- **분모에 실패를 포함하는 이유**: 묻는 사람에게는 권한 확인 실패도 오류도
  「답을 못 받았다」 다. 분모에서 빼면 **어려운 질문을 피할수록 KPI 가 오른다.**
  올리는 길이 자료를 쌓고 검색을 고치는 것뿐이어야 한다.

진단은 원인별로 쪼개서 본다(콘솔). KPI 는 결과 하나만 본다. 두 수가 다른 것이 맞다.

사용법:

    python scripts/kpi_resolution_rate.py                     # 최근 90일
    python scripts/kpi_resolution_rate.py --days 30
    python scripts/kpi_resolution_rate.py --qa-log /var/lib/tybot/qa-log
    python scripts/kpi_resolution_rate.py --json out.json     # 기계용

서버에서는 venv 로 부른다.

    sudo -u tybot /opt/tybot/.venv/bin/python \\
        /opt/tybot/scripts/kpi_resolution_rate.py --days 90

**이 스크립트는 아무것도 쓰지 않는다.** 질문·답변 본문도 읽지 않고 세지도 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tybot.answer_status import answer_failed, failure_code

KST = timezone(timedelta(hours=9))

#: 업무 질문이 아닌 것. 봇이 잘못한 게 없는데 분모에 들어가면 지표가 깎인다.
#: `intent.py` 의 `SELF_KINDS` 와 같은 축이다 — 거기에 값이 늘면 여기도 늘려야 한다.
NON_BUSINESS = frozenset({"status", "help", "memory", "smalltalk", "out_of_scope", "ingest"})

#: 비율을 내려면 최소 이만큼. `console/health.py` 의 `MIN_SAMPLE` 과 같은 값이다.
#: 모자라면 **0% 가 아니라 `None`** 이다 — 못 본 것과 없는 것은 다르다.
MIN_SAMPLE = 10


def parse_ts(raw: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=KST) if moment.tzinfo is None else moment


def is_business(row: dict) -> bool:
    """업무 질문인가.

    `intent_kind` 가 `error` 인 줄이 있다(처리 중 예외). 그 줄은 **의도를 잃은
    것이지 잡담이 아니다.** 업무로 센다 — 빼면 오류가 많을수록 KPI 가 좋아진다.
    """
    return str(row.get("intent_kind") or "").strip() not in NON_BUSINESS


def is_resolved(row: dict) -> bool:
    """출처가 붙은 답변인가."""
    citations = row.get("citations") or []
    if not isinstance(citations, list):
        return False
    return bool([c for c in citations if str(c).strip()]) and not answer_failed(row)


def unresolved_reason(row: dict) -> str:
    """왜 해결이 아닌가. **지표가 아니라 고칠 자리를 가리키는 값이다.**"""
    if answer_failed(row):
        return f"실패:{failure_code(row) or 'unknown'}"
    reason = str(row.get("reason") or "").strip()
    if reason == "permission_unverified":
        return "권한 미확인"
    if reason in {"no_hits", "channel_no_archive", "no_access"}:
        return {"no_hits": "근거 없음", "channel_no_archive": "채널 아카이브 없음",
                "no_access": "열람 권한 없음"}[reason]
    if int(row.get("hits") or 0) > 0:
        # 근거는 찾았는데 출처를 안 붙였다. 원칙 2 위반이라 따로 보여야 한다.
        return "출처 미표기(근거는 있었음)"
    return f"기타:{reason or '미기재'}"


def load(qa_log: Path, since: datetime) -> tuple[list[dict], list[str], int]:
    """기간 안의 줄만. 읽은 파일 이름과 **깨진 줄 수**를 같이 돌려준다.

    깨진 줄을 조용히 버리면 분모가 줄고 KPI 가 올라간다. 세서 보고한다.
    """
    if not qa_log.is_dir():
        raise SystemExit(f"감사 기록 디렉터리가 없습니다: {qa_log}")
    rows: list[dict] = []
    read: list[str] = []
    broken = 0
    for path in sorted(qa_log.glob("qa-*.jsonl")):
        month = path.stem.removeprefix("qa-")
        if month < since.strftime("%Y-%m"):
            continue
        read.append(path.name)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            moment = parse_ts(row.get("ts") or "")
            if moment is None:
                broken += 1
                continue
            if moment >= since:
                rows.append(row)
    return rows, read, broken


def rate(resolved: int, asked: int) -> float | None:
    """표본이 모자라면 **비율을 내지 않는다.**"""
    return resolved / asked if asked >= MIN_SAMPLE else None


def tally(rows: list[dict]) -> dict:
    business = [r for r in rows if is_business(r)]
    resolved = [r for r in business if is_resolved(r)]
    reasons = Counter(unresolved_reason(r) for r in business if not is_resolved(r))

    by_ws: dict[str, dict] = {}
    for row in business:
        key = str(row.get("workspace") or "(없음)")
        slot = by_ws.setdefault(key, {"asked": 0, "resolved": 0})
        slot["asked"] += 1
        slot["resolved"] += int(is_resolved(row))
    for slot in by_ws.values():
        slot["rate"] = rate(slot["resolved"], slot["asked"])

    # 같은 기간을 콘솔 방식으로도 세어 **차이를 눈에 보이게** 둔다.
    # 목표치를 정할 때 두 수를 섞으면 콘솔은 초록인데 KPI 는 미달인 상태가 된다.
    searched = [r for r in business if str(r.get("reason") or "") != "permission_unverified"]
    console_grounded = [
        r for r in searched if int(r.get("hits") or 0) > 0 and not answer_failed(r)
    ]
    return {
        "logged": len(rows),
        "nonBusiness": len(rows) - len(business),
        "asked": len(business),
        "resolved": len(resolved),
        "resolutionRate": rate(len(resolved), len(business)),
        "consoleGroundedRate": rate(len(console_grounded), len(searched)),
        "unresolvedReasons": reasons.most_common(),
        "byWorkspace": dict(sorted(by_ws.items(), key=lambda kv: -kv[1]["asked"])),
    }


def percent(value: float | None) -> str:
    return "표본 부족" if value is None else f"{value:.1%}"


def report(result: dict, *, days: int, since: datetime, read: list[str], broken: int) -> str:
    out = [
        "# KPI 질문 해결률 — 감사 기록 재집계",
        "",
        f"기간      최근 {days}일 ({since.date()} 이후, KST)",
        f"읽은 파일  {', '.join(read) if read else '없음'}",
        f"기록 전체  {result['logged']:,}건 (업무 아님 {result['nonBusiness']:,}건 제외)",
    ]
    if broken:
        out.append(f"읽지 못한 줄  {broken:,}건 — 분모에서 빠졌습니다. 그만큼 수치가 높게 나옵니다")
    out += [
        "",
        "## 전사",
        "",
        f"  질문(업무)      {result['asked']:,}",
        f"  해결(출처 있음) {result['resolved']:,}",
        f"  **해결률**      {percent(result['resolutionRate'])}",
        "",
        f"  참고 · 콘솔 `groundedRate` 방식  {percent(result['consoleGroundedRate'])}",
        "        (분자 `hits>0` · 분모에서 권한 미확인 제외 — **KPI 와 다른 수가 맞습니다**)",
    ]
    if result["unresolvedReasons"]:
        out += ["", "## 해결 안 된 이유", "", "| 이유 | 건수 |", "|---|---:|"]
        out += [f"| {name} | {n:,} |" for name, n in result["unresolvedReasons"]]
    if result["byWorkspace"]:
        out += [
            "",
            "## 워크스페이스별",
            "",
            "**전사 평균만 보면 죽은 워크스페이스가 안 보입니다.**",
            "",
            "| 워크스페이스 | 질문 | 해결 | 해결률 |",
            "|---|---:|---:|---:|",
        ]
        out += [
            f"| {key} | {v['asked']:,} | {v['resolved']:,} | {percent(v['rate'])} |"
            for key, v in result["byWorkspace"].items()
        ]
    out += [
        "",
        "---",
        "",
        f"표본이 {MIN_SAMPLE}건 미만이면 비율 대신 「표본 부족」 입니다. **0% 가 아닙니다.**",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90, help="최근 며칠 (기본 90)")
    ap.add_argument("--qa-log", default=os.getenv("QA_LOG_DIR", "./qa-log"), help="감사 기록 디렉터리")
    ap.add_argument("--json", metavar="경로", help="집계 결과를 JSON 으로도 쓴다")
    args = ap.parse_args()

    if args.days < 1:
        print("--days 는 1 이상이어야 합니다", file=sys.stderr)
        return 2

    since = datetime.now(KST) - timedelta(days=args.days)
    rows, read, broken = load(Path(args.qa_log), since)
    result = tally(rows)
    result["days"] = args.days
    result["since"] = since.isoformat(timespec="seconds")
    result["brokenLines"] = broken

    text = report(result, days=args.days, since=since, read=read, broken=broken)
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON: {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
