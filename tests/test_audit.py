"""감사 기록 — 요청 1건이 JSONL·MD 양쪽에 남고, 아카이브를 오염시키지 않는지."""
from __future__ import annotations

import json

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore
from tybot.audit import QALog, QARecord


def _rec(**kw):
    base = dict(
        workspace="pilot",
        channel="#프로젝트-업데이트",
        channel_id="C123",
        user="U123",
        user_name="단라운",
        question="김해외동 기성금 얼마야?",
        intent_kind="search",
        intent_source="llm",
        reason="answered",
        hits=3,
        scope="채널 5개",
        citations=["#프로젝트-업데이트, 📄프로젝트-업데이트.md(2026-08-19)"],
        model="claude-haiku-4-5-20251001",
        cost_usd=0.00042,
        elapsed_ms=1234,
        answer="기성금은 3억 2천만원입니다.",
    )
    base.update(kw)
    return QARecord.build(**base)


def test_jsonl_and_md_written(tmp_path):
    log = QALog(tmp_path)
    log.write(_rec())

    jsonl = list(tmp_path.glob("qa-*.jsonl"))
    assert len(jsonl) == 1
    row = json.loads(jsonl[0].read_text(encoding="utf-8").strip())
    assert row["question"] == "김해외동 기성금 얼마야?"
    assert row["citations"] and row["model"] and row["elapsed_ms"] == 1234

    md = list(tmp_path.glob("20*.md"))
    assert len(md) == 1
    body = md[0].read_text(encoding="utf-8")
    assert "김해외동 기성금 얼마야?" in body
    assert "감사 기록" in body  # 아카이브가 아님을 파일 자체가 명시


def test_appends_not_overwrites(tmp_path):
    log = QALog(tmp_path)
    log.write(_rec(question="첫 질문"))
    log.write(_rec(question="둘째 질문"))
    jsonl = next(tmp_path.glob("qa-*.jsonl")).read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl) == 2
    body = next(tmp_path.glob("20*.md")).read_text(encoding="utf-8")
    assert body.count("감사 기록") == 1  # 헤더는 한 번만
    assert "첫 질문" in body and "둘째 질문" in body


def test_log_line_always_contains_question():
    """경로에 따라 로그가 달라지지 않는다 — 상태 질문도 질문 텍스트가 남는다."""
    for kind in ("status", "help", "summary", "search", "advice", "ingest"):
        line = _rec(intent_kind=kind, question="현재 너의 상태 알려줘").log_line()
        assert 'q="현재 너의 상태 알려줘"' in line
        assert f"intent={kind}/" in line


def test_scope_recorded_without_channel_names():
    """감사 로그 자체가 유출 경로가 되지 않게 권한범위는 개수만 남긴다."""
    line = _rec(scope="채널 5개").log_line()
    assert "채널 5개" in line


def test_md_is_not_read_as_archive(tmp_path):
    """감사 MD 가 아카이브 검색에 섞이면 요약 재귀가 된다 — 경로 분리를 고정한다."""
    archive = tmp_path / "archive"
    (archive / "channels" / "pilot").mkdir(parents=True)
    qa = tmp_path / "qa-log"
    QALog(qa).write(_rec(question="비밀 질문", answer="봇이 만든 답"))

    store = ArchiveStore(archive)
    ctx = RequestContext(workspace="pilot", role="exec")
    assert store.docs() == []
    assert store.search("비밀 질문", ctx) == []
    assert store.search("봇이 만든 답", ctx) == []


def test_write_failure_does_not_raise(tmp_path):
    """감사 기록 실패로 답변이 막히면 안 된다."""
    blocked = tmp_path / "file"
    blocked.write_text("not a dir", encoding="utf-8")
    QALog(blocked / "sub").write(_rec())  # 예외 없이 통과해야 한다


def test_long_text_is_clipped():
    rec = _rec(question="가" * 9000)
    assert len(rec.question) < 4200 and "총 9000자" in rec.question
