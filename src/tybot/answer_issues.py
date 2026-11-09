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
import re
import sys
from collections import Counter
from collections.abc import Collection
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .answer_status import answer_failed, failure_code
from .feedback import event_id
from .intent import followup_hint

KST = timezone(timedelta(hours=9))
SLOW_MS = 15_000
HIGH_SEARCH_COUNT = 20
SUCCESS_SAMPLE_LIMIT = 20
PROBLEM_FEEDBACK = {"negative", "missing", "correction"}

_SEARCH_COUNT_RE = re.compile(r"검색\s*([0-9][0-9,]*)\s*건")
_PARTIAL_SCOPE_MARKERS = (
    "※ 확인하지 못한 범위:",
    "범위 상한(",
    "이번 답변에서 제외:",
)
_RESOURCE_ERROR_MARKERS = (
    "oom",
    "out_of_memory",
    "out-of-memory",
    "out of memory",
    "memoryerror",
    "resource_exhausted",
)
_ELLIPSIS_REFERENCE_RE = re.compile(
    r"^\s*(?:그럼|그러면|그건|그거|그\s*(?:문서|자료|내용|부분)|"
    r"위\s*(?:문서|자료|내용)|앞(?:서|의)?\s*(?:문서|자료|내용))\b"
)
_FOLLOWUP_RESOLUTIONS = {"prior_turn", "prior_topic", "prior_attachments"}
_SCOPE_MISS_RESOLUTIONS = {"scoped_empty", "widened_after_scope_miss"}


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


