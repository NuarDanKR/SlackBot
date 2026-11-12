"""VIP 답변 편차 진단 스크립트 — 읽기만 하고, 근거 0건과 모델 판단을 가른다."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import diagnose_vip_answers as diag

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=KST)
Q = "태영건설에서 2026년 연말까지 반드시 달성해야 할 재무목표 Top5를 정리해 표로 설명해주세요"


def _write(root: Path, rows: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "qa-2026-11.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )


def _row(minutes, user, reason, docs):
    return {
        "ts": (NOW - timedelta(minutes=minutes)).isoformat(),
        "user": user, "question": Q, "intent_kind": reason,
        "evidence_refs": [{"document_path": d, "line_no": 1} for d in docs],
    }


def test_it_separates_empty_search_from_model_judgement(tmp_path):
    _write(tmp_path, [
        _row(30, "U0AAAA1111", "no_hits", []),
        _row(20, "U0AAAA1111", "answered", ["a.md"]),
        _row(10, "U0BBBB2222", "answered", ["a.md", "b.md"]),
    ])
    rows = diag.load(tmp_path, contains="재무목표", days=7, now=NOW)
    assert [r["refs"] for r in rows] == [0, 1, 2]
    assert all(r["user"].startswith("…") and len(r["user"]) == 5 for r in rows)
    summary = diag.summarize(rows)
    assert summary["reasons"] == {"no_hits": 1, "answered": 2}
    assert summary["docSets"] == 2
    assert any("근거 0건" in v for v in summary["verdict"])


def test_same_evidence_everywhere_points_at_the_model(tmp_path):
    _write(tmp_path, [_row(20, "U1", "answered", ["a.md"]), _row(10, "U2", "answered", ["a.md"])])
    summary = diag.summarize(diag.load(tmp_path, contains="재무 목표", days=7, now=NOW))
    assert summary["verdict"] == ["모든 회차가 같은 근거를 읽음 — 실패했다면 모델 판단 쪽"]


def test_old_records_and_other_questions_are_ignored(tmp_path):
    old = _row(60 * 24 * 30, "U1", "answered", ["a.md"])
    other = dict(_row(5, "U1", "answered", ["a.md"]), question="회의실 예약")
    _write(tmp_path, [old, other])
    assert diag.load(tmp_path, contains="재무목표", days=7, now=NOW) == []
