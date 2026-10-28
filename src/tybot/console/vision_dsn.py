"""Vision 전용 접속 자격(DSN)을 **그 자리에서만** 읽는다.

## 왜 별도 모듈인가

콘솔은 이미 `DATABASE_URL` 을 들고 있다. 그 값으로 B-81 표를 읽으면 화면은
정상으로 보이고, 그 순간 **Master 권한으로 Vision 표를 만지는 길**이 생긴다.
`tybot_vision` 을 NOLOGIN 그룹으로 따로 둔 이유가 통째로 사라진다.

그래서 폴백을 **코드에서 없앴다.** 이 모듈은 Vision 전용 값만 찾고, 못 찾으면
빈 목록이나 정상이 아니라 고정 코드로 닫는다.

## 어디서 찾나

1. `TYBOT_VISION_DATABASE_URL` — 값 직접
2. `TYBOT_VISION_DATABASE_URL_FILE` — 파일 경로 명시
3. `CREDENTIALS_DIRECTORY`/`tybot-vision-database-url` — systemd `LoadCredential`

2번과 3번은 **POSIX 권한을 먼저 본다.** group/other 비트가 하나라도 서 있으면
읽지 않는다. 읽은 뒤에 막는 것은 늦다 — 이미 같은 호스트의 다른 서비스가 읽을
수 있었다는 뜻이다.

## 값도 경로도 밖으로 내보내지 않는다

오류는 ASCII 고정 코드만 들고 있다. 경로를 메시지에 적으면 그 경로가 API 응답과
로그에 그대로 실린다. 원인 예외도 `from None` 으로 끊는다 — `OSError` 의 문자열에
경로가 들어 있어서, 트레이스백이 찍히는 순간 같은 것이 샌다.

이 모듈은 **로그를 남기지 않는다.** 남길 만한 것이 값과 경로뿐이기 때문이다.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

#: systemd `LoadCredential=` 에 넣을 이름. 운영 문서와 같은 철자여야 한다.
CREDENTIAL_NAME = "tybot-vision-database-url"

#: 설정이 아예 없다. 「없음」 과 「잘못됨」 을 같은 코드로 보내면 운영자가
#: 고칠 자리를 못 찾는다.
UNCONFIGURED = "vision_database_unconfigured"
#: 파일은 있는데 group/other 가 읽을 수 있다.
BAD_MODE = "vision_database_credential_mode"
#: 경로가 가리키는 것을 읽지 못했거나 내용이 비었다.
UNREADABLE = "vision_database_credential_unreadable"
#: Master/Archiver 와 **같은 DSN** 이다. 전용 계정을 만든 의미가 없다.
SHARED = "vision_database_shared_credential"
# A different login can make the dedicated grants meaningless even when the
# DSN text differs from the main DATABASE_URL.
WRONG_ROLE = "vision_database_wrong_role"
RUNTIME_ROLE = "tybot_vision_runtime"


class VisionDsnError(RuntimeError):
    """고정 코드만 들고 있는 오류. 값·경로·원인 본문을 담지 않는다."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_file(path: Path, *, explicit: bool) -> str:
    """코드를 모아 두었다가 **except 블록 밖에서** 던진다.

    `raise … from None` 은 `__cause__` 만 지운다. `__context__` 에는 원래
    `OSError` 가 그대로 남고, 그 객체의 `filename` 이 경로다. 트레이스백
    출력은 `__suppress_context__` 를 존중하지만 예외 체인을 따라가는 로거는
    그렇지 않다 — 거기서 경로가 샌다(2026-10-28 실측).
    """
    code, value, info = "", "", None
    # 있는지부터 본다. 플랫폼 분기를 앞에 두면 Windows 에서는 `stat` 을 건너뛰어
    # 「없음」 과 「못 읽음」 이 섞인다(2026-10-28 실측: 미설정이 unreadable 로 나왔다).
    try:
        info = path.stat()
    except OSError:
        # 명시하지 않은 기본 경로가 없는 것은 **미설정**이다. systemd 는
        # `CREDENTIALS_DIRECTORY` 를 늘 주므로, 그 안에 우리 파일이 없다는 것은
        # 아직 안 넣었다는 뜻이지 설정이 틀렸다는 뜻이 아니다.
        code = UNREADABLE if explicit else UNCONFIGURED
    # Windows 는 이 비트가 뜻을 갖지 않는다 — 늘 `0o666` 류라 여기서 재면
    # **개발 PC 에서만 실패하는 검사**가 된다.
    if not code and os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        code = BAD_MODE
    if not code:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            code = UNREADABLE
    if not code and not value:
        code = UNREADABLE
    if code:
        raise VisionDsnError(code)
    return value


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _dsn_login(url: str) -> tuple[str, str]:
    """DSN 이 **실제로 쓰게 될** scheme 과 접속 user. 못 읽으면 빈 값이다.

    문자열 비교로는 부족하다. `DATABASE_URL` 과 글자가 달라도 관리자 계정으로
    붙는 DSN 이면 전용 역할을 만든 의미가 없다.

    ## 질의 문자열이 userinfo 를 이긴다

    libpq 는 URI 를 키워드로 펼칠 때 userinfo 를 먼저 넣고 **질의 문자열로
    덮어쓴다.** 그래서

        postgresql://tybot_vision_runtime@host/db?user=postgres

    는 실제로 `postgres` 로 접속한다. userinfo 만 보면 이 DSN 이 통과한다
    (2026-10-28 실측: 통과했다). 질의 쪽을 **나중에** 읽어 같은 순서로 덮는다.

    ## URI 형식만 받는다

    `host=… user=…` 키워드 형식도 libpq 는 받지만 여기서는 거절한다. 형식마다
    파서를 따로 두면 둘 중 하나만 고쳐지는 날이 오고, 그때 조용히 통과하는
    쪽이 생긴다. 운영 문서도 URI 하나만 적는다.
    """
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme
        username = unquote(parsed.username or "")
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "user":
                username = value
    except ValueError:
        return "", ""
    return scheme, username


def resolve_vision_database_url() -> str:
    """Vision 전용 DSN. 없으면 예외다 — **일반 `DATABASE_URL` 로 내려가지 않는다.**"""
    url = _env("TYBOT_VISION_DATABASE_URL")
    if not url:
        explicit = _env("TYBOT_VISION_DATABASE_URL_FILE")
        directory = _env("CREDENTIALS_DIRECTORY")
        if explicit:
            url = _read_file(Path(explicit), explicit=True)
        elif directory:
            url = _read_file(Path(directory) / CREDENTIAL_NAME, explicit=False)
    if not url:
        raise VisionDsnError(UNCONFIGURED)
    # 복사해 붙인 경우를 여기서 잡는다. 접속은 성공하므로 화면은 정상으로 보이고,
    # 권한 분리는 이미 없어진 뒤다.
    if url == _env("DATABASE_URL"):
        raise VisionDsnError(SHARED)
    scheme, username = _dsn_login(url)
    if scheme not in {"postgres", "postgresql"} or username != RUNTIME_ROLE:
        raise VisionDsnError(WRONG_ROLE)
    return url


__all__ = [
    "BAD_MODE",
    "CREDENTIAL_NAME",
    "RUNTIME_ROLE",
    "SHARED",
    "UNCONFIGURED",
    "UNREADABLE",
    "WRONG_ROLE",
    "VisionDsnError",
    "resolve_vision_database_url",
]