def recorded_search_count(row: dict) -> int | None:
    """Return the largest search count that the QA record actually exposes.

    Older records only have the human-readable ``scope`` value. Newer or test
    records may carry a numeric field. Missing is unknown, not zero.
    """
    counts: list[int] = []
    for key in ("search_calls", "search_count", "searches"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts.append(value)
    match = _SEARCH_COUNT_RE.search(str(row.get("scope") or ""))
    if match:
        counts.append(int(match.group(1).replace(",", "")))
    for trace in row.get("task_traces") or []:
        if not isinstance(trace, dict):
            continue
        for key in ("search_calls", "search_count", "searches"):
            value = trace.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts.append(value)
    return max(counts) if counts else None


def _partial_scope(row: dict) -> bool:
    answer = str(row.get("answer") or "")
    if any(marker in answer for marker in _PARTIAL_SCOPE_MARKERS):
        return True
    if str(row.get("reason") or "") == "search_budget":
        return True
    if str(row.get("specialist_error_code") or "") == "search-budget-exhausted":
        return True
    return any(
        isinstance(trace, dict)
        and (
            str(trace.get("status") or trace.get("result") or "").lower()
            == "partial"
            or bool(str(trace.get("coverage_note") or "").strip())
        )
        for trace in (row.get("task_traces") or [])
    )


def _resource_exhausted(row: dict) -> bool:
    values = [
        failure_code(row),
        str(row.get("error") or ""),
        str(row.get("specialist_error_code") or ""),
    ]
    values.extend(
        str(trace.get("error_code") or trace.get("result") or "")
        for trace in (row.get("task_traces") or [])
        if isinstance(trace, dict)
    )
    joined = " ".join(values).casefold()
    return any(marker in joined for marker in _RESOURCE_ERROR_MARKERS)


def issue_codes(row: dict, feedback: list[dict]) -> list[str]:
    codes: list[str] = []
    reason = str(row.get("reason") or "")
    if answer_failed(row):
        codes.append("error")
    code = failure_code(row).casefold()
    if "timeout" in code:
        codes.append("timeout")
    if _resource_exhausted(row):
        codes.append("resource_exhaustion")
    if (
        "answer" in row
        and not str(row.get("answer") or "").strip()
        and not str(row.get("artifact_operation") or "").strip()
    ):
        codes.append("empty_answer")
    if "citations" in row and not (row.get("citations") or []):
        codes.append("missing_citations")
    if reason == "permission_unverified":
        # **자료 없음으로 세지 않는다.** 권한 조회가 실패한 것이라 사람이 할 일이
        # 다르고, 자료 없음에 섞으면 운영 지표에서 영원히 안 보인다.
        codes.append("permission_unverified")
    elif int(row.get("hits") or 0) == 0 or reason in {
        "no_hits", "no_access", "channel_no_archive",
    }:
        codes.append("no_evidence")
    if int(row.get("elapsed_ms") or 0) > SLOW_MS:
        codes.append("slow")
    searches = recorded_search_count(row)
    if searches is not None and searches > HIGH_SEARCH_COUNT:
        codes.append("high_search_volume")
    if _partial_scope(row):
        codes.append("partial_scope")
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
    resolution = str(row.get("context_resolution") or "none")
    reference_mode, _attachments = followup_hint(str(row.get("question") or ""))
    if reference_mode != "none" and resolution == "none":
        codes.append("context_reference_missed")
    if resolution in _FOLLOWUP_RESOLUTIONS and not _strings(row.get("context_parent_ids")):
        codes.append("context_lineage_missing")
    if resolution in _SCOPE_MISS_RESOLUTIONS:
        codes.append("context_scope_miss")
    return codes


def _strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _conversation_key(row: dict) -> tuple[str, ...] | None:
    workspace = str(row.get("workspace") or "").strip()
    channel_id = str(row.get("channel_id") or "").strip()
    thread_ts = str(row.get("thread_ts") or "").strip()
    user = str(row.get("user") or "").strip()
    if workspace and channel_id and thread_ts:
        return ("thread", workspace, channel_id, thread_ts)
    if workspace and channel_id.startswith("D") and user:
        return ("dm", workspace, channel_id, user)
    return None


def probable_ellipsis_context_misses(records: list[dict]) -> set[str]:
    """Find successful-looking turns that probably lost an omitted subject.

    This is deliberately a review candidate, not a production classifier. It only
    fires when an earlier QA turn exists in the same DM or Slack thread. The report
    can therefore expose phrases such as ``그럼 그 내용은?`` without changing what
    the bot searches or allowing a previous answer to become evidence.
    """
    latest: dict[tuple[str, ...], dict] = {}
    misses: set[str] = set()
    for row in sorted(records, key=lambda item: str(item.get("ts") or "")):
        key = _conversation_key(row)
        if key is None:
            continue
        question = str(row.get("question") or "")
        resolution = str(row.get("context_resolution") or "none")
        record_id = str(row.get("record_id") or "")
        if (
            key in latest
            and record_id
            and resolution == "none"
            and _ELLIPSIS_REFERENCE_RE.search(question)
        ):
            misses.add(record_id)
        latest[key] = row
    return misses


def _text(value: object, limit: int = 6000) -> str:
    text = str(value or "").strip().replace("```", "''' ")
    if len(text) > limit:
        return text[:limit].rstrip() + "\n...[truncated]"
    return text


def _block(value: object, *, empty: str = "(없음)") -> str:
    return f"```text\n{_text(value) or empty}\n```"


def _coordinates_block(row: dict) -> str:
    coordinates = {
        "evidence_refs": row.get("evidence_refs") or [],
        "attachment_refs": row.get("attachment_refs") or [],
    }
    return _block(json.dumps(coordinates, ensure_ascii=False, indent=2))


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
    ellipsis_misses = probable_ellipsis_context_misses(records)
    candidates: list[tuple[dict, list[dict], list[str]]] = []
    for row in records:
        linked = grouped.get(str(row.get("record_id") or ""), [])
        codes = issue_codes(row, linked)
        if str(row.get("record_id") or "") in ellipsis_misses:
            codes.append("possible_context_miss")
        if codes:
            candidates.append((row, linked, codes))
    selected = candidates[:limit]
    counts = Counter(code for _row, _feedback, codes in candidates for code in codes)
    grounded_successes = [
        row
        for row in records
        if not answer_failed(row)
        and int(row.get("hits") or 0) > 0
        and bool(str(row.get("answer") or "").strip())
        and bool(row.get("citations") or [])
    ]
    success_sample = grounded_successes[:SUCCESS_SAMPLE_LIMIT]
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
        f"- 문제 후보: {len(candidates)}건 (상세 표시 {len(selected)}건 · 상한 {limit}건)",
        f"- 근거 있는 성공 답변: {len(grounded_successes)}건 "
        f"(표본 {len(success_sample)}건 · 상한 {SUCCESS_SAMPLE_LIMIT}건)",
        f"- 피드백: 긍정 {positive}건 / 문제·정정 {negative}건",
        "",
        "## 문제 분포",
        "",
        "| 분류 | 건수 |",
        "|---|---:|",
    ]
    labels = {
        "error": "처리 오류",
        "timeout": "타임아웃",
        "resource_exhaustion": "기록된 메모리·자원 고갈",
        "empty_answer": "빈 답변",
        "missing_citations": "출처 없음",
        "no_evidence": "근거 없음/권한 범위 없음",
        "permission_unverified": "권한 범위 확인 실패",
        "slow": "15초 초과",
        "high_search_volume": f"검색 {HIGH_SEARCH_COUNT}건 초과",
        "partial_scope": "일부 범위 미확인·상한 제외",
        "specialist_failure": "전문 봇 실패·폴백",
        "feedback": "사용자 문제·정정 피드백",
        "context_reference_missed": "명시적 이전 맥락 지칭 미해결",
        "context_lineage_missing": "후속 맥락 부모 QA 누락",
        "context_scope_miss": "이전 근거 좌표 재개방 실패",
        "possible_context_miss": "생략형 이전 맥락 누락 후보",
    }
    out.extend(f"| {labels[code]} | {counts.get(code, 0)} |" for code in labels)
    out.extend([
        "",
        "> OOM으로 프로세스가 QA 기록을 쓰기 전에 종료되면 이 파일만으로는 잡히지 않습니다. "
        "서비스 재시작·커널 OOM 기록과 별도로 대조해야 합니다.",
        "> `no_evidence`에는 수집·권한·검색어 불일치가 섞일 수 있습니다. "
        "그 수를 곧바로 벡터 검색이 해결할 건수로 해석하지 않습니다.",
    ])

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
            f"- 기록된 검색: {recorded_search_count(row) if recorded_search_count(row) is not None else '모름'}건",
            f"- 모델: `{row.get('model') or '-'}` · 전문 봇 시도: "
            f"`{row.get('specialists_attempted') or row.get('attempted_specialists') or '-'}`",
            f"- 오류: `{_text(row.get('error'), 1000) or '-'}`",
            f"- 맥락: `{row.get('context_resolution') or 'none'}` · 부모 QA: "
            f"`{', '.join(_strings(row.get('context_parent_ids'))) or '-'}`",
            "",
            "**질문**",
            _block(row.get("question")),
            "",
            "**실제 답변**",
            _block(row.get("answer")),
            "",
            "**출처**",
            _block("\n".join(str(item) for item in (row.get("citations") or []))),
            "",
            "**근거 좌표 (현재 권한으로 다시 열어 검증)**",
            _coordinates_block(row),
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
        "## 성공 답변 근거 충실도 표본",
        "",
        f"> 근거와 출처가 기록된 성공 답변 중 최신 {len(success_sample)}건입니다. "
        "문장 정확성은 자동 판정하지 않고 사람이 원문 좌표와 대조합니다.",
    ])
    for index, row in enumerate(success_sample, start=1):
        out.extend([
            "",
            f"### S{index}. {row.get('ts') or '-'} · {row.get('workspace') or '-'} "
            f"· QA `{row.get('record_id') or '-'}`",
            "",
            "**질문**",
            _block(row.get("question")),
            "",
            "**실제 답변**",
            _block(row.get("answer")),
            "",
            "**출처**",
            _block("\n".join(str(item) for item in (row.get("citations") or []))),
            "",
            "**근거 좌표 (현재 권한으로 다시 열어 검증)**",
            _coordinates_block(row),
        ])
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
