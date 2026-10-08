"""console-web 소스 위치를 한 곳에서 찾는다.

console-web 은 별도 저장소로 분리됐다. 화면 소스를 읽어 계약을 고정하는 시험
(`test_console_bot_ui_contract`, `test_pf_isolation`, `test_manifest_sync`)은
이제 소스가 **어디 있는지 모르는 상태**에서 돈다. 경로를 파일마다 따로 적으면
한쪽만 고쳐지고 나머지는 조용히 skip 된다 — 그래서 여기 하나만 둔다.

찾는 순서:
1. `CONSOLE_WEB_DIR` 환경변수 (서버 update.sh 가 넣는다)
2. `SlackBot/console-web` (분리 전 · 전환 기간)
3. `SlackBot/../console-web` (로컬에서 두 저장소를 나란히 둔 경우)

`REQUIRE_CONSOLE_WEB=1` 이면 못 찾았을 때 skip 이 아니라 **실패**한다.
배포 게이트가 「소스 없음」 으로 계약 시험을 통째로 건너뛰고 통과하면,
화면과 API 가 어긋난 채로 배포된다.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def console_web_dir() -> Path | None:
    candidates: list[Path] = []
    env = os.getenv("CONSOLE_WEB_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    candidates += [ROOT / "console-web", ROOT.parent / "console-web"]
    for c in candidates:
        if (c / "package.json").is_file():
            return c.resolve()
    return None


def console_src_or_skip() -> Path:
    """`console-web/src` 를 돌려준다. 없으면 skip, 필수면 실패."""
    found = console_web_dir()
    if found is not None:
        return found / "src"
    reason = (
        "console-web 소스를 찾지 못했습니다. "
        "CONSOLE_WEB_DIR 로 경로를 지정하거나 SlackBot 옆에 console-web 을 두세요."
    )
    if os.getenv("REQUIRE_CONSOLE_WEB") == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason, allow_module_level=True)
