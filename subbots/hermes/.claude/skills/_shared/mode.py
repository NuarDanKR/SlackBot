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

`HERMES_MODE` 가 비었으면 **PF 직접 실행**이다. 알려진 값은 `pf`·`pf-archiver`·`tybot`이고,
그 밖의 값은 기동 오류다. 오타를 PF 로 받아 주면 TYBot 연동으로 띄운 줄 알았던
스킬의 원문 쓰기가 열리기 때문이다.
"""
import os
import sys

TYBOT = "tybot"
PF = "pf"
PF_ARCHIVER = "pf-archiver"


class ModeConfigError(RuntimeError):
    """설정값이 모드가 아니다. **기동을 막는다** — 고쳐야 하는 것은 설정이다."""

    def __init__(self, raw: object) -> None:
        super().__init__(
            f"HERMES_MODE 값이 올바르지 않습니다: {raw!r}\n"
            f'쓸 수 있는 값은 "{PF}", "{PF_ARCHIVER}", "{TYBOT}" 이고, '
            f'비우면 "{PF}" 입니다.'
        )
        self.raw = raw
        self.code = "hermes_mode_invalid"


def mode() -> str:
    """지금 모드. **모르는 값이면 던진다.**

    봇 쪽(`src/mode.js`)과 **같은 판정이어야 한다.** 한쪽만 너그러우면 봇은 기동에
    실패하는데 스킬은 PF 로 돌아 쓰기가 열린다 — 그 상태는 오류로 보이지 않는다.

    처음에는 둘 다 「모르는 값은 PF」 였다. `HERMES_MODE=tybo` 로 띄우면 연동인 줄
    아는 프로세스가 쓰기가 열린 채로 돈다. 멈추면 그 자리에서 알지만, 열린 채로
    도는 것은 원문이 늘어난 뒤에야 안다.
    """
    raw = (os.environ.get("HERMES_MODE") or "").strip().lower()
    if raw == "":
        return PF
    if raw in (PF, PF_ARCHIVER, TYBOT):
        return raw
    raise ModeConfigError(os.environ.get("HERMES_MODE"))


def is_tybot_mode() -> bool:
    return mode() == TYBOT


def archive_writes_blocked() -> bool:
    """Archiving Bot이 정본을 소유해 Hermes 원문 쓰기가 금지됐는지 반환한다."""
    return mode() != PF


class ArchiveWriteBlocked(RuntimeError):
    """막힌 동작. 「실패」 가 아니라 「금지」 라 호출부가 구별할 수 있어야 한다."""

    def __init__(self, action: str) -> None:
        current = mode()
        label = "TYBot 연동 모드" if current == TYBOT else "PF Archiver 모드"
        super().__init__(
            f"[{label}] {action} 은(는) 막혀 있습니다.\n"
            "Archiving Bot이 원문 수집·소급·첨부 저장을 맡으므로 Hermes는 원문을 "
            "쓰지 않습니다.\n"
            "Hermes의 기존 원문 writer를 되살릴 때만 HERMES_MODE=pf로 실행하세요."
        )
        self.action = action
        self.code = "tybot_mode_write_blocked"


def assert_may_write_archive(action: str) -> None:
    """원문을 쓰려는 자리마다 **맨 앞에서** 부른다.

    돌려주는 값이 없다 — 통과하거나 던지거나 둘뿐이다. 불리언을 돌려주면 부르는
    쪽이 확인을 잊어도 조용히 지나간다.
    """
    if archive_writes_blocked():
        raise ArchiveWriteBlocked(action)


def exit_if_blocked(action: str) -> None:
    """CLI 스킬의 `main()` 맨 앞에서 부른다.

    스택을 보여 주는 대신 사람 말로 멈춘다. 스킬을 집어 든 쪽이 에이전트일 수도
    있어서, 왜 멈췄는지가 출력 첫 줄에 있어야 한다.
    """
    if archive_writes_blocked():
        print(ArchiveWriteBlocked(action), file=sys.stderr)
        raise SystemExit(2)
