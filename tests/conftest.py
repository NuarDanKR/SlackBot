"""시험이 **운영 DB 에 붙지 않게** 막는다.

2026-09-30 실측. `python -m pytest` 가 개발 PC 의 운영 DB 에 행을 썼다.

## 무슨 일이 있었나

`tests/test_orgsync.py` 의 `db_conn` 픽스처가 `envfile.load_env_file()` 을 직접
부른다. 그 함수는 저장소 루트의 `.env` 를 읽어 **`os.environ` 에 그대로 넣는다.**
`monkeypatch` 가 넣은 값이 아니라 픽스처가 넣은 값이라, 그 시험이 끝나도
`DATABASE_URL` 이 프로세스에 남는다.

그 뒤로 알파벳 순서상 뒤에 오는 시험들은 **진짜 DB 에 붙는다.** `tybot.db.connect()`
도 `console.workspace_store._connect()` 도 `DATABASE_URL` 하나만 보기 때문이다.
실제로 `test_shadow_channel_layout.py` 가 `archive_message_revision` 에 행을 넣었고,
다음 실행에서 같은 키가 이미 있다고 거절당해 그때서야 드러났다
(`create is only valid for the first archive revision`).

오류가 난 것이 다행이다. **오류를 안 내는 쓰기는 아무도 모른다.**

## 여기서 막는 방법

시험 세션이 시작할 때 `TYBOT_ENV_FILE` 을 **빈 파일**로 고정한다. `load_env_file()`
은 이 값을 제일 먼저 보므로 저장소 `.env` 를 읽지 않는다.

`load_dotenv(override=False)` 규약은 그대로라, 사람이 셸에서 **명시적으로**
`DATABASE_URL=… pytest` 를 주면 그 값이 이긴다. 통합 시험을 못 돌리게 막는 것이
아니라, **실수로 붙는 것**을 막는다. 붙이려면 그렇게 적어야 한다.

`TYBOT_SCHEMA_TEST_DSN`(격리 스키마 검증)은 건드리지 않는다. 그건 원래 별도
변수이고 `.env` 에 없다.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_repo_env_file(tmp_path_factory) -> None:
    """저장소 `.env` 가 시험 프로세스로 새지 않게 한다.

    `monkeypatch` 를 쓰지 않는 이유: 세션 범위 픽스처에서는 쓸 수 없고, 여기서
    필요한 것은 **세션 전체** 동안 고정되는 값이다.
    """
    empty = tmp_path_factory.mktemp("env") / "tests.env"
    empty.write_text("", encoding="utf-8")
    before = os.environ.get("TYBOT_ENV_FILE")
    os.environ["TYBOT_ENV_FILE"] = str(empty)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TYBOT_ENV_FILE", None)
        else:
            os.environ["TYBOT_ENV_FILE"] = before
