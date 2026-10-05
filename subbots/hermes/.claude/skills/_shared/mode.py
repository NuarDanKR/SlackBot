#!/usr/bin/env python3
"""실행 모드 — 스킬이 원문을 써도 되는가.

봇 쪽의 같은 판정은 `src/mode.js` 다. **두 곳이 같은 환경변수를 본다**(`HERMES_MODE`).
값이 갈리면 봇은 막혔는데 스킬은 쓰는 상태가 되고, 그건 오류가 아니라 **권한 판정을
거치지 않은 원문 사본**으로 나타난다.

## 스킬이 가장 위험한 축이다

CLI 는 사람이 직접 친다. 스케줄러는 시각이 정해져 있다. 그런데 스킬은 **사람이
명령을 치지 않아도 에이전트가 집어서** 실행할 수 있다. `fetch_slack_files.py` 는
Slack 에서 파일을 내려받고, `insert_entry.py`·`insert_messages.py` 는 아카이브에
직접 쓴다. 연동 모드에서 그 경로가 열려 있으면 「아무도 안 불렀는데 원문이 늘어난」
상태가 생기고, 그때 무엇이 썼는지 추적할 자리가 없다.

## 기본값

`HERMES_MODE` 가 비었거나 모르는 값이면 **PF 직접 실행**이다. 운영을 끄는 결정은
명시적이어야 한다 — 환경변수를 안 넘겼다고 돌던 수집이 조용히 멈추면 안 된다.
"""
import os
import sys

TYBOT = "tybot"
PF = "pf"


def mode() -> str:
    """지금 모드. 모르는 값은 PF 로 본다 — 오타가 운영을 멈추게 하지 않는다."""
    raw = (os.environ.get("HERMES_MODE") or "").strip().lower()
    return TYBOT if raw == TYBOT else PF


def is_tybot_mode() -> bool:
    return mode() == TYBOT


class ArchiveWriteBlocked(RuntimeError):
    """막힌 동작. 「실패」 가 아니라 「금지」 라 호출부가 구별할 수 있어야 한다."""

    def __init__(self, action: str) -> None:
        super().__init__(
            f"[TYBot 연동 모드] {action} 은(는) 막혀 있습니다.\n"
            "연동 모드에서 Hermes 는 원문을 쓰지 않습니다 — 근거는 TYBot 의 권한 "
            "검사된 도구로만 읽습니다.\n"
            "PF 직접 실행이라면 HERMES_MODE 를 비우고 다시 실행하세요."
        )
        self.action = action
        self.code = "tybot_mode_write_blocked"


def assert_may_write_archive(action: str) -> None:
    """원문을 쓰려는 자리마다 **맨 앞에서** 부른다.

    돌려주는 값이 없다 — 통과하거나 던지거나 둘뿐이다. 불리언을 돌려주면 부르는
    쪽이 확인을 잊어도 조용히 지나간다.
    """
    if is_tybot_mode():
        raise ArchiveWriteBlocked(action)


def exit_if_blocked(action: str) -> None:
    """CLI 스킬의 `main()` 맨 앞에서 부른다.

    스택을 보여 주는 대신 사람 말로 멈춘다. 스킬을 집어 든 쪽이 에이전트일 수도
    있어서, 왜 멈췄는지가 출력 첫 줄에 있어야 한다.
    """
    if is_tybot_mode():
        print(ArchiveWriteBlocked(action), file=sys.stderr)
        raise SystemExit(2)
