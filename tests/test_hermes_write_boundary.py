"""Hermes 원문 쓰기 경계 — 진입점을 목록으로 고정한다.

설계: `docs/design/hermes-write-entrypoints.md`

`82c120f` 로 Hermes 전체 소스가 `subbots/hermes/` 에 들어왔다. 그 안에는 Slack 에서
원문을 받아 아카이브에 쓰는 경로가 여럿 있다 — 스케줄러·CLI·스킬. 지금은 아무도
실행하지 않지만, 「지금 안 돈다」 와 「돌 수 없다」 는 다르다.

여기서 고정하는 것은 넷이다.

1. 쓰기 진입점이 **목록보다 늘지 않는다**
2. TYBot 은 Hermes 소스를 **실행하지 않는다** — 읽는 것은 `contract/` 뿐
3. 배포가 소스를 **싣지 않는다**
4. 연동 선언이 **읽기 전용 도구 모드**를 유지한다

깨졌을 때 할 일은 시험을 고치는 것이 아니라 **왜 늘었는지 확인하는 것**이다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"

pytestmark = pytest.mark.skipif(
    not HERMES.is_dir(), reason="subbots/hermes 가 없는 설치에서는 건너뛴다"
)

# --- 목록 (설계 문서 §2 와 같은 것) -------------------------------------------
#
# 늘릴 때는 **문서와 함께** 고친다. 코드에만 적으면 왜 늘었는지가 사라진다.
WRITING_NPM_SCRIPTS = frozenset({
    "ingest",        # 자동 반영
    "backfill",      # 소급 수집
    "init-archive",  # 저장소 씨앗
    "digest:daily",
    "digest:weekly",
    "start",         # 스케줄러 기동 — 쓰기를 예약한다
})
READONLY_NPM_SCRIPTS = frozenset({
    "ask", "health", "doctor", "deployed",
    "check", "check:offline", "check:archive", "check:live",
    "log:render", "log:measure",
})

WRITING_SKILL_SCRIPTS = frozenset({
    ".claude/skills/archive-inbox/scripts/decide_work.py",
    ".claude/skills/archive-inbox/scripts/review_work.py",
    ".claude/skills/archive-run/scripts/board.py",
    ".claude/skills/doc-archive/scripts/apply_approvals.py",
    ".claude/skills/doc-archive/scripts/check_against_libreoffice.py",
    ".claude/skills/doc-archive/scripts/fetch_slack_files.py",
    ".claude/skills/doc-archive/scripts/insert_entry.py",
    ".claude/skills/doc-archive/scripts/join_channels.py",
    ".claude/skills/doc-archive/scripts/review_batch.py",
    ".claude/skills/doc-archive/scripts/xlsx_to_blocks.py",
    ".claude/skills/slack-sync/scripts/apply_edits.py",
    ".claude/skills/slack-sync/scripts/insert_messages.py",
    ".claude/skills/slack-sync/scripts/verify_archive.py",
    ".claude/skills/slack-sync/scripts/verify_backfill.py",
})

WRITING_SRC_MODULES = frozenset({
    "src/convo-log/store.js",
    "src/ingest/git.js",
    "src/ingest/pending-work.js",
    "src/ingest/slack-archive.js",
    "src/ingest/util.js",
    "src/ingest/verify.js",
    "src/slack-live.js",
})

SCHEDULED_JOBS = frozenset({
    "daily", "weekly", "health", "healthPre", "ingest", "ingestPre",
})

# 파이썬 쪽 쓰기·다운로드·외부 실행 신호.
_PY_WRITE = re.compile(
    r"""open\([^)]*['"][wax]|write_text|writelines|mkdir|"""
    r"""shutil\.(copy|move|rmtree)|os\.replace|urlopen|requests\.(get|post)|subprocess"""
)
# JS 쪽 같은 신호.
_JS_WRITE = re.compile(
    r"(writeFile|appendFile|mkdirSync|rmSync|renameSync|unlinkSync|execFile|spawn)"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --- 1. 진입점이 늘지 않는다 ---------------------------------------------------
def test_npm_entrypoints_match_the_inventory():
    """`package.json` 에 새 명령이 생기면 어느 쪽인지 적고 넘어가야 한다."""
    scripts = set(json.loads(_read(HERMES / "package.json"))["scripts"])

    assert scripts == WRITING_NPM_SCRIPTS | READONLY_NPM_SCRIPTS, (
        "npm 진입점이 목록과 다르다. docs/design/hermes-write-entrypoints.md §2.2 를 "
        "먼저 고치고 이 목록을 맞춰라"
    )


def test_skill_write_scripts_match_the_inventory():
    """스킬은 사람이 명령을 치지 않아도 **에이전트가 고를 수 있다.**

    그래서 가장 위험한 축이고, 가장 조용히 늘어난다.
    """
    found = {
        p.relative_to(HERMES).as_posix()
        for p in HERMES.glob(".claude/skills/*/scripts/*.py")
        if not p.name.startswith("test_") and _PY_WRITE.search(_read(p))
    }

    assert found == WRITING_SKILL_SCRIPTS, (
        "쓰기·다운로드 가능한 스킬 스크립트가 목록과 다르다. "
        f"새로 생긴 것: {sorted(found - WRITING_SKILL_SCRIPTS)} / "
        f"사라진 것: {sorted(WRITING_SKILL_SCRIPTS - found)}"
    )


def test_src_write_modules_match_the_inventory():
    found = {
        p.relative_to(HERMES).as_posix()
        for p in HERMES.glob("src/**/*.js")
        if _JS_WRITE.search(_read(p))
    }

    assert found == WRITING_SRC_MODULES, (
        f"쓰기 모듈이 목록과 다르다. 새로 생긴 것: {sorted(found - WRITING_SRC_MODULES)}"
    )


def test_scheduled_jobs_match_the_inventory():
    """기동만으로 예약되는 작업. 여기 늘면 **아무도 안 눌러도** 쓰기가 는다."""
    text = _read(HERMES / "src" / "scheduler.js")
    # 예약은 두 모양으로 적힌다 — 직접 `schedule(client, 'x')` 와 역할 묶음
    # `take(ROLES.X, 'a', 'b')`. **둘 다 봐야 한다.** 한쪽만 보면 구조를 바꾼 날
    # 목록이 조용히 비고, 빈 목록은 늘 통과한다.
    jobs = set(re.findall(r"schedule\(client,\s*'([^']+)'\)", text))
    for group in re.findall(r"take\(\s*ROLES\.[A-Z_]+\s*,([^)]*)\)", text):
        jobs.update(re.findall(r"'([^']+)'", group))

    assert jobs == SCHEDULED_JOBS


# --- 2. TYBot 은 Hermes 소스를 실행하지 않는다 ---------------------------------
def test_tybot_only_reads_the_contract_never_the_source():
    """연동 모드의 쓰기 차단은 **이것 하나로** 성립한다.

    Hermes 는 자기 권한이 없다. 우리 도구(`specialist_tools`)만 부르고 그 도구는
    읽기 전용이며, 권한은 `RequestContext` 한 곳에서 판정된다(원칙 3).
    소스를 부르는 경로가 생기는 순간 그 보장이 통째로 사라진다.
    """
    # 경로는 두 가지 모양으로 적힌다 — 리터럴(`subbots/hermes/src/x.js`)과
    # 조각 조립(`"subbots" / "hermes" / "contract"`). **둘 다 봐야 한다.**
    # 처음에는 리터럴만 봤는데, 우리 코드는 전부 조각 조립이라 시험이 헛돌았다.
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        lines = _read(path).splitlines()
        for number, line in enumerate(lines, start=1):
            code = line.split("#", 1)[0]
            if "subbots" not in code or "hermes" not in code:
                continue
            # Path 체인은 다음 줄로 넘어갈 수 있다. 창을 조금 넓게 본다.
            window = " ".join(lines[number - 1 : number + 2])
            if "contract" in window:
                continue
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {code.strip()}")

    assert not offenders, (
        "우리 코드가 Hermes 소스를 가리킨다. 연동 모드는 계약만 읽어야 한다: "
        + "; ".join(offenders)
    )


def test_no_python_module_imports_or_spawns_hermes():
    """`subprocess` 로 `npm run ...` 를 부르는 경로도 같은 구멍이다."""
    bad = re.compile(r"(npm\s+run|node\s+)[^\n\"']*(ingest|backfill|init-archive|digest)")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if bad.search(_read(path))
    ]

    assert not offenders, f"Hermes 쓰기 명령을 부르는 코드가 있다: {offenders}"


