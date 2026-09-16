"""답변 문제 후보와 피드백을 Markdown 한 장으로 묶는다.

## 왜 파일 한 장인가
콘솔 화면은 **서버의 운영 기록**을 읽는다. 개발 PC 저장소에는 그 원문이 없다 —
그래서 화면만 보고는 무엇이 왜 틀렸는지 코드 쪽에서 재현할 수 없다. 문제 후보를
질문·답변·출처·추적과 함께 한 장으로 묶어 건네면, 받은 쪽이 그 자리에서 분류하고
고칠 수 있다.

## 이 파일에 담기는 것
**사내 질문과 답변 원문, 사용자 피드백 원문이 들어간다.** 그래서
- 저장소에 커밋하지 않는다(`/qa-issues/` 는 `.gitignore` 에 있다)
- 공개 경로에 두지 않는다
- 받은 쪽은 원문을 코드·테스트·문서·커밋 메시지에 옮겨 적지 않는다

## 왜 `scripts/` 가 아니라 여기인가
CLI(`scripts/export_answer_issues.py`)와 콘솔 다운로드가 **같은 보고서**를 만들어야
한다. 두 벌로 두면 한쪽만 고쳐지고, 「내가 받은 파일과 서버가 만든 파일이 다르다」 가
된다 — 그때 어느 쪽이 맞는지 판정할 근거가 없다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Collection
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .feedback import event_id

KST = timezone(timedelta(hours=9))
SLOW_MS = 15_000
PROBLEM_FEEDBACK = {"negative", "missing", "correction"}


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
    return rows


# 권한 범위는 **행을 고르는 자리에서 한 번만** 정한다. 보고서를 다 만든 뒤 목록만
# 걸러내는 방식은 쓰지 않는다 — 분포 표와 「연결되지 않은 피드백」 같은 합계가 그대로
# 남아, 볼 수 없는 워크스페이스의 사용 양상이 숫자로 새어 나간다.
def _in_scope(
    row: dict, *, field: str, workspace: str, allowed: Collection[str] | None
) -> bool:
    key = str(row.get(field) or "")
    if workspace and key != workspace:
        return False
    return allowed is None or key in allowed


def load_records(
    root: Path,
    *,
    since: date,
    workspace: str = "",
    allowed: Collection[str] | None = None,
) -> list[dict]:
    rows = [row for path in sorted(root.glob("qa-*.jsonl")) for row in read_jsonl(path)]
    rows = [row for row in rows if str(row.get("ts") or "")[:10] >= since.isoformat()]
    rows = [
        row for row in rows
        if _in_scope(row, field="workspace", workspace=workspace, allowed=allowed)
    ]
    rows.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return rows


def load_feedback(
    root: Path,
    *,
    since: date,
    workspace: str = "",
    allowed: Collection[str] | None = None,
) -> list[dict]:
    rows = [
        row
        for path in sorted(root.glob("feedback-*.jsonl"))
        for row in read_jsonl(path)
    ]
    rows = [row for row in rows if str(row.get("at") or "")[:10] >= since.isoformat()]
    return [
        row for row in rows
        if _in_scope(row, field="workspace", workspace=workspace, allowed=allowed)
    ]


def active_feedback(rows: list[dict]) -> list[dict]:
    cancelled = {event_id(row) for row in rows if row.get("action") == "removed"}
    resolved = {
        str(row.get("target") or ""): row
        for row in rows
        if row.get("action") == "resolved" and row.get("target")
    }
    active: list[dict] = []
    for row in rows:
        if row.get("action") in {"removed", "resolved"}:
            continue
        eid = event_id(row)
        if eid in cancelled:
            continue
        item = dict(row)
        item["event_id"] = eid
        item["resolved"] = resolved.get(eid)
        active.append(item)
    return active


def feedback_by_record(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in active_feedback(rows):
        key = str(row.get("qa_record_id") or "")
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def issue_codes(row: dict, feedback: list[dict]) -> list[str]:
    codes: list[str] = []
    reason = str(row.get("reason") or "")
    error = str(row.get("error") or "").strip()
    if error or reason in {
        "error", "unavailable", "invalid-output", "timeout", "specialist_unavailable",
    }:
        codes.append("error")
    if int(row.get("hits") or 0) == 0 or reason in {"no_hits", "no_access"}:
        codes.append("no_evidence")
    if int(row.get("elapsed_ms") or 0) > SLOW_MS:
        codes.append("slow")
    traces = row.get("task_traces") or []
    if any(
        str(trace.get("status") or trace.get("result") or "").lower()
        in {
            "error", "failed", "fallback", "timeout", "unavailable",
            "invalid-output", "specialist_unavailable",
        }
        for trace in traces
        if isinstance(trace, dict)
    ):
        codes.append("specialist_failure")
    if any(str(item.get("kind") or "") in PROBLEM_FEEDBACK for item in feedback):
        codes.append("feedback")
    return codes


def _text(value: object, limit: int = 6000) -> str:
    text = str(value or "").strip().replace("```", "''' ")
    if len(text) > limit:
        return text[:limit].rstrip() + "\n...[truncated]"
    return text


def _block(value: object, *, empty: str = "(없음)") -> str:
    return f"```text\n{_text(value) or empty}\n```"


def render_report(
    records: list[dict],
    feedback_rows: list[dict],
    *,
    since: date,
    generated_at: datetime,
    workspace: str = "",
    limit: int = 200,
) -> str:
    grouped = feedback_by_record(feedback_rows)
    selected: list[tuple[dict, list[dict], list[str]]] = []
    for row in records:
        linked = grouped.get(str(row.get("record_id") or ""), [])
        codes = issue_codes(row, linked)
        if codes:
            selected.append((row, linked, codes))
    selected = selected[:limit]
    counts = Counter(code for _row, _feedback, codes in selected for code in codes)
    positive = sum(
        1 for row in active_feedback(feedback_rows) if row.get("kind") == "positive"
    )
    negative = sum(
        1 for row in active_feedback(feedback_rows) if row.get("kind") in PROBLEM_FEEDBACK
    )

    out = [
        "# TYBot 답변 문제 검토 패킷",
        "",
        "> 사내 질문·답변과 피드백이 포함된 운영 자료입니다. 저장소에 커밋하지 마세요.",
        "",
        f"- 생성 시각: {generated_at.isoformat(timespec='seconds')}",
        f"- 조회 시작일: {since.isoformat()}",
        f"- 워크스페이스: {workspace or '전체'}",
        f"- 조회 답변: {len(records)}건",
        f"- 문제 후보: {len(selected)}건 (표시 상한 {limit}건)",
        f"- 피드백: 긍정 {positive}건 / 문제·정정 {negative}건",
        "",
        "## 문제 분포",
        "",
        "| 분류 | 건수 |",
        "|---|---:|",
    ]
    labels = {
        "error": "처리 오류",
        "no_evidence": "근거 없음/권한 범위 없음",
        "slow": "15초 초과",
        "specialist_failure": "전문 봇 실패·폴백",
        "feedback": "사용자 문제·정정 피드백",
    }
    out.extend(f"| {labels[code]} | {counts.get(code, 0)} |" for code in labels)

    known_ids = {str(row.get("record_id") or "") for row in records}
    orphan = [
        row
        for row in active_feedback(feedback_rows)
        if row.get("kind") in PROBLEM_FEEDBACK
        and str(row.get("qa_record_id") or "") not in known_ids
    ]
    if orphan:
        out.extend([
            "",
            "## 연결되지 않은 피드백",
            "",
            f"QA 기록이 조회 기간에 없거나 ID가 없는 피드백 {len(orphan)}건입니다.",
        ])
        for row in orphan[:50]:
            out.append(
                f"- {row.get('at', '-')} · {row.get('workspace', '-')} · "
                f"{row.get('kind', '-')} · QA `{row.get('qa_record_id') or '-'}` · "
                f"{_text(row.get('text'), 500) or '(내용 없음)'}"
            )

    out.extend(["", "## 문제 후보 상세"])
    for index, (row, linked, codes) in enumerate(selected, start=1):
        out.extend([
            "",
            f"### {index}. {row.get('ts') or '-'} · {row.get('workspace') or '-'} "
            f"· {row.get('channel') or row.get('channel_id') or '-'}",
            "",
            f"- QA ID: `{row.get('record_id') or '-'}`",
            f"- 분류: {', '.join(codes)}",
            f"- 의도: `{row.get('intent_kind') or '-'}` / 출처: `{row.get('intent_source') or '-'}`",
            f"- 결과: `{row.get('reason') or '-'}` · 근거 {int(row.get('hits') or 0)}건 "
            f"· {int(row.get('elapsed_ms') or 0)}ms",
            f"- 모델: `{row.get('model') or '-'}` · 전문 봇 시도: "
            f"`{row.get('specialists_attempted') or row.get('attempted_specialists') or '-'}`",
            f"- 오류: `{_text(row.get('error'), 1000) or '-'}`",
            "",
            "**질문**",
            _block(row.get("question")),
            "",
            "**실제 답변**",
            _block(row.get("answer")),
            "",
            "**출처**",
            _block("\n".join(str(item) for item in (row.get("citations") or []))),
        ])
        if row.get("task_traces"):
            out.extend([
                "",
                "**라우팅·전문 봇 추적**",
                _block(json.dumps(row.get("task_traces"), ensure_ascii=False, indent=2)),
            ])
        if linked:
            out.extend(["", "**연결된 피드백**"])
            for item in linked:
                resolved = item.get("resolved") or {}
                status = "처리됨" if resolved else "미처리"
                out.append(
                    f"- {item.get('at') or '-'} · {item.get('kind') or '-'} · {status}: "
                    f"{_text(item.get('text'), 1000) or '(내용 없음)'}"
                )
                if resolved:
                    out.append(
                        f"  처리 메모: {_text(resolved.get('text'), 1000) or '(없음)'}"
                    )
    out.extend([
        "",
        "## Claude 작업 규칙",
        "",
        "1. 각 문제를 수집·권한·검색·분류·전문 봇·렌더링·운영 설정 중 하나로 분류합니다.",
        "2. 질문이나 답변 원문을 코드·테스트 픽스처·문서에 그대로 복사하지 않습니다.",
        "3. 같은 원인의 반복 건을 묶고 재현 가능한 최소 테스트를 먼저 추가합니다.",
        "4. 피드백을 해결한 뒤 콘솔 처리 메모에 커밋과 검증 결과를 남깁니다.",
        "",
    ])
    return "\n".join(out)


def build_report(
    qa_log_dir: Path | str,
    *,
    days: int = 7,
    workspace: str = "",
    limit: int = 200,
    now: datetime | None = None,
    allowed: Collection[str] | None = None,
) -> str:
    """한 번에 보고서를 만든다. CLI 와 콘솔이 같이 쓰는 입구다.

    `allowed` 를 주면 그 워크스페이스의 기록만으로 **보고서 전체를** 만든다.
    콘솔에서 범위가 좁혀진 관리자가 눌렀을 때 쓴다.
    """
    if days < 1 or limit < 1:
        raise ValueError("days 와 limit 은 1 이상이어야 합니다.")
    generated_at = now or datetime.now(KST)
    since = generated_at.date() - timedelta(days=days - 1)
    root = Path(qa_log_dir).expanduser()
    return render_report(
        load_records(root, since=since, workspace=workspace, allowed=allowed),
        load_feedback(root, since=since, workspace=workspace, allowed=allowed),
        since=since,
        generated_at=generated_at,
        workspace=workspace,
        limit=limit,
    )


def report_filename(*, generated_at: datetime | None = None, workspace: str = "") -> str:
    """받는 쪽이 파일만 보고 범위를 알 수 있게 짓는다.

    같은 날 워크스페이스를 바꿔 두 번 받으면 이름이 갈려야 한다 — 같으면 브라우저가
    `(1)` 을 붙이고, 그러면 **어느 쪽이 무엇인지 파일명으로는 알 수 없다.**
    """
    day = (generated_at or datetime.now(KST)).date().isoformat()
    scope = workspace.strip().lower() or "all"
    return f"answer-issues-{day}-{scope}.md"
