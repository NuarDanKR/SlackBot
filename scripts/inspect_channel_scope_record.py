"""Inspect one channel QA record without printing questions, answers, or file paths.

This only checks recorded reference coordinates, not every tool read or live ACL.
No model, Slack, DB, or archive calls are made. The audit file is never modified.
"""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _stamp(value: object) -> Decimal | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,8}", value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def inspect(path: Path, *, workspace: str, channel_id: str,
            request_ts: str | None = None) -> tuple[dict, int]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", workspace):
        raise ValueError("invalid workspace")
    if not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel_id):
        raise ValueError("channel questions only; DM scope needs current authorization")
    if request_ts is not None and _stamp(request_ts) is None:
        raise ValueError("invalid request timestamp")
    chosen = None
    chosen_stamp = Decimal(-1)
    duplicates = 0
    unreadable = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
            except ValueError:
                unreadable += 1
                continue
            if row.get("workspace") != workspace or row.get("channel_id") != channel_id:
                continue
            stamp = _stamp(row.get("request_ts"))
            if stamp is None:
                unreadable += 1
                continue
            if request_ts is not None and stamp != _stamp(request_ts):
                continue
            if stamp > chosen_stamp:
                chosen, chosen_stamp, duplicates = row, stamp, 1
            elif stamp == chosen_stamp:
                duplicates += 1

    result = {"read_only": True, "scope": "recorded-references-only",
              "workspace": workspace, "channel_id": channel_id,
              "rpc_verified": False, "all_tool_reads_verified": False,
              "unreadable_records": unreadable}
    if chosen is None:
        return {**result, "status": "record_not_found"}, 2
    result["request_ts"] = str(chosen["request_ts"])
    decision = chosen.get("decision_id")
    result["decision_id"] = decision if (
        isinstance(decision, str) and re.fullmatch(r"[a-f0-9]{16,64}", decision)
    ) else None
    reason = chosen.get("reason")
    result["reason"] = reason if reason in {
        "answered", "advice", "no_hits", "no_access", "channel_no_archive",
        "permission_unverified", "error", "specialist_unavailable",
        "evidence_insufficient", "clarify", "search_budget",
    } else "other"
    result["hermes_attempted"] = "hermes" in (chosen.get("attempted_specialists") or [])
    responder = chosen.get("final_responder")
    result["final_responder"] = responder if responder in {
        "hermes", "master-interim", "none",
    } else "other"
    invalid = 0
    total = 0
    mismatches = set()
    for field in ("evidence_refs", "attachment_refs"):
        refs = chosen.get(field, [])
        if not isinstance(refs, list):
            invalid += 1
            continue
        for ref in refs:
            if (not isinstance(ref, dict)
                    or not isinstance(ref.get("workspace"), str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", ref["workspace"])
                    or not isinstance(ref.get("channel_id"), str)
                    or not re.fullmatch(r"[CGD][A-Z0-9]{8,}", ref["channel_id"])):
                invalid += 1
                continue
            total += 1
            pair = (ref["workspace"], ref["channel_id"])
            if pair != (workspace, channel_id):
                mismatches.add(pair)
    result.update(reference_count=total, invalid_references=invalid,
                  outside_scopes=[{"workspace": ws, "channel_id": ch}
                                  for ws, ch in sorted(mismatches)])
    if mismatches:
        return {**result, "status": "outside_channel_reference"}, 1
    if unreadable or duplicates != 1 or invalid:
        return {**result, "status": "record_incomplete_or_ambiguous"}, 2
    if reason not in {"answered", "advice"}:
        return {**result, "status": "not_answered"}, 2
    if not total:
        return {**result, "status": "references_unavailable"}, 2
    return {**result, "status": "recorded_refs_in_channel"}, 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-file", required=True, type=Path)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--channel", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--request-ts")
    selection.add_argument("--latest", action="store_true")
    args = parser.parse_args(argv)
    try:
        result, code = inspect(args.qa_file, workspace=args.workspace,
                               channel_id=args.channel, request_ts=args.request_ts)
    except Exception as exc:  # noqa: BLE001 - no audit body or private exception text
        result, code = {"read_only": True, "status": "inspection_failed",
                        "error_type": type(exc).__name__}, 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
