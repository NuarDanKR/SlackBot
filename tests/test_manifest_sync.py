"""매니페스트는 저장소 파일 **하나**가 진실이다.

예전에는 같은 내용이 두 곳에 있었다 — `docs/pilot/slack-app-manifest.yaml` 과
`console-web/src/components/SetupGuide.tsx` 의 MANIFEST 상수. 기능을 더할 때마다
둘을 함께 고쳐야 했고, 한쪽만 고치면 **화면 안내를 보고 만든 앱에 스코프가 빠졌다.**
그러면 봇이 오류 없이 반쪽만 동작한다(첨부만 누락, 특정 채널만 수집 등) —
가장 잡기 어려운 실패다.

이제 콘솔이 `GET /api/manifest` 로 파일을 직접 읽는다. 이 테스트는 그 계약과,
**사본이 다시 생기지 않는 것**을 고정한다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# 콘솔 백엔드는 선택 설치다(`pip install -e ".[console]"`). 봇만 도는 서버에서
# 이 파일 때문에 배포 게이트가 막히면, 정작 봇 배포가 콘솔 사정에 발이 묶인다.
pytest.importorskip("fastapi", reason="콘솔 의존성 미설치")

ROOT = Path(__file__).resolve().parent.parent

def _developer():
    """매니페스트 조회는 개발자·관리자만 볼 수 있다(스코프 구성이 드러난다).

    빈 `object()` 를 넘기면 권한 속성이 하나 늘 때마다 이 파일이 깨진다.
    실제 사용자 타입을 쓰면 권한 규칙이 바뀌어도 그대로 따라간다.
    """
    from tybot.console.auth import DEVELOPER, ConsoleUser

    return ConsoleUser(email="dev@taeyoung.com", name="개발자", role=DEVELOPER)
REPO_MANIFEST = ROOT / "docs" / "pilot" / "slack-app-manifest.yaml"
CONSOLE_SRC = ROOT / "console-web" / "src"


def test_repo_manifest_exists():
    assert REPO_MANIFEST.is_file(), f"매니페스트 원본이 없다: {REPO_MANIFEST}"


def test_api_serves_the_repo_file(monkeypatch, tmp_path):
    """서버가 읽는 경로가 저장소 파일과 같아야 한다."""
    from tybot.console.app import manifest_path

    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    assert manifest_path() == REPO_MANIFEST


def test_manifest_path_can_be_overridden(monkeypatch, tmp_path):
    from tybot.console.app import manifest_path

    monkeypatch.setenv("MANIFEST_PATH", str(tmp_path / "m.yaml"))
    assert manifest_path() == tmp_path / "m.yaml"


def test_endpoint_returns_content_and_checksum(monkeypatch, tmp_path):
    import hashlib

    from tybot.console.app import manifest

    body = "display_information:\n  name: TYBot\n"
    f = tmp_path / "m.yaml"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MANIFEST_PATH", str(f))

    out = manifest(user=_developer())
    assert out["content"] == body
    assert out["sha256"] == hashlib.sha256(body.encode()).hexdigest()[:12]
    assert out["updated_at"]


def test_missing_file_is_reported_not_silently_empty(monkeypatch, tmp_path):
    """조용히 빈 내용을 내려보내면 사람이 빈 매니페스트를 붙여넣게 된다."""
    from fastapi import HTTPException

    from tybot.console.app import manifest

    monkeypatch.setenv("MANIFEST_PATH", str(tmp_path / "없는파일.yaml"))
    with pytest.raises(HTTPException) as e:
        manifest(user=_developer())
    assert e.value.status_code == 503


@pytest.mark.skipif(not CONSOLE_SRC.is_dir(), reason="콘솔 소스 없음")
def test_no_manifest_copy_in_console_source():
    """화면 코드에 매니페스트 사본이 다시 생기면 같은 어긋남이 돌아온다."""
    offenders = []
    for path in CONSOLE_SRC.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        # 주석·설명이 아니라 '실제 매니페스트 본문'만 잡는다.
        if re.search(r"^\s*display_information:\s*$", text, re.M):
            offenders.append(path.relative_to(ROOT))
    assert not offenders, (
        f"매니페스트 사본이 있다: {offenders}. "
        "GET /api/manifest 로 저장소 파일을 받아 쓰세요."
    )


def test_manifest_has_the_scopes_the_bot_actually_needs():
    """봇 코드가 부르는 API 에 대응하는 스코프가 빠지면 조용한 고장이 된다."""
    text = REPO_MANIFEST.read_text(encoding="utf-8")
    for scope in (
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "channels:join",
        "files:read",
        "canvases:write",
        "chat:write",
        "commands",
        "reactions:read",
        "reactions:write",
    ):
        assert scope in text, f"스코프 누락: {scope}"
    for event in (
        "app_mention",
        "message.channels",
        "channel_created",
        "channel_rename",
        "reaction_added",
        "reaction_removed",
        "app_home_opened",
    ):
        assert event in text, f"이벤트 누락: {event}"
