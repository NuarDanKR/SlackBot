from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

from tybot.answer_issues import KST


def load_script():
    path = Path(__file__).parents[1] / "scripts" / "export_hybrid_search_baseline.py"
    spec = importlib.util.spec_from_file_location("export_hybrid_search_baseline", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_export_contains_counts_but_no_questions_answers_ids_or_sources(tmp_path):
    secret_question = "secret question about project alpha"
    secret_answer = "secret answer from private source"
    secret_id = "record-private-123"
    write_jsonl(
        tmp_path / "qa-2026-11.jsonl",
        [
            {
                "ts": "2026-11-04T10:00:00+09:00",
                "workspace": "sample",
                "record_id": secret_id,
                "question": secret_question,
                "answer": secret_answer,
                "reason": "no_hits",
                "hits": 0,
                "citations": ["#private / secret.md"],
            },
            {
                "ts": "2026-11-04T11:00:00+09:00",
                "workspace": "sample",
                "record_id": "grounded-id",
                "question": "another secret",
                "answer": "another answer",
                "reason": "ok",
                "hits": 2,
                "citations": ["#private / source.md"],
            },
        ],
    )
    module = load_script()
    payload = module.build_baseline(
        tmp_path,
        since=date(2026, 11, 1),
        workspace="sample",
        generated_at=datetime(2026, 11, 4, 12, tzinfo=KST),
    )
    rendered = json.dumps(payload, ensure_ascii=False)
    assert payload["records"] == 2
    assert payload["states"] == {"grounded_sample": 1, "query_miss": 1}
    assert payload["eligibleTotal"] == 2
    for secret in (
        secret_question,
        secret_answer,
        secret_id,
        "grounded-id",
        "#private",
        "secret.md",
    ):
        assert secret not in rendered


def test_workspace_filter_is_applied_before_aggregation(tmp_path):
    write_jsonl(
        tmp_path / "qa-2026-11.jsonl",
        [
            {
                "ts": "2026-11-04T10:00:00+09:00",
                "workspace": "wanted",
                "reason": "no_hits",
                "hits": 0,
            },
            {
                "ts": "2026-11-04T10:00:00+09:00",
                "workspace": "other",
                "reason": "permission_unverified",
                "hits": 0,
            },
        ],
    )
    payload = load_script().build_baseline(
        tmp_path,
        since=date(2026, 11, 1),
        workspace="wanted",
        generated_at=datetime(2026, 11, 4, 12, tzinfo=KST),
    )
    assert payload["records"] == 1
    assert payload["states"] == {"query_miss": 1}


def test_unknown_no_evidence_is_reported_but_not_eligible(tmp_path):
    write_jsonl(
        tmp_path / "qa-2026-11.jsonl",
        [
            {
                "ts": "2026-11-04T10:00:00+09:00",
                "workspace": "sample",
                "reason": "",
                "hits": 0,
            }
        ],
    )
    payload = load_script().build_baseline(
        tmp_path,
        since=date(2026, 11, 1),
        generated_at=datetime(2026, 11, 4, 12, tzinfo=KST),
    )
    assert payload["unknownNoEvidence"] == 1
    assert payload["eligibleTotal"] == 0
