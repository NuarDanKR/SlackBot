"""커밋 가드 훅 — 강제 푸시 차단이 **정확히** 걸리는가.

가드는 두 방향으로 틀릴 수 있고, 둘 다 나쁘다.

| 틀리는 방향 | 결과 |
|---|---|
| 진짜 강제 푸시를 놓친다 | 중앙 저장소 이력이 손상된다. 되돌릴 수 없다 |
| 멀쩡한 명령을 막는다 | 사람이 가드를 끈다. 그때 진짜가 지나간다 |

2026-09-07 에 두 번째가 실제로 났다 — 명령어 **전체**에서 `-f` 를 찾다가,
커밋 메시지 heredoc 안의 `psql ... -f -` 예시를 강제 푸시로 읽었다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard.py"

# 리터럴을 쪼개 둔다. 이 파일을 다루는 셸 명령이 가드에 걸리지 않게 하려는 것이 아니라,
# **테스트가 자기 자신을 막는 일**을 피하려는 것이다(가드는 Bash 명령을 검사한다).
PUSH = "git " + "push"


def _blocked(command: str) -> bool:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.returncode != 0


@pytest.mark.parametrize(
    "command",
    [
        f"{PUSH} --force origin master",
        f"{PUSH} origin master --force",
        f"{PUSH} -f origin master",
        f"{PUSH} origin master -f",
        f"{PUSH} --force-with-lease",
        f"cd /repo && {PUSH} --force",
    ],
    ids=["force앞", "force뒤", "f앞", "f뒤", "with-lease", "체인"],
)
def test_real_force_pushes_are_blocked(command):
    assert _blocked(command), f"강제 푸시를 놓쳤다: {command}"


@pytest.mark.parametrize(
    "command",
    [
        f"{PUSH} origin master",
        f"{PUSH} -q origin master",
        f"git commit -m 'psql -f -' && {PUSH} origin master",
        "grep -f patterns.txt file.txt",
    ],
    ids=["평범", "조용히", "메시지에-f", "푸시아님"],
)
def test_ordinary_commands_are_not_blocked(command):
    """오탐이 잦으면 사람이 가드를 끄고, 그때 진짜가 지나간다."""
    assert not _blocked(command), f"멀쩡한 명령을 막았다: {command}"


def test_a_comment_on_the_push_line_still_blocks():
    """같은 줄 주석의 `-f` 도 막힌다. **일부러 그대로 둔다.**

    `#` 에서 잘라 내면 인자 안의 `#`(`feat#3` 같은 브랜치명) 뒤에 오는 진짜 `-f` 를
    놓친다. 두 오류 중 놓치는 쪽이 훨씬 나쁘다 — 중앙 저장소 이력은 되돌릴 수 없다.
    주석을 지우면 통과하므로 사람이 막히는 값은 작다.
    """
    assert _blocked(f"{PUSH} origin master  # -f 는 쓰지 않는다")
