"""배포 범위 판정 — 바뀐 파일만 보고 「콘솔만 재시작해도 되는가」 를 정한다.

배포 관리 화면에는 버튼이 둘이다.

| 버튼 | 하는 일 |
|---|---|
| 봇 배포 | 지금까지와 같다. 테스트 → 설치 → 스키마 → **봇·콘솔 재시작** |
| 콘솔 배포 | 테스트 → 설치 → 스키마 → **콘솔만 재시작**. 봇은 그대로 돈다 |

콘솔과 봇은 같은 코드 폴더(`/opt/tybot`)를 쓴다. 그래서 「콘솔 배포」 는 **바뀐 파일이 전부
콘솔 전용일 때만** 해도 된다. 봇 코드가 섞인 채로 콘솔만 재시작하면 봇 프로세스는 옛 코드로
돌고, 타이머 작업(수집·DM 등)은 새 코드로 떠서 한 서버에 두 버전이 섞인다.

## 「콘솔 전용」 을 어떻게 정하나
`src/tybot/console/` 아래라고 다 콘솔 전용이 아니다. 봇이 가져다 쓰는 모듈이 있다
(`workspace_store`·`audit_store`·`bot_identity` 등). 그래서 **가져오기(import) 그래프**로 정한다.

1. `src/tybot/` 에서 `console/` 밖의 모든 .py 가 가져가는 `tybot.console.*` 모듈을 모은다
2. 그 모듈들이 다시 가져가는 콘솔 모듈을 끝까지 따라간다 → 「봇이 쓰는 콘솔 모듈」
3. 이 집합에 없는 `console/*.py` 만 콘솔 전용이다

경로 목록을 사람이 관리하지 않는다 — 봇이 새로 콘솔 모듈을 가져다 쓰기 시작하면 그 순간
판정이 따라 바뀐다. 정적 분석이라 `importlib` 로 이름을 조립해 부르는 경우는 못 본다.
그래서 모르는 경우는 전부 **봇 쪽으로** 판정한다(콘솔 배포가 거절될 뿐, 잘못 배포되지 않는다).

## 나머지 파일
- 실행되지 않는 것(테스트·문서·마크다운) → 범위에 영향 없음
- 스키마(`deploy/sql/`) → 영향 없음. 두 배포 모두 스키마를 적용한다
- 콘솔 유닛·콘솔 의존성·콘솔용 sudo 헬퍼 → 콘솔
- 그 밖의 전부(배포 스크립트, 봇 코드, `scripts/` …) → 봇

사용:
    python scripts/deploy_scope.py --root <소스> <바뀐 경로>...
    git diff --name-only A B | python scripts/deploy_scope.py --root <소스> -
출력은 JSON 한 줄. `scope` 가 `console` 이면 콘솔 배포 가능, `bot` 이면 봇 배포가 필요하다.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PKG = "tybot"
CONSOLE = "tybot.console"

#: 실행되지 않는 파일 — 어느 배포에도 영향이 없다.
NEUTRAL_PREFIXES = ("tests/", "docs/", ".github/", ".claude/", "deploy/sql/")
NEUTRAL_SUFFIXES = (".md",)

#: 콘솔 프로세스만 쓰는 배포 파일.
CONSOLE_DEPLOY_FILES = frozenset({
    "deploy/tybot-console.service",
    "deploy/requirements-console.txt",
    "deploy/tybot-console-logs",
    "deploy/tybot-console-logs.sudoers",
    "deploy/tybot-console-timers",
    "deploy/tybot-console-timers.sudoers",
})


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root / "src").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path, module: str) -> set[str]:
    """이 파일이 가져가는 모듈 이름(절대 경로로 풀어서). 못 읽으면 빈 집합."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    is_pkg = path.name == "__init__.py"
    package = module if is_pkg else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target:
                continue
            found.add(target)
            # `from .console import audit_store` 처럼 이름이 하위 모듈일 수 있다.
            found.update(f"{target}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def bot_used_console_modules(root: Path) -> set[str]:
    """봇(콘솔 밖 코드)이 직접·간접으로 쓰는 `tybot.console.*` 모듈."""
    src = root / "src" / PKG
    console_dir = src / "console"
    console_files = {_module_name(root, p): p for p in console_dir.rglob("*.py")}
    reached: set[str] = set()
    queue: list[str] = []

    def take(names: set[str]) -> None:
        for name in names:
            if name in console_files and name not in reached:
                reached.add(name)
                queue.append(name)

    for path in src.rglob("*.py"):
        if console_dir in path.parents:
            continue
        take(_imports(path, _module_name(root, path)))
    while queue:
        name = queue.pop()
        take(_imports(console_files[name], name))
    if reached:
        reached.add(CONSOLE)   # 패키지 __init__ 은 하위 모듈과 함께 실행된다
    return reached


def classify(root: Path, changed: list[str]) -> dict:
    """바뀐 경로들을 봇·콘솔·무관으로 나누고 배포 범위를 정한다."""
    shared = bot_used_console_modules(root)
    out: dict[str, list[str]] = {"bot": [], "console": [], "neutral": []}
    for raw in changed:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        if path.startswith(NEUTRAL_PREFIXES) or path.endswith(NEUTRAL_SUFFIXES):
            out["neutral"].append(path)
        elif path in CONSOLE_DEPLOY_FILES:
            out["console"].append(path)
        elif path.startswith(f"src/{PKG}/console/") and path.endswith(".py"):
            module = _module_name(root, root / path)
            (out["bot"] if module in shared else out["console"]).append(path)
        else:
            out["bot"].append(path)
    scope = "bot" if out["bot"] else "console"
    return {"scope": scope, **out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("paths", nargs="*", help="바뀐 경로. '-' 면 표준 입력에서 한 줄씩")
    args = parser.parse_args(argv)
    paths = list(args.paths)
    if paths == ["-"]:
        paths = sys.stdin.read().splitlines()
    print(json.dumps(classify(args.root.resolve(), paths), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
