"""읽기 전용 조회용 PostgreSQL 연결.

봇은 지금까지 DB 없이 동작했다(아카이브가 진실, DB 는 재구성 가능한 인덱스). 그래서
연결은 **선택**이다 — `DATABASE_URL` 이 없거나 psycopg 가 없으면 `None` 을 돌려주고
호출자가 기능을 비활성 안내로 대체한다. 예외를 올려 봇을 죽이지 않는다.

`autocommit=True` 로 연다. 여기서 여는 연결은 조회 전용이고, 트랜잭션을 열어 두면
Slack 응답을 기다리는 동안 잠금이 남는다. 쓰기가 필요한 잡(`orgsync` 등)은 자기
연결을 따로 만든다.
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

logger = logging.getLogger("tybot.db")

# 조회가 오래 걸려도 Slack 3초 응답 한도를 넘기지 않게 짧게 둔다.
CONNECT_TIMEOUT_SEC = 3
STATEMENT_TIMEOUT_MS = 4000

_warned = False


def available() -> bool:
    return bool(os.getenv("DATABASE_URL"))


@contextlib.contextmanager
def connect() -> Iterator[object | None]:
    """조회용 연결. 쓸 수 없으면 `None` 을 넘긴다.

    호출 예:
        with connect() as conn:
            if conn is None:
                return UNAVAILABLE
    """
    global _warned
    url = os.getenv("DATABASE_URL")
    if not url:
        yield None
        return
    try:
        import psycopg
    except ImportError:
        if not _warned:
            logger.warning(
                "DATABASE_URL 이 있지만 psycopg 가 없습니다. "
                "pip install 'psycopg[binary]' — DB 기능은 비활성으로 안내합니다."
            )
            _warned = True
        yield None
        return

    try:
        with psycopg.connect(
            url,
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_SEC,
            row_factory=psycopg.rows.dict_row,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            yield conn
    except Exception as e:  # noqa: BLE001 - DB 장애로 봇이 죽지 않는다
        logger.warning("DB 연결 실패: %s", e)
        yield None
