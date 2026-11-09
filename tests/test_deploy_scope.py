"""콘솔 배포 / 봇 배포 — 범위 판정과 배포 스크립트의 약속."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deploy_scope", ROOT / "scripts" / "deploy_scope.py")
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def tree(tmp_path):
    return _tree(tmp_path, {
        "src/tybot/__init__.py": "",
        "src/tybot/bot.py": "from .console.shared import x\nfrom .console import audit\n",
        "src/tybot/console/__init__.py": "",
        "src/tybot/console/shared.py": "from .helper import y\nx = 1\n",
        "src/tybot/console/helper.py": "y = 2\n",
        "src/tybot/console/audit.py": "",
        "src/tybot/console/app.py": "from . import shared, only\n",
        "src/tybot/console/only.py": "from tybot.console.helper import y\n",
    })


def test_bot_used_modules_follow_imports_transitively(tree):
    used = scope.bot_used_console_modules(tree)
    assert used == {"tybot.console", "tybot.console.shared", "tybot.console.helper",
                    "tybot.console.audit"}


@pytest.mark.parametrize(("changed", "expected"), [
    (["src/tybot/console/only.py", "src/tybot/console/app.py"], "console"),
    (["src/tybot/console/only.py", "tests/test_x.py", "docs/a.md", "deploy/sql/x.sql"], "console"),
    (["deploy/tybot-console.service", "deploy/requirements-console.txt"], "console"),
    (["src/tybot/console/helper.py"], "bot"),            # 봇이 간접으로 쓴다
    (["src/tybot/console/__init__.py"], "bot"),
    (["src/tybot/console/only.py", "src/tybot/bot.py"], "bot"),
    (["deploy/update.sh"], "bot"),                       # 배포 장치 자체
    (["scripts/anything.py"], "bot"),
    ([], "console"),
])
def test_classify(tree, changed, expected):
    assert scope.classify(tree, changed)["scope"] == expected


def test_classify_on_the_real_tree():
    """실제 코드에서 대표 경우가 맞게 나뉘는지. 봇이 쓰는 콘솔 모듈은 봇 쪽이다."""
    result = scope.classify(ROOT, [
        "src/tybot/console/license_people.py",
        "src/tybot/console/workspace_store.py",
        "src/tybot/bot_access.py",
    ])
    assert result["console"] == ["src/tybot/console/license_people.py"]
    assert set(result["bot"]) == {"src/tybot/console/workspace_store.py", "src/tybot/bot_access.py"}
    assert "tybot.console.app" not in scope.bot_used_console_modules(ROOT)


def test_cli_reads_paths_from_stdin(tree):
    out = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "deploy_scope.py"), "--root", str(tree), "-"],
        input="src/tybot/console/only.py\n", capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["scope"] == "console"


def test_update_checks_scope_before_install_and_restarts_only_console():
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    guard = 'scripts/deploy_scope.py" --root "$SRC" -'
    install = 'TYBOT_INSTALL_HINTS=0 bash "$SRC/deploy/install.sh"'
    assert guard in script and script.index(guard) < script.index(install)
    assert 'git diff --name-only "$DEPLOYED" HEAD' in script     # 배포본 기준 비교
    console_block = script[script.index('if [[ "$SCOPE" == "console" ]]; then\n  # 봇과'):]
    console_block = console_block[:console_block.index("\nfi\n")]
    assert "systemctl restart tybot-console" in console_block
    assert "systemctl restart tybot\n" not in console_block
    assert "exit 0" in console_block
    assert '[[ "$SCOPE" == "console" ]] || SCOPE=all' in script


def test_runner_picks_scope_from_a_fixed_list_and_explains_refusal():
    script = (ROOT / "deploy" / "deploy-runner.sh").read_text(encoding="utf-8")
    assert 'console) SCOPE=console ;;' in script and '*)       SCOPE=all ;;' in script
    assert 'TYBOT_DEPLOY_SCOPE="$SCOPE"' in script
    assert "UPDATE_RC == 3" in script and "「봇 배포」 로 진행하세요" in script


def test_install_creates_console_secret_once():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    block = script[script.index("# 콘솔 세션 서명 키"):script.index("# 워크스페이스 Slack 토큰 암호화 키")]
    assert '! grep -qE "^CONSOLE_SECRET=.{32,}"' in block
    assert "secrets.token_urlsafe(48)" in block
    assert ">> \"$CONF_DIR/tybot.env\"" in block
