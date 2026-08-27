"""저장소 매니페스트와 콘솔 안내 화면의 매니페스트가 어긋나지 않게 고정한다.

같은 내용이 두 곳에 있다: `docs/pilot/slack-app-manifest.yaml` 과
`console-web/src/components/SetupGuide.tsx` 의 MANIFEST 상수.

한쪽만 고치면 **화면 안내를 보고 만든 앱에 스코프가 빠진다.** 그러면 봇이 오류 없이
반쪽만 동작한다(첨부만 누락, 특정 채널만 수집 등) — 가장 잡기 어려운 실패다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO_MANIFEST = ROOT / "docs" / "pilot" / "slack-app-manifest.yaml"
CONSOLE_GUIDE = ROOT / "console-web" / "src" / "components" / "SetupGuide.tsx"


def _console_manifest() -> str:
    text = CONSOLE_GUIDE.read_text(encoding="utf-8")
    return text.split("const MANIFEST = `", 1)[1].split("`", 1)[0]


def _list_items(text: str, section: str) -> list[str]:
    """`section:` 아래의 `- 항목` 들을 순서대로. 주석은 떼어낸다."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.split("#")[0].rstrip()
        if stripped.strip().endswith(section):
            inside = True
            continue
        if not inside:
            continue
        body = stripped.strip()
        if body.startswith("- ") and not body.startswith("- command"):
            out.append(body[2:].strip())
        elif body and not body.startswith("-"):
            inside = False
    return out


@pytest.mark.skipif(not CONSOLE_GUIDE.exists(), reason="콘솔 화면이 없는 배포본")
@pytest.mark.parametrize("section", ["bot:", "bot_events:"])
def test_scopes_and_events_match(section):
    repo = _list_items(REPO_MANIFEST.read_text(encoding="utf-8"), section)
    console = _list_items(_console_manifest(), section)
    assert repo, f"저장소 매니페스트에서 {section} 목록을 찾지 못했다"
    assert repo == console, (
        f"{section} 불일치 — 저장소에만 {sorted(set(repo) - set(console))}, "
        f"콘솔에만 {sorted(set(console) - set(repo))}. 두 곳을 함께 고쳐야 한다."
    )


@pytest.mark.skipif(not CONSOLE_GUIDE.exists(), reason="콘솔 화면이 없는 배포본")
@pytest.mark.parametrize(
    "key,value",
    [
        ("socket_mode_enabled", "true"),   # 인바운드 포트를 열지 않는다
        ("messages_tab_read_only_enabled", "false"),  # DM 입력창이 열려야 한다
        ("token_rotation_enabled", "false"),
    ],
)
def test_critical_settings_match(key, value):
    repo = REPO_MANIFEST.read_text(encoding="utf-8")
    console = _console_manifest()
    for name, text in (("저장소", repo), ("콘솔", console)):
        line = next((ln for ln in text.splitlines() if key in ln), None)
        assert line is not None, f"{name} 매니페스트에 {key} 가 없다"
        assert line.split("#")[0].strip().endswith(value), f"{name}: {key} 가 {value} 가 아니다"
