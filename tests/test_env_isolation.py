"""시험이 **운영 DB 에 붙지 않는다.**

2026-09-30 실측. `python -m pytest` 한 번이 개발 PC 의 운영 DB
(`.env` 의 `DATABASE_URL`)에 `archive_message_revision` 행을 썼다.

경로는 이랬다.

1. `test_orgsync.db_conn` 픽스처가 `envfile.load_env_file()` 을 부른다
2. 그 함수가 저장소 `.env` 를 읽어 `os.environ["DATABASE_URL"]` 에 넣는다
3. `monkeypatch` 가 넣은 값이 아니라 **되돌려지지 않는다**
4. 뒤에 오는 시험들이 진짜 DB 에 붙는다

막는 곳은 `conftest.py` 다. 여기서는 그 방벽이 **실제로 서 있는지**만 본다.
방벽이 사라지면 조용히 다시 붙게 되고, 그때는 오류가 아니라 행으로 나타난다.
"""

from __future__ import annotations

import os
from pathlib import Path

from tybot.envfile import load_env_file

REPO = Path(__file__).resolve().parent.parent


def test_the_env_file_pointer_is_not_the_repo_dotenv():
    """이것이 방벽 자체다. 가리키는 곳이 저장소 `.env` 면 그 안의 DSN 이 들어온다."""
    pointer = os.environ.get("TYBOT_ENV_FILE", "")

    assert pointer, "conftest 의 세션 픽스처가 안 돌았습니다"
    assert Path(pointer).resolve() != (REPO / ".env").resolve()


def test_loading_the_env_file_here_brings_no_database_url():
    """`load_env_file()` 을 부르는 시험이 또 생겨도 DSN 이 안 딸려 오게."""
    before = os.environ.get("DATABASE_URL")

    load_env_file()

    assert os.environ.get("DATABASE_URL") == before


def test_an_explicitly_exported_dsn_still_wins():
    """통합 시험을 못 돌리게 막는 것이 아니다. **실수로 붙는 것**을 막는다.

    `DATABASE_URL=… python -m pytest` 로 적으면 그대로 쓰인다 —
    `load_env_file()` 이 `override=False` 이기 때문이다.
    """
    os.environ["DATABASE_URL"] = "postgresql://explicit/only-for-this-test"
    try:
        load_env_file()

        assert os.environ["DATABASE_URL"] == "postgresql://explicit/only-for-this-test"
    finally:
        os.environ.pop("DATABASE_URL", None)


def test_the_repo_dotenv_is_not_committed():
    """`.env` 자체는 저장소 밖 자산이다(절대 원칙 6). 커밋되면 DSN 이 공개된다."""
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    rules = {line.strip() for line in ignore}

    assert ".env" in rules or "/.env" in rules
