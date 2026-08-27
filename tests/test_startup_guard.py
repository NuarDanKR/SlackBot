"""아카이브에 못 쓰면 기동을 막는다 — '답은 하는데 원문을 버리는' 상태 방지.

실제 사고: `tybot.env` 에 `ARCHIVE_DIR` 이 빠져 기본값 `./archive` 로 떨어졌고,
운영 유닛은 코드 경로가 읽기 전용이라 봇이 Slack 에 붙어 답변까지 하면서
원문·감사기록을 한 줄도 저장하지 못했다. 에러 로그만 남고 아무도 몰랐다.
"""
from __future__ import annotations

import pytest

from tybot.slack.pilot import check_paths, enforce_archive_writable
from tybot.workspaces import ConfigError


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("ALLOW_READONLY_ARCHIVE", raising=False)
    yield


def _blocked(tmp_path) -> str:
    """디렉터리를 만들 수 없는 경로 — 부모가 일반 파일이다."""
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    return str(f / "archive")


def test_unwritable_archive_aborts_startup(tmp_path):
    problems = check_paths(_blocked(tmp_path), str(tmp_path / "qa-log"))
    assert "아카이브" in problems
    with pytest.raises(ConfigError, match="아카이브에 쓸 수 없어 기동하지 않습니다"):
        enforce_archive_writable(problems)


def test_error_names_the_setting_to_fix(tmp_path):
    problems = check_paths(_blocked(tmp_path), str(tmp_path / "qa-log"))
    with pytest.raises(ConfigError) as e:
        enforce_archive_writable(problems)
    assert "ARCHIVE_DIR" in str(e.value)
    assert "ALLOW_READONLY_ARCHIVE" in str(e.value)  # 조회 전용 탈출구를 알려준다


def test_readonly_override_allows_startup(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ALLOW_READONLY_ARCHIVE", "1")
    problems = check_paths(_blocked(tmp_path), str(tmp_path / "qa-log"))
    enforce_archive_writable(problems)  # 예외 없음
    assert any("ALLOW_READONLY_ARCHIVE=1" in r.message for r in caplog.records)


def test_writable_archive_passes(tmp_path):
    problems = check_paths(str(tmp_path / "archive"), str(tmp_path / "qa-log"))
    assert problems == {}
    enforce_archive_writable(problems)


def test_unwritable_qa_log_only_warns(tmp_path):
    """감사기록이 막혀도 원문 자산은 사라지지 않는다 — 기동은 시킨다."""
    problems = check_paths(str(tmp_path / "archive"), _blocked(tmp_path))
    assert "감사기록" in problems
    assert "아카이브" not in problems
    enforce_archive_writable(problems)  # 막지 않는다


def test_relative_default_archive_is_caught_when_unwritable(tmp_path, monkeypatch):
    """운영 사고 재현 — ARCHIVE_DIR 미설정 시 기본값이 읽기 전용 경로를 가리킨다."""
    monkeypatch.chdir(tmp_path)
    blocked = _blocked(tmp_path)
    problems = check_paths(blocked, str(tmp_path / "qa-log"))
    with pytest.raises(ConfigError):
        enforce_archive_writable(problems)
