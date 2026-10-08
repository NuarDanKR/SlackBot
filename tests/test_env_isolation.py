"""시험이 **운영 DB 에 붙지 않는다.**

2026-09-30 실측. `python -m pytest` 한 번이 개발 PC 의 운영 DB
(`.env` 의 `DATABASE_URL`)에 `archive_message_revision` 행을 썼다.

경로는 이랬다.

1. `test_orgsync.db_conn` 픽스처가 `envfile.load_env_file()` 을 부른다
2. 그 함수가 저장소 `.env` 를 읽어 `os.environ["DATABASE_URL"]` 에 넣는다
3. `monkeypatch` 가 넣은 값이 아니라 **되돌려지지 않는다**
4. 뒤에 오는 시험들이 진짜 DB 에 붙는다

막는 곳은 `conftest.py` 의 수집 전 훅이다. 여기서는 방벽이 실제로 서 있는지 본다.
방벽이 사라지면 조용히 다시 붙게 되고, 그때는 오류가 아니라 행으로 나타난다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tybot.archive import ingest_ack
from tybot.envfile import load_env_file

REPO = Path(__file__).resolve().parent.parent


def test_the_env_file_pointer_is_not_the_repo_dotenv():
    """이것이 방벽 자체다. 가리키는 곳이 저장소 `.env` 면 그 안의 DSN 이 들어온다."""
    pointer = os.environ.get("TYBOT_ENV_FILE", "")

    assert pointer, "conftest 의 세션 픽스처가 안 돌았습니다"
    assert Path(pointer).resolve() != (REPO / ".env").resolve()
    assert Path(pointer).read_text(encoding="utf-8") == ""
    assert os.environ.get("ENV_SETTINGS_PATH") == pointer


def test_loading_the_env_file_here_brings_no_database_url():
    """`load_env_file()` 을 부르는 시험이 또 생겨도 DSN 이 안 딸려 오게."""
    before = os.environ.get("DATABASE_URL")

    load_env_file()

    assert os.environ.get("DATABASE_URL") == before


def test_runtime_state_cannot_fall_through_to_the_operational_tree():
    """Tests without a local override must never touch the live ACK outbox."""
    state_root = Path(os.environ["STATE_DIR"]).resolve()
    lock_root = Path(os.environ["LOCK_DIR"]).resolve()
    env_guard = Path(os.environ["TYBOT_ENV_FILE"]).resolve().parent

    assert state_root.parent == env_guard
    assert lock_root.parent == env_guard
    assert ingest_ack.outbox_path("tyit") == (
        state_root / "state" / "ingest-ack-outbox" / "tyit.jsonl"
    )
    assert Path("/var/lib/tybot") not in (state_root, lock_root)


def test_ambient_database_url_is_rejected_before_collection(monkeypatch):
    import conftest

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/tyslackai")
    with pytest.raises(pytest.UsageError, match="ambient DATABASE_URL"):
        conftest.pytest_configure(None)


def test_the_repo_dotenv_is_not_committed():
    """`.env` 자체는 저장소 밖 자산이다(절대 원칙 6). 커밋되면 DSN 이 공개된다."""
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    rules = {line.strip() for line in ignore}

    assert ".env" in rules or "/.env" in rules
