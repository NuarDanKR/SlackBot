"""쓰기 경로 점검 — 봇과 `check_env.py` 가 **같은 코드**로 확인한다.

이 모듈이 따로 있는 이유: 점검 스크립트가 통과했는데 봇 기동이 실패하는 어긋남을
없애기 위해서다(`envfile.py` 와 같은 취지). slack 의존성 없이 불러올 수 있어야 하므로
`slack.pilot` 이 아니라 여기에 둔다.
"""
from __future__ import annotations

import logging
import os
import pathlib

log = logging.getLogger("tybot.paths")

LABELS = {"아카이브": "ARCHIVE_DIR", "감사기록": "QA_LOG_DIR"}


def writable(path: str) -> str | None:
    """쓰기 가능 여부 점검. 문제가 있으면 사유 문자열을 반환한다.

    아카이브 쓰기 실패는 '조용한 고장'의 대표 사례다 - 봇은 정상 응답하는데
    원문이 하나도 쌓이지 않는다. 기동 시점에 잡아 로그와 `상태`에 드러낸다.
    """
    d = pathlib.Path(path)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except Exception as e:  # noqa: BLE001 - 사유를 문자열로 올려 호출자가 판단한다
        return f"{e.__class__.__name__}: {e}"


def check_paths(archive_dir: str, qa_dir: str) -> dict[str, str]:
    """아카이브·감사기록 쓰기 가능 여부. 조용한 고장을 기동 시점에 드러낸다."""
    problems: dict[str, str] = {}
    for label, path in (("아카이브", archive_dir), ("감사기록", qa_dir)):
        why = writable(path)
        if why:
            problems[label] = f"{path} ({why})"
            log.error(
                "%s 디렉터리에 쓸 수 없습니다: %s - %s. "
                "tybot.env 의 ARCHIVE_DIR/QA_LOG_DIR 를 /var/lib/tybot 아래로 지정하세요.",
                label, path, why,
            )
    if not problems:
        log.info("경로 점검 통과 - archive=%s qa_log=%s", archive_dir, qa_dir)
    return problems


def archive_dir() -> str:
    return os.getenv("ARCHIVE_DIR", "./archive")


def qa_log_dir() -> str:
    return os.getenv("QA_LOG_DIR", "./qa-log")