# --- 3. 배포가 소스를 싣지 않는다 ----------------------------------------------
def test_install_ships_only_the_contract():
    """`install.sh` 가 소스를 복사하기 시작하면 서버에 쓰기 경로가 생긴다."""
    install = _read(ROOT / "deploy" / "install.sh")
    hermes_lines = [
        line.strip()
        for line in install.splitlines()
        if "subbots" in line and not line.strip().startswith("#")
    ]

    assert hermes_lines, "install.sh 에서 subbots 처리를 찾지 못했다"
    assert any("contract/prompt.md" in line for line in hermes_lines)
    assert not any(
        re.search(r"subbots/\*?/?(src|scripts|\.claude)", line) for line in hermes_lines
    ), f"배포가 Hermes 소스를 싣는다: {hermes_lines}"


# --- 4. 연동 선언이 읽기 전용 도구 모드를 유지한다 -----------------------------
def test_the_integration_declares_read_only_tool_mode():
    """`execution_mode` 가 바뀌면 권한 판정의 주인이 바뀐다."""
    toml = _read(HERMES / "tybot-specialist.toml")

    assert 'execution_mode = "tools"' in toml
    assert 'prompt = "contract/prompt.md"' in toml
    # 실행 경로·외부 URL 은 선언에 없어야 한다(subbots/README.md 의 경계).
    assert not re.search(r"^\s*(command|exec|entrypoint|url)\s*=", toml, re.MULTILINE)


def test_the_contract_files_our_code_reads_exist():
    """계약이 없으면 전문 봇은 「미배포」 다 — 조용히 못 답하는 것보다 낫다."""
    assert (HERMES / "contract" / "prompt.md").is_file()
    assert (HERMES / "contract" / "summary-review.md").is_file()
