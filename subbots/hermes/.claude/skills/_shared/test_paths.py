#!/usr/bin/env python3
"""paths.py 시험. **임시 폴더에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/_shared/test_paths.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

paths.py 가 정하는 것은 **봇과 스킬이 같은 자료를 보느냐**다. 여기가 어긋나면
에러가 안 나고 봇은 옛 자료를, 스킬은 새 자료를 본다 — 증상이 오답으로만 나타나서
며칠 뒤에야 드러난다. 그래서 「우선순위가 이 순서다」를 코드로 못박아 둔다.

특히 **환경변수가 .env 를 이긴다**는 순서가 중요하다. VM 은 systemd 가 환경변수를
주고, PC 는 .env 만 있다. 순서가 뒤집히면 VM 에서 .env 의 옛 경로가 이겨서
systemd 설정을 고쳐도 아무 일이 안 일어난다.
"""
import os
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

FAIL = []


def check(name, got, want):
    if got == want:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      나온 것: {got}\n      바랄 것: {want}")
        FAIL.append(name)


def test_code_root_is_repo_top():
    """CODE_ROOT 는 .claude/skills/_shared 에서 네 단계 위다."""
    want = Path(__file__).resolve().parents[3]
    check("CODE_ROOT = 저장소 뿌리", paths.CODE_ROOT, want)


def test_env_var_wins():
    """환경변수가 있으면 그것이 이긴다."""
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "hermes"
        code.mkdir()
        (code / ".env").write_text("HERMES_DATA_ROOT=/from/env/file\n", encoding="utf-8")
        os.environ["HERMES_DATA_ROOT"] = str(Path(td) / "from-env-var")
        try:
            got = paths.data_root_from(code)
        finally:
            del os.environ["HERMES_DATA_ROOT"]
        check("환경변수가 .env 를 이긴다", got, (Path(td) / "from-env-var").resolve())


def test_env_file_used_when_no_var():
    """환경변수가 없으면 .env 의 줄을 쓴다."""
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "hermes"
        code.mkdir()
        target = Path(td) / "archive"
        (code / ".env").write_text(
            f"SLACK_BOT_TOKEN=xoxb-x\nHERMES_DATA_ROOT={target}\n", encoding="utf-8")
        os.environ.pop("HERMES_DATA_ROOT", None)
        check(".env 의 줄을 읽는다", paths.data_root_from(code), target.resolve())


def test_fallback_is_sibling():
    """둘 다 없으면 코드 저장소의 이웃."""
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "hermes"
        code.mkdir()
        os.environ.pop("HERMES_DATA_ROOT", None)
        check("기본값 = 이웃 hermes-archive",
              paths.data_root_from(code), (Path(td) / "hermes-archive").resolve())


def test_env_file_strips_quotes():
    """값의 따옴표를 벗긴다 (주석 줄이 섞여 있어도).

    **주석을 건너뛰는 것 자체는 여기서 검사되지 않는다.** paths.py 의 `#` 검사는
    지워도 이 시험이 안 빨개진다 — 주석 줄을 partition 하면 `#` 가 키 안에 들어가
    (`'# HERMES_DATA_ROOT'`) 어차피 안 맞기 때문이다 (2026-08-31 돌연변이 검사).
    그래서 아래 이름에 「주석」을 안 쓴다. 시험이 안 하는 일을 이름이 하면
    다음 사람이 덮여 있다고 믿는다.
    """
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "hermes"
        code.mkdir()
        target = Path(td) / "quoted"
        (code / ".env").write_text(
            f'# HERMES_DATA_ROOT=/wrong/commented\nHERMES_DATA_ROOT="{target}"\n',
            encoding="utf-8")
        os.environ.pop("HERMES_DATA_ROOT", None)
        check("따옴표를 벗긴다", paths.data_root_from(code), target.resolve())


def test_missing_env_file_is_not_an_error():
    """.env 가 아예 없어도 죽지 않고 기본값으로 간다."""
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "hermes"
        code.mkdir()
        os.environ.pop("HERMES_DATA_ROOT", None)
        check(".env 가 없으면 기본값",
              paths.data_root_from(code), (Path(td) / "hermes-archive").resolve())


def test_derived_paths_hang_off_data_root():
    """ARCHIVE·DOCS_DIR 따위가 DATA_ROOT 아래에 있다."""
    check("ARCHIVE", paths.ARCHIVE, paths.DATA_ROOT / "slack-export")
    check("CHANNELS", paths.CHANNELS, paths.DATA_ROOT / "slack-export" / "channels")
    check("DOCS_DIR", paths.DOCS_DIR, paths.DATA_ROOT / "documents")
    check("PROJECTS", paths.PROJECTS, paths.DATA_ROOT / "documents" / "projects")
    check("CONFIG", paths.CONFIG, paths.DATA_ROOT / "config.json")
    check("ENV_FILE", paths.ENV_FILE, paths.CODE_ROOT / ".env")


for fn in (test_code_root_is_repo_top, test_env_var_wins, test_env_file_used_when_no_var,
           test_fallback_is_sibling, test_env_file_strips_quotes,
           test_missing_env_file_is_not_an_error, test_derived_paths_hang_off_data_root):
    print(fn.__doc__.splitlines()[0])
    fn()

print()
if FAIL:
    print(f"실패 {len(FAIL)}건: {', '.join(FAIL)}")
    sys.exit(1)
print("전부 통과")
