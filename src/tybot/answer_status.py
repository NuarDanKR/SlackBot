"""Classify QA records independently from Slack delivery success."""
from __future__ import annotations

FAILED_REASONS = {
    "error",
    "invalid-output",
    "specialist_unavailable",
    "timeout",
    "unavailable",
}
FAILED_TRACE_RESULTS = {
    "error",
    "failed",
    "invalid-output",
    "registry_error",
    "specialist_unavailable",
    "timeout",
    "unavailable",
}


def trace_failed(trace: object) -> bool:
    if not isinstance(trace, dict):
        return False
    result = str(trace.get("status") or trace.get("result") or "").lower()
    error_code = str(trace.get("error_code") or "").strip()
    responder = str(trace.get("final_responder") or "").strip()
    return result in FAILED_TRACE_RESULTS or bool(error_code and responder in {"", "none"})


def answer_failed(row: dict) -> bool:
    if str(row.get("error") or "").strip():
        return True
    if str(row.get("reason") or "") in FAILED_REASONS:
        return True
    responder = str(row.get("final_responder") or "").strip()
    if str(row.get("specialist_error_code") or "").strip() and responder in {"", "none"}:
        return True
    return any(trace_failed(trace) for trace in (row.get("task_traces") or []))


def failure_code(row: dict) -> str:
    if not answer_failed(row):
        return ""
    code = str(row.get("specialist_error_code") or "").strip()
    if code:
        return code
    error = str(row.get("error") or "").strip()
    if error:
        return error
    for trace in row.get("task_traces") or []:
        if trace_failed(trace):
            return str(trace.get("error_code") or trace.get("result") or "answer_failed")
    return str(row.get("reason") or "answer_failed")
