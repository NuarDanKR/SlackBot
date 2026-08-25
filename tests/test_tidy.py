"""정리 잡 — 조용한 고장을 드러내되 원문은 절대 건드리지 않는다."""
from __future__ import annotations

import datetime as dt

import pytest

from tybot.tidy import inspect, prune_reports, write_report

TODAY = dt.date.today()


def _doc(channel="#팀_자금(ABB540)_주간보고", *, last=None, lines=None, extra_raw=""):
    last = last if last is not None else f"{TODAY}T09:00+09:00"
    body = lines if lines is not None else [f"> [{TODAY} 09:15] 홍길동: 기성금 3억 청구"]
    return (
        "---\n"
        "workspace: pilot\n"
        f'channel: "{channel}"\n'
        "visibility: private\n"
        f"acl: [{channel}]\n"
        "doc_count: 1\n"
        f"last_ingested: {last}\n"
        "---\n\n## 요약\n-\n\n## 원문 (자동 취합, 편집 금지)\n"
        + "\n".join(body)
        + ("\n" + extra_raw if extra_raw else "")
        + "\n"
    )


def _write(tmp_path, name="주간보고.md", text=None):
    p = tmp_path / "channels" / "pilot" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text is not None else _doc(), encoding="utf-8")
    return p


def test_healthy_archive_has_no_findings(tmp_path):
    _write(tmp_path)
    r = inspect(tmp_path)
    assert r.docs == 1 and r.raw_lines == 1
    assert r.findings == []
    assert "이상 없음" in r.to_markdown()


def test_schema_violation_is_an_error(tmp_path):
    _write(tmp_path, "깨진.md", "프론트매터 없음\n\n## 원문\n> [2026-08-25 09:00] a: b\n")
    r = inspect(tmp_path)
    assert len(r.errors) == 1
    assert "스키마 위반" in r.errors[0].detail


def test_stale_channel_is_warned(tmp_path):
    old = (TODAY - dt.timedelta(days=5)).isoformat() + "T09:00+09:00"
    _write(tmp_path, text=_doc(last=old))
    r = inspect(tmp_path)
    assert any("수집 없음" in f.detail for f in r.warns)


def test_recent_channel_is_not_warned(tmp_path):
    recent = (TODAY - dt.timedelta(days=1)).isoformat() + "T09:00+09:00"
    _write(tmp_path, text=_doc(last=recent))
    assert not any("수집 없음" in f.detail for f in inspect(tmp_path).warns)


def test_unparsable_last_ingested_is_warned(tmp_path):
    _write(tmp_path, text=_doc(last="언젠가"))
    assert any("last_ingested" in f.detail for f in inspect(tmp_path).warns)


def test_empty_raw_section_is_warned(tmp_path):
    _write(tmp_path, text=_doc(lines=[]))
    assert any("원문 0줄" in f.detail for f in inspect(tmp_path).warns)


def test_malformed_raw_line_is_an_error(tmp_path):
    """형식이 깨진 원문 줄은 검색에서 조용히 빠진다 - 오류로 잡아야 한다."""
    _write(tmp_path, text=_doc(extra_raw="> 형식이 깨진 줄"))
    r = inspect(tmp_path)
    assert any("파싱 안 되는" in f.detail for f in r.errors)


def test_duplicate_lines_are_warned(tmp_path):
    line = f"> [{TODAY} 09:15] 홍길동: 같은 발언"
    _write(tmp_path, text=_doc(lines=[line, line]))
    assert any("중복 원문" in f.detail for f in inspect(tmp_path).warns)


def test_inspect_never_modifies_originals(tmp_path):
    """가장 중요한 성질 - 점검은 읽기만 한다."""
    p = _write(tmp_path, text=_doc(extra_raw="> 형식이 깨진 줄"))
    before = p.read_bytes()
    inspect(tmp_path)
    assert p.read_bytes() == before


def test_report_is_written_outside_archive(tmp_path):
    """리포트가 아카이브 안에 들어가면 그게 다시 근거로 검색된다."""
    archive = tmp_path / "archive"
    reports = tmp_path / "reports"
    (archive / "channels" / "pilot").mkdir(parents=True)
    (archive / "channels" / "pilot" / "a.md").write_text(_doc(), encoding="utf-8")

    r = inspect(archive)
    path = write_report(r, reports)
    assert path is not None and reports in path.parents
    assert not list(archive.rglob("tidy-*.md"))

    from tybot.access import RequestContext
    from tybot.archive.store import ArchiveStore

    store = ArchiveStore(archive)
    ctx = RequestContext(workspace="pilot", role="exec")
    assert all("tidy" not in d.path.name for d in store.docs())
    assert store.search("점검 리포트", ctx) == []


def test_summary_line_for_journal(tmp_path):
    _write(tmp_path)
    line = inspect(tmp_path).summary_line()
    assert line.startswith("tidy docs=1") and "errors=0" in line


def test_prune_removes_old_reports_only(tmp_path):
    old = tmp_path / "tidy-2020-01-01.md"
    new = tmp_path / f"tidy-{TODAY}.md"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    assert prune_reports(tmp_path, keep_days=30) == 1
    assert new.exists() and not old.exists()


def test_missing_archive_dir_is_not_a_crash(tmp_path):
    r = inspect(tmp_path / "없음")
    assert r.docs == 0 and r.findings == []


@pytest.mark.parametrize("level", ["error", "warn"])
def test_markdown_groups_by_level(tmp_path, level):
    if level == "error":
        _write(tmp_path, "깨진.md", "프론트매터 없음\n\n## 원문\n> [2026-08-25 09:00] a: b\n")
        expect = "## 오류"
    else:
        _write(tmp_path, text=_doc(lines=[]))
        expect = "## 경고"
    assert expect in inspect(tmp_path).to_markdown()
