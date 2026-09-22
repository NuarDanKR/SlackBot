"""PF 콘솔 설정. **`PF_` 로 시작하는 변수만 읽는다.**

## 왜 접두사를 고정하나
같은 호스트에 `/etc/tybot/tybot.env` 가 있다. 그 파일에는 TYBot 의 `DATABASE_URL` 과
Slack·Anthropic 키가 들어 있다. PF 프로세스가 실수로 그중 하나를 읽으면 **분리해 둔
DB role 이 의미를 잃는다** — 코드는 PF role 로 붙는 줄 알지만 실제로는 TYBot role 로
붙고, 그때는 TYBot 질문·원문 표까지 전부 열린다.

그래서 이름을 겹치지 않게 둔다. `DATABASE_URL` 이 환경에 있어도 이 모듈은 쳐다보지
않는다. 값이 없으면 **기동을 막는다** — 없는 채로 뜨면 「빈 화면」 으로 보이고,
사람은 아직 데이터가 없는 것으로 읽는다.
"""
from __future__ import annotations

import os
from pathlib import Path

# 세션 쿠키. TYBot 콘솔의 `tybot_console` 과 이름이 달라야 한다 — 같으면 한쪽 로그인이
# 다른 쪽 쿠키를 덮어써서, 사용자는 이유 없이 로그아웃된다.
SESSION_COOKIE = "pf_console"

# `Path=/pf` 로 둔다. 브라우저가 `/pf/` 요청에만 이 쿠키를 붙이므로, TYBot 콘솔
# (`/`) 로 가는 요청에는 PF 세션이 실려 가지 않는다. 반대로 TYBot 쿠키는 `Path=/`
# 라 `/pf/` 에도 실려 오지만, PF 쪽은 이름이 다르므로 읽지 않는다.
SESSION_COOKIE_PATH = "/pf"

SESSION_HOURS = 12


class PFConfigError(RuntimeError):
    """설정이 없어 PF 콘솔을 안전하게 열 수 없다."""


def database_url() -> str:
    """PF 전용 DB 접속 문자열. **TYBot 의 `DATABASE_URL` 을 대신 쓰지 않는다.**"""
    url = os.getenv("PF_DATABASE_URL", "").strip()
    if not url:
        raise PFConfigError(
            "PF_DATABASE_URL 이 없습니다. PF 콘솔은 TYBot 의 DATABASE_URL 을 쓰지 않습니다"
            " — 전용 DB role 접속 문자열을 /etc/tybot-pf/console.env 에 넣으세요."
        )
    return url


def session_secret() -> str:
    """세션 서명 키.

    없으면 **막지 않고 임시 키를 만든다.** 서명 키가 없다고 화면 전체가 안 뜨면
    상태를 못 보는데, 이 화면의 존재 이유가 상태를 보는 것이다. 대신 재시작할 때마다
    다시 로그인해야 하고, 그 사실을 로그로 남긴다.
    """
    return os.getenv("PF_CONSOLE_SECRET", "").strip()


def cookie_secure() -> bool:
    """`Secure` 쿠키를 붙일지.

    **TLS 를 붙인 뒤에 켠다.** 그 전에 켜면 브라우저가 쿠키를 보내지 않아 로그인이
    되지 않고, 화면에는 「비밀번호가 틀렸다」 처럼 보인다(B-35 와 같은 함정).
    """
    return os.getenv("PF_CONSOLE_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}


def dist_dir() -> Path | None:
    """`/pf/` 화면 빌드 산출물. 없으면 API 만 연다."""
    raw = os.getenv("PF_CONSOLE_DIST", "").strip()
    return Path(raw) if raw else None


def state_root() -> Path:
    """서비스 상태 파일들이 있는 상위 경로.

    DB 의 `managed_service.state_dir` 가 비어 있을 때만 쓰는 기본값이다. 요청이
    경로를 정하는 일은 없다 — 그건 임의 파일 읽기다.
    """
    return Path(os.getenv("PF_STATE_ROOT", "/var/lib/tybot-subbots"))


def stale_after_seconds() -> int:
    """상태 파일이 이만큼 낡으면 「멈춤」 으로 본다.

    Hermes 가 상태를 얼마나 자주 쓰는지는 프금팀 계약이다(§7.4). 계약이 확정되기
    전까지는 넉넉하게 두고, 화면이 **마지막 기록 시각을 같이 보여 준다** — 판정보다
    시각이 먼저다. 판정만 보이면 임계값이 틀렸을 때 사람이 알 방법이 없다.
    """
    try:
        return max(60, int(os.getenv("PF_STATE_STALE_SECONDS", "900")))
    except ValueError:
        return 900
