"""TYBot 연동 모드 — 원문 쓰기가 **우회 진입점별로** 막히는가.

설계: `docs/design/hermes-write-entrypoints.md`
목록 고정: `tests/test_hermes_write_boundary.py`

`test_hermes_write_boundary.py` 는 **진입점이 늘지 않는 것**을 본다. 이 파일은
**각 진입점이 실제로 막히는 것**을 본다. 둘은 다른 질문이다 — 목록이 맞아도 관문이
안 걸려 있으면 아무것도 막히지 않는다.

## 네 축

| 축 | 어떻게 막나 |
|---|---|
| 스케줄러 | 쓰는 작업을 **예약하지 않는다**(실행 시 실패가 아니라 미등록) |
| CLI | 진입 스크립트가 종료 코드 2 로 멈춘다 |
| 스킬 | `main()` 맨 앞에서 종료 코드 2 로 멈춘다 |
| 직접 모듈 호출 | 쓰기 함수가 `ArchiveWriteBlocked` 를 던진다 |

## 기본값은 건드리지 않는다

`HERMES_MODE` 가 비면 **PF 직접 실행**이고 지금까지와 똑같다. 그것도 여기서 본다 —
차단을 넣으면서 PF 운영을 조용히 멈추는 것이 가장 나쁜 실패다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
SKILLS = HERMES / ".claude" / "skills"

pytestmark = pytest.mark.skipif(
    not HERMES.is_dir(), reason="subbots/hermes 가 없는 설치에서는 건너뛴다"
)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node 가 없어 건너뛴다")

# `@slack/bolt` 등이 없으면 ESM 은 **링크 단계에서** 실패한다 — 어떤 최상위 코드도
# 돌기 전이라 관문이 설 자리가 없다. 그건 코드 결함이 아니라 환경이다.
#
# 그래서 실행 시험은 의존성이 있을 때만 돌리고, 없을 때는 **정적 시험**이 같은 것을
# 본다(관문이 있는가·맨 앞인가). 둘 다 없으면 「설치 안 돼서 통과」 가 된다 —
# 그게 가장 나쁜 초록불이다.
HAS_DEPS = (HERMES / "node_modules").is_dir()
needs_deps = pytest.mark.skipif(
    not HAS_DEPS, reason="subbots/hermes/node_modules 가 없어 실행 시험은 건너뛴다"
)

# 막혀야 하는 CLI 진입점 (docs/design/hermes-write-entrypoints.md §2.2)
BLOCKED_CLI = ("run-ingest.js", "run-backfill.js", "init-archive.js")

# 막혀야 하는 스킬 (§2.3). 경로는 저장소 기준.
BLOCKED_SKILLS = (
    "doc-archive/scripts/fetch_slack_files.py",
    "doc-archive/scripts/insert_entry.py",
    "doc-archive/scripts/join_channels.py",
    "doc-archive/scripts/apply_approvals.py",
    "doc-archive/scripts/review_batch.py",
    "slack-sync/scripts/insert_messages.py",
    "slack-sync/scripts/apply_edits.py",
    "archive-inbox/scripts/decide_work.py",
    "archive-inbox/scripts/review_work.py",
    "archive-run/scripts/board.py",
)

# 직접 부를 수 있는 쓰기 함수 (§2.1·§2.4)
BLOCKED_FUNCTIONS = (
    ("src/ingest/index.js", "runIngest"),
    ("src/ingest/slack-archive.js", "ingestConversations"),
    ("src/ingest/backfill.js", "writeMonth"),
    ("src/ingest/git.js", "commitAndPush"),
)


def _env(mode: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("HERMES_MODE", None)
    if mode is not None:
        env["HERMES_MODE"] = mode
    # 스킬이 _shared 를 찾을 때 저장소 밖 설정을 끌어오지 않게 한다.
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _node(
    script: str, mode: str | None, *, data_root: Path | None = None,
) -> subprocess.CompletedProcess:
    env = _env(mode)
    if data_root is not None:
        env["HERMES_DATA_ROOT"] = str(data_root)
    return subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=HERMES, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )


# --- 모드 판정 자체 -----------------------------------------------------------
@needs_node
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "pf"), ("", "pf"), ("  ", "pf"), ("pf", "pf"), ("PF", "pf"),
     ("pf-archiver", "pf-archiver"), (" PF-ARCHIVER ", "pf-archiver"),
     ("tybot", "tybot"), ("TYBOT", "tybot"), (" tybot ", "tybot")],
)
def test_only_the_known_values_are_accepted(value, expected):
    """비우면 PF, `pf`·`pf-archiver`·`tybot` 만 그 모드다.

    **모르는 값은 PF 로 떨어지지 않는다**(2026-10-06 정정). 처음에는 「오타가 운영을
    멈추게 하지 않는다」 로 PF 로 봤는데, `HERMES_MODE=tybo` 로 띄우면 연동인 줄 아는
    프로세스가 **쓰기가 열린 채로** 돈다. 그 경계는
    `test_hermes_tybot_roles.py::test_any_other_value_fails_to_start` 가 본다.
    """
    got = _node("import {mode} from './src/mode.js'; console.log(mode());", value)

    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == expected


# --- 축 1·4. 직접 모듈 호출 ---------------------------------------------------
@needs_node
@needs_deps
@pytest.mark.parametrize("blocked_mode", ["tybot", "pf-archiver"])
@pytest.mark.parametrize(("module", "fn"), BLOCKED_FUNCTIONS)
def test_a_write_function_refuses_when_archiver_owns_the_source(
    tmp_path, blocked_mode, module, fn,
):
    """위 관문을 전부 우회해 함수를 직접 불러도 여기서 막힌다."""
    script = (
        f"import {{ {fn} }} from './{module}';\n"
        f"try {{ await {fn}(null, {{}}); console.log('NOT_BLOCKED'); }}\n"
        "catch (e) { console.log(e.code === 'tybot_mode_write_blocked' "
        "? 'BLOCKED' : 'OTHER:' + (e.code || e.message)); }"
    )
    shutil.copyfile(HERMES / "config.example.json", tmp_path / "config.json")
    got = _node(script, blocked_mode, data_root=tmp_path)

    assert "BLOCKED" in got.stdout, f"{fn} 이 막히지 않았다: {got.stdout}{got.stderr}"


@needs_node
@needs_deps
@pytest.mark.parametrize(("module", "fn"), BLOCKED_FUNCTIONS)
def test_the_same_function_is_not_blocked_in_pf_mode(module, fn):
    """PF 직접 실행은 **그대로여야 한다.** 차단이 운영까지 끄면 실패다.

    인자가 없어 다른 이유로 실패하는 것은 상관없다. 보는 것은 하나 —
    `tybot_mode_write_blocked` 로 막히지 **않는** 것.
    """
    script = (
        f"import {{ {fn} }} from './{module}';\n"
        f"try {{ await {fn}(null, {{}}); console.log('RAN'); }}\n"
        "catch (e) { console.log(e.code === 'tybot_mode_write_blocked' "
        "? 'BLOCKED' : 'OTHER'); }"
    )
    got = _node(script, None)

    assert "BLOCKED" not in got.stdout, f"PF 모드인데 {fn} 이 막혔다"


# --- 축 2. CLI ----------------------------------------------------------------
@needs_node
@needs_deps
@pytest.mark.parametrize("blocked_mode", ["tybot", "pf-archiver"])
@pytest.mark.parametrize("script_name", BLOCKED_CLI)
def test_a_write_cli_stops_before_doing_anything(blocked_mode, script_name):
    """사람이 직접 치는 자리다. 스택이 아니라 **사람 말로** 멈춰야 한다."""
    got = subprocess.run(
        [NODE, f"scripts/{script_name}", "--dry"],
        cwd=HERMES, env=_env(blocked_mode), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )

    assert got.returncode == 2, f"{script_name} 종료 코드 {got.returncode}"
    assert "원문" in got.stderr
    assert "HERMES_MODE" in got.stderr


# --- 축 3. 스킬 ---------------------------------------------------------------
@pytest.mark.parametrize("blocked_mode", ["tybot", "pf-archiver"])
@pytest.mark.parametrize("rel", BLOCKED_SKILLS)
def test_a_write_skill_stops_at_the_top_of_main(blocked_mode, rel):
    """**가장 위험한 축.** 사람이 명령을 치지 않아도 에이전트가 집어 실행한다."""
    path = SKILLS / rel
    assert path.is_file(), f"목록에 있는 스킬이 없다: {rel}"

    got = subprocess.run(
        [sys.executable, str(path), "--help"],
        cwd=HERMES, env=_env(blocked_mode), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )

    assert got.returncode == 2, f"{rel} 종료 코드 {got.returncode}: {got.stdout}{got.stderr}"
    assert "원문" in got.stderr


def test_a_write_skill_stops_without_python_on_path():
    env = _env("tybot")
    env["PATH"] = ""
    got = subprocess.run(
        [sys.executable, str(SKILLS / BLOCKED_SKILLS[0]), "--help"],
        cwd=HERMES, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
    )

    assert got.returncode == 2, got.stderr
    assert "TYBot 연동 모드" in got.stderr


@pytest.mark.parametrize("rel", BLOCKED_SKILLS)
def test_every_write_skill_guards_before_its_first_side_effect(rel):
    """관문이 `main()` **첫 줄**에 있어야 한다.

    인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 밖으로 나간다 — 네트워크는
    되돌릴 수 없다.
    """
    text = (SKILLS / rel).read_text(encoding="utf-8", errors="replace")
    body = text.split("def main(", 1)[1]
    head = "\n".join(body.splitlines()[:8])

    assert "exit_if_blocked" in head, f"{rel}: main() 앞머리에 관문이 없다"


# --- 축 1. 스케줄러 -----------------------------------------------------------
def test_the_scheduler_does_not_even_register_write_jobs():
    """예약해 두고 실행할 때 막으면 **매 회차 실패**가 쌓여 고장으로 보인다.

    안 하는 것과 못 하는 것은 화면에서 구별돼야 한다.
    """
    text = (HERMES / "src" / "scheduler.js").read_text(encoding="utf-8")
    start = text.split("export function startScheduler", 1)[1]

    # 예약은 역할로 걸린다(`ownsRole`). 역할이 없으면 `take()` 가 건너뛴다.
    assert "ownsRole(" in start
    assert "take(ROLES.INGEST" in start, "수집 예약이 역할 기준이 아니다"


# --- 근거는 TYBot 도구로만 ----------------------------------------------------
def test_the_integration_contract_still_declares_read_only_tools():
    """막는 것만으로는 부족하다 — **어디서 읽는지**도 고정돼 있어야 한다."""
    toml = (HERMES / "tybot-specialist.toml").read_text(encoding="utf-8")

    assert 'execution_mode = "tools"' in toml
    assert 'prompt = "contract/prompt.md"' in toml


# --- 의존성 없이도 보는 것 ----------------------------------------------------
#
# 위 실행 시험은 `node_modules` 가 있어야 돈다. 없을 때 아무것도 안 보면
# 「설치 안 돼서 통과」 가 되고, 그게 가장 나쁜 초록불이다. 아래는 소스만 읽는다.
@pytest.mark.parametrize(("module", "fn"), BLOCKED_FUNCTIONS)
def test_every_write_function_guards_in_its_first_lines(module, fn):
    """관문이 함수 **앞머리**에 있어야 한다.

    `runIngest` 는 아래에서 git sync 를 돈다. 관문이 그 뒤에 있으면 트리를 건드린
    뒤에 막는 꼴이라, 막았는데 작업 트리는 더러워진 상태가 남는다.
    """
    text = (HERMES / module).read_text(encoding="utf-8")
    assert f"export async function {fn}(" in text or f"export function {fn}(" in text

    body = text.split(f"function {fn}(", 1)[1]
    head = "\n".join(body.splitlines()[:8])

    assert "assertMayWriteArchive(" in head, f"{module}:{fn} 앞머리에 관문이 없다"


@pytest.mark.parametrize("script_name", BLOCKED_CLI)
def test_every_write_cli_carries_the_guard(script_name):
    """CLI 는 종료 코드 2 로 멈춰야 한다 — 실패(1)와 금지(2)는 다르다."""
    text = (HERMES / "scripts" / script_name).read_text(encoding="utf-8")

    assert "archiveWritesBlocked" in text, f"{script_name} 에 관문이 없다"
    assert "process.exit(2)" in text


#: 환경변수를 **읽는** 모양. 이름이 주석에 적힌 것은 읽는 것이 아니다.
#
# 전에는 `HERMES_MODE` 라는 **글자**가 있으면 실패였다. 그 규칙은 설명을 적는 것까지
# 막는다 — 모드가 왜 그 자리에 걸리는지 주석에 적을 수 없으면, 다음 사람은 그 자리가
# 모드와 관계있다는 것을 모른다. 지키려는 것은 「두 곳만 **판정한다**」 이므로 읽는
# 모양을 본다.
_MODE_READS = re.compile(
    r"""(?:process\.env(?:\.HERMES_MODE|\[\s*['"]HERMES_MODE['"]\s*\])"""
    r"""|os\.environ(?:\.get\(\s*)?\[?\s*['"]HERMES_MODE['"]"""
    r"""|getenv\(\s*['"]HERMES_MODE['"])"""
)


def test_the_mode_module_is_the_only_place_that_reads_the_switch():
    """`HERMES_MODE` 를 여기저기서 **읽으면** 판정이 갈린다.

    갈리면 오류가 아니라 **한쪽만 막힌 상태**로 나타난다 — 봇은 멈췄는데 스킬은
    쓰고 있는 식이다. 판정하는 곳은 `src/mode.js` 와 `_shared/mode.py` 둘뿐이다.

    **이름을 적은 것과 읽은 것을 가른다.** 주석·오류 문구·시험 환경 구성에는 그 이름이
    나올 수밖에 없다. 그것까지 막으면 모드가 걸리는 자리에 이유를 못 적게 되고, 이유가
    없는 관문은 다음 사람이 걷어낸다.
    """
    readers = sorted(
        path.relative_to(HERMES).as_posix()
        for path in HERMES.rglob("*.*")
        if path.suffix in {".js", ".py"}
        and "node_modules" not in path.parts
        and _MODE_READS.search(path.read_text(encoding="utf-8", errors="replace"))
    )

    assert readers == ["\x2eclaude/skills/_shared/mode.py", "src/mode.js"], readers
