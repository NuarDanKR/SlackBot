"""수집 진단 — 채널을 식별할 수 없는 원문 (B-61).

오염을 막은 뒤에도 조치가 남는다. 표시명을 못 읽는 문서는 이제 **어느 채널에도
붙지 않는다** — 다른 채널의 근거가 되는 것보다 낫지만, 그 내용이 답변에서 빠진다는
뜻이기도 하다. 운영자가 그 목록을 볼 수 없으면 조치 자체가 시작되지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import diagnose_collection


def _doc(root: Path, relative: str, *, channel: str, channel_id: str | None,
         version: int, line: str, quote: bool = True) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [
        "---",
        "workspace: tyit",
        f'channel: "{channel}"' if quote else f"channel: {channel}",
        "visibility: private",
        "acl:",
        "  - tyit",
        f"schema_version: {version}",
    ]
    if channel_id:
        head.append(f"channel_id: {channel_id}")
    if version == 2:
        head.append("source_date: 2026-09-17")
    head += ["---", "", "## 원문", "", f"> [2026-09-17 09:00] 홍길동: {line}", ""]
    path.write_text("\n".join(head) + "\n", encoding="utf-8")


def _run(tmp_path, capsys) -> str:
    diagnose_collection.main(["--archive", str(tmp_path)])
    return capsys.readouterr().out


def test_a_document_with_an_unreadable_channel_name_is_listed(tmp_path, capsys):
    """`channel: #이름` 에 따옴표가 없으면 표시명이 통째로 잘린다. 이게 2026-09-17
    오염의 원인이었고, 고치기 전까지 그 원문은 답변에서 빠져 있다."""
    _doc(tmp_path, "workspaces/tyit/channels/C_REPORT/raw/2026-09-17.md",
         channel="#팀-전산_abb155-전산팀장보고", channel_id="C_REPORT", version=2,
         line="정상 문서의 줄")
    _doc(tmp_path, "channels/broken/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id=None, version=1,
         line="표시명을 읽지 못한 줄", quote=False)

    out = _run(tmp_path, capsys)

    assert "채널을 식별할 수 없는 원문" in out
    assert "broken" in out
    assert "따옴표" in out
    # 조치 순서를 말한다 — 고치면 그 근거가 갑자기 요약에 나타난다.
    assert "검토자에게" in out


def test_a_legacy_name_with_no_matching_channel_is_listed_separately(tmp_path, capsys):
    """채널명이 바뀐 뒤 옛 이름만 남은 경우다. 조치가 다르므로 문장도 다르다."""
    _doc(tmp_path, "workspaces/tyit/channels/C_REPORT/raw/2026-09-17.md",
         channel="#팀-전산_abb155-전산팀장보고", channel_id="C_REPORT", version=2,
         line="정상 문서의 줄")
    _doc(tmp_path, "channels/renamed/2026-09-17.md",
         channel="#옛-이름", channel_id=None, version=1, line="옛 이름으로 남은 줄")

    out = _run(tmp_path, capsys)

    assert "renamed" in out
    assert "채널명이 바뀐 뒤" in out


def test_a_legacy_name_that_matches_a_real_channel_is_not_listed(tmp_path, capsys):
    """마이그레이션 별칭이 정상 동작하는 경우다. 이것까지 경고하면 진짜 문제가 묻힌다."""
    _doc(tmp_path, "workspaces/tyit/channels/C_NOTICE/raw/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id="C_NOTICE", version=2,
         line="v2 문서의 줄")
    _doc(tmp_path, "channels/notice/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id=None, version=1,
         line="같은 채널의 v1 줄")

    out = _run(tmp_path, capsys)

    assert "없다. 모든 원문 문서가 채널에 귀속된다." in out


def test_a_clean_archive_says_so(tmp_path, capsys):
    _doc(tmp_path, "workspaces/tyit/channels/C1/raw/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id="C1", version=2, line="줄")

    out = _run(tmp_path, capsys)

    assert "없다. 모든 원문 문서가 채널에 귀속된다." in out


def test_archive_default_is_loaded_from_tybot_env_file(tmp_path, capsys, monkeypatch):
    """운영 명령은 TYBOT_ENV_FILE만 넘긴다. ARCHIVE_DIR 기본값을 환경 파일보다
    먼저 계산하면 실행 디렉터리의 ``./archive``를 열어 권한 오류가 난다."""
    archive = tmp_path / "service-archive"
    _doc(archive, "workspaces/tyit/channels/C1/raw/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id="C1", version=2,
         line="환경 파일에서 찾은 원문")
    env_file = tmp_path / "tybot.env"
    env_file.write_text(f"ARCHIVE_DIR={archive}\n", encoding="utf-8")

    monkeypatch.delenv("ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("TYBOT_ENV_FILE", str(env_file))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(diagnose_collection, "_load_workspaces", lambda: ([], "test"))

    result = diagnose_collection.main([])
    out = capsys.readouterr().out

    assert result == 0
    assert "tyit" in out
    assert "환경 파일에서 찾은 원문" not in out
