"""답변 봇 보정 — 감사기록(record_id)이 정답이다."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fix_bot", ROOT / "scripts" / "fix_specialist_call_bot.py")
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)


def _log(directory: Path, ids: list[str]) -> Path:
    directory.mkdir(parents=True)
    (directory / "qa-2026-11.jsonl").write_text(
        "\n".join(json.dumps({"record_id": i, "question": "본문은 안 본다"}) for i in ids) + "\n\n{broken\n",
        encoding="utf-8")
    return directory


def test_record_ids_reads_only_ids(tmp_path):
    assert fix.record_ids(_log(tmp_path / "ty", ["a", "b"])) == {"a", "b"}
    assert fix.record_ids(tmp_path / "없음") == set()


def test_plan_trusts_the_audit_log_only():
    rows = [
        {"id": 1, "bot": "vipbot", "qa_record_id": "ty1"},   # 잘못 채운 TYBot 질문 → 바로잡음
        {"id": 2, "bot": "vipbot", "qa_record_id": "vip1"},  # 진짜 VIPBot → 그대로
        {"id": 3, "bot": "", "qa_record_id": "vip2"},        # 빈 값 → VIPBot 으로
        {"id": 4, "bot": "vipbot", "qa_record_id": "nowhere"},  # 모름 → 건드리지 않음
        {"id": 5, "bot": "", "qa_record_id": ""},
        {"id": 6, "bot": "tybot", "qa_record_id": "both"},   # 양쪽에 있음(이상) → 그대로
    ]
    changes = fix.plan(rows, {"ty1", "both"}, {"vip1", "vip2", "both"})
    assert changes == [(1, "vipbot", "tybot"), (3, "", "vipbot")]


def test_schema_no_longer_guesses_vipbot_from_the_model():
    schema = (ROOT / "deploy/sql/console_schema.sql").read_text(encoding="utf-8")
    assert "SET bot = 'vipbot'" not in schema
    assert "ADD COLUMN IF NOT EXISTS bot text" in schema
