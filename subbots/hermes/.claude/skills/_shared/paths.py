#!/usr/bin/env python3
"""스킬 다섯이 함께 쓰는 경로 (2026-09-22 에 `issue-trim` 이 들었다). **여기 한 자리에만 둔다.**

저장소가 둘로 갈렸다 — 코드(이 저장소)와 자료(slack-export·documents·config.json).
경로를 스크립트마다 따로 잡으면 한쪽만 고쳐져 조용히 갈린다. 갈리면 에러가 아니라
**오답**으로 나타난다: 봇은 옛 자료를, 스킬은 새 자료를 본다.

봇 쪽의 같은 값은 `src/config.js` 의 ROOT·DATA_ROOT 다.
둘이 같은 곳을 가리키는지는 `scripts/check-roots.js` 가 본다.

── 갈리기 전에는 이랬다 ──

각 스크립트가 `ROOT = Path(__file__).resolve().parents[4]` 로 **파일 위치에서 추측**했고,
`review_batch.py` 는 그 위험을 이렇게 적어 두고 있었다 — "스킬을 다른 깊이에 깔면
엉뚱한 저장소에서 git 을 돌리게 되고, 그러면 관문이 「담긴 문서 없음」이라며 통과시킨다
— 훅은 걸려 있는데 아무것도 안 지킨다." 이 파일이 그 자리를 닫는다.
"""
import os
from pathlib import Path

#: 코드 저장소 뿌리. 이 파일은 <뿌리>/.claude/skills/_shared/paths.py 에 있다.
CODE_ROOT = Path(__file__).resolve().parents[3]


def _from_env_file(env_path):
    """`.env` 에서 HERMES_DATA_ROOT= 줄을 찾는다. 없으면 None.

    dotenv 를 쓰지 않는다 — 스킬은 파이썬 의존성을 openpyxl 하나로 묶어 두었고,
    여기 필요한 것은 줄 하나 읽기뿐이다.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        # `#` 검사는 **방어용이고 지금은 결과를 안 바꾼다.** 주석 줄을 partition 하면
        # `#` 가 키 안에 들어가(`'# HERMES_DATA_ROOT'`) 아래 `==` 가 어차피 안 맞는다.
        # 그래서 이 줄을 지워도 시험이 안 빨개진다 — 2026-08-31 돌연변이 검사로 확인했다.
        # 남겨 두는 것은 키를 == 이 아니라 startswith 로 무르게 고치는 날을 위해서다.
        # 그날 이 줄이 없으면 주석이 값으로 읽히고, 증상은 에러가 아니라 엉뚱한 경로다.
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep and key.strip() == "HERMES_DATA_ROOT":
            val = val.strip().strip('"').strip("'")
            if val:
                return val
    return None


def data_root_from(code_root):
    """자료 저장소 뿌리를 정한다. **환경변수 → .env → 이웃 폴더** 순.

    이 순서를 뒤집지 말 것. VM 은 systemd 가 환경변수를 주고 PC 는 `.env` 만 있다.
    `.env` 가 이기면 VM 에서 systemd 설정을 고쳐도 아무 일이 안 일어난다.
    """
    code_root = Path(code_root).resolve()
    val = os.environ.get("HERMES_DATA_ROOT") or _from_env_file(code_root / ".env")
    if val:
        return Path(val).expanduser().resolve()
    return (code_root.parent / "hermes-archive").resolve()


#: 자료 저장소 뿌리.
DATA_ROOT = data_root_from(CODE_ROOT)

ARCHIVE = DATA_ROOT / "slack-export"
CHANNELS = ARCHIVE / "channels"
DOCS_DIR = DATA_ROOT / "documents"
PROJECTS = DOCS_DIR / "projects"
CONFIG = DATA_ROOT / "config.json"
ENV_FILE = CODE_ROOT / ".env"
