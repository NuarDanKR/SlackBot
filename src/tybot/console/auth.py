"""콘솔 로그인.

VPN 안에서만 열리지만 **누가 승인했고 누가 원문을 열었는지 남겨야 하므로** 사용자 식별은 한다.
"VPN 안이니 아무나"로 두면 승인 기록에 이름을 쓸 수 없다.

## 흐름
1. 아이디·비밀번호로 `POST /api/login`
2. 서버가 **서명된 세션 값**을 HttpOnly 쿠키로 내려준다
3. 이후 요청은 그 쿠키로 사용자를 식별한다

세션은 서버에 저장하지 않는다(서명만 검증). DB 없이도 돌아가고, 프로세스를 재시작하면
`CONSOLE_SECRET` 이 고정돼 있는 한 로그인이 유지된다.

## 계정
`CONSOLE_ACCOUNTS` 환경변수로 관리한다. 한 줄에 하나씩, 쉼표로 구분한다.

    아이디:비밀번호해시:이메일:역할[:워크스페이스|워크스페이스]

비밀번호 해시는 아래로 만든다(표준 라이브러리만 쓴다. 새 의존성 없음).

    python -m tybot.console.auth 새비밀번호

**값을 비워 두면 임시 계정 `admin` / `1111` 이 열린다.** 파일럿에서 화면을 보기 위한
편의값이고, 그 상태에서는 기동 로그와 화면 양쪽에 경고가 뜬다.
계정 관리는 PostgreSQL 로 옮길 예정이다(BACKLOG B-16).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger("tybot.console.auth")

OWNER = "owner"
MEMBER = "member"

SESSION_COOKIE = "tybot_console"
# 세션 유효 시간. 업무 중 다시 로그인하지 않을 만큼 길고, 자리를 비운 사이에는 끊길 만큼 짧게.
SESSION_HOURS = 12

# scrypt 파라미터. 표준 라이브러리만 쓴다.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

# 설정이 비었을 때 열어 두는 임시 계정. 운영에서는 반드시 교체한다.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "1111"


class AuthError(RuntimeError):
    """인증 실패. 라우터가 401 로 바꾼다."""


# ---------------------------------------------------------------------------
# 비밀번호
# ---------------------------------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt$솔트$해시` 형식으로 만든다."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """저장된 해시와 비교한다. 실패 사유를 구분해 알려 주지 않는다."""
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        logger.warning("비밀번호 해시 형식이 잘못됐습니다. 이 계정으로는 로그인할 수 없습니다.")
        return False
    if scheme != "scrypt":
        logger.warning("알 수 없는 비밀번호 해시 방식: %s", scheme)
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
    except ValueError:
        return False
    # 앞자리부터 하나씩 맞춰 보는 공격을 막으려고 상수 시간으로 비교한다
    return hmac.compare_digest(candidate.hex(), digest_hex)


# ---------------------------------------------------------------------------
# 계정
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConsoleUser:
    email: str
    name: str
    role: str
    workspaces: frozenset[str] = field(default_factory=frozenset)
    # 전체 워크스페이스를 다룰 수 있는가 (owner 이거나 `*` 로 지정된 경우)
    all_workspaces: bool = False

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    def may_see(self, workspace: str) -> bool:
        return self.all_workspaces or workspace in self.workspaces

    def display(self) -> str:
        return self.name or self.email.split("@")[0]


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str
    user: ConsoleUser


def _account(username: str, password_hash: str, email: str, role: str, ws_spec: str) -> Account:
    all_ws = role == OWNER or ws_spec == "*"
    workspaces = frozenset(w.strip() for w in ws_spec.split("|") if w.strip() and w.strip() != "*")
    return Account(
        username=username,
        password_hash=password_hash,
        user=ConsoleUser(
            email=email,
            name=email.split("@")[0],
            role=role,
            workspaces=workspaces,
            all_workspaces=all_ws,
        ),
    )


def parse_accounts(spec: str | None) -> list[Account]:
    """`CONSOLE_ACCOUNTS` 파싱. 형식이 틀린 항목은 버리고 경고한다.

    한 줄이 잘못됐다고 콘솔 전체가 안 뜨면 곤란하지만, **아무도 못 들어가는 상태**도 위험하므로
    버려진 항목은 반드시 로그에 남긴다.
    """
    out: list[Account] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 4:
            logger.warning(
                "CONSOLE_ACCOUNTS 항목 형식 오류(무시): %r — 아이디:해시:이메일:역할[:워크스페이스]",
                parts[0] if parts else chunk,
            )
            continue
        username, pw_hash, email, role = (p.strip() for p in parts[:4])
        ws_spec = parts[4].strip() if len(parts) > 4 else ""
        if not username or "@" not in email or role not in (OWNER, MEMBER):
            logger.warning("CONSOLE_ACCOUNTS 항목 값 오류(무시): 아이디 %r", username)
            continue
        out.append(_account(username, pw_hash, email, role, ws_spec))
    return out


def default_accounts() -> list[Account]:
    """설정이 비었을 때 열어 두는 임시 계정."""
    return [
        _account(
            DEFAULT_USERNAME,
            hash_password(DEFAULT_PASSWORD),
            "admin@taeyoung.com",
            OWNER,
            "*",
        )
    ]


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """로그인과 세션 검증."""

    def __init__(
        self,
        *,
        accounts_spec: str | None = None,
        secret: str | None = None,
        mode: str | None = None,
    ) -> None:
        raw = accounts_spec if accounts_spec is not None else os.getenv("CONSOLE_ACCOUNTS")
        parsed = parse_accounts(raw)
        self.using_default = not parsed
        self.accounts = parsed or default_accounts()

        if self.using_default:
            logger.warning(
                "임시 계정(%s / %s)으로 콘솔이 열려 있습니다. "
                "이 콘솔은 봇 토큰과 배포 권한을 쥔 화면입니다. "
                "CONSOLE_ACCOUNTS 를 설정해 교체하세요 — 해시 생성: "
                "python -m tybot.console.auth <새비밀번호>",
                DEFAULT_USERNAME,
                DEFAULT_PASSWORD,
            )

        # 세션 서명 키. 값을 주지 않으면 기동할 때마다 새로 만든다(재시작 시 로그인 풀림).
        self.secret = (secret if secret is not None else os.getenv("CONSOLE_SECRET", "")).strip()
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
            logger.warning(
                "CONSOLE_SECRET 이 없어 임시 키로 세션을 서명합니다. "
                "서버를 재시작하면 모두 다시 로그인해야 합니다."
            )

        # 인증 프록시를 앞단에 두는 구성. 프록시를 반드시 거칠 때만 쓴다.
        self.mode = (mode or os.getenv("CONSOLE_AUTH", "password")).strip().lower()

    # --- 계정 조회 --------------------------------------------------------
    def by_username(self, username: str) -> Account | None:
        for a in self.accounts:
            if a.username.lower() == username.strip().lower():
                return a
        return None

    def by_email(self, email: str) -> ConsoleUser | None:
        for a in self.accounts:
            if a.user.email.lower() == email.strip().lower():
                return a.user
        return None

    # --- 로그인 -----------------------------------------------------------
    def login(self, username: str, password: str) -> str:
        """성공하면 세션 값을 돌려준다. 실패 사유는 구분해 알려 주지 않는다.

        "그런 아이디 없음"과 "비밀번호 틀림"을 구분해 주면 어떤 아이디가 있는지 알려 주는 셈이다.
        """
        account = self.by_username(username)
        if account is None:
            # 아이디가 없어도 같은 시간이 걸리게 해 존재 여부를 유추하지 못하게 한다
            verify_password(password, hash_password("존재하지 않는 계정"))
            raise AuthError("아이디 또는 비밀번호가 맞지 않습니다.")
        if not verify_password(password, account.password_hash):
            raise AuthError("아이디 또는 비밀번호가 맞지 않습니다.")
        return self.issue(account.username)

    def issue(self, username: str, *, now: float | None = None) -> str:
        """`아이디|만료시각` 에 서명해 붙인다."""
        exp = int((now or time.time()) + SESSION_HOURS * 3600)
        payload = f"{username}|{exp}".encode()
        sig = hmac.new(self.secret.encode(), payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify(self, session: str, *, now: float | None = None) -> ConsoleUser:
        try:
            payload_b64, sig_b64 = session.split(".", 1)
            payload = _unb64(payload_b64)
            sig = _unb64(sig_b64)
        except (ValueError, TypeError):
            raise AuthError("세션 값이 올바르지 않습니다. 다시 로그인해 주세요.") from None

        expected = hmac.new(self.secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise AuthError("세션 서명이 맞지 않습니다. 다시 로그인해 주세요.")

        try:
            username, exp_text = payload.decode().rsplit("|", 1)
            exp = int(exp_text)
        except (ValueError, UnicodeDecodeError):
            raise AuthError("세션 값이 올바르지 않습니다. 다시 로그인해 주세요.") from None

        if (now or time.time()) > exp:
            raise AuthError("로그인이 만료되었습니다. 다시 로그인해 주세요.")

        account = self.by_username(username)
        if account is None:
            # 계정 설정에서 지워졌으면 남아 있던 세션도 무효다
            raise AuthError("사용할 수 없는 계정입니다. 관리자에게 문의해 주세요.")
        return account.user

    # --- 요청에서 사용자 찾기 ---------------------------------------------
    def identify(self, *, session: str | None, forwarded_email: str | None) -> ConsoleUser:
        if self.mode == "proxy":
            if not forwarded_email:
                raise AuthError("인증 프록시가 사용자 정보를 넘기지 않았습니다.")
            user = self.by_email(forwarded_email)
            if user is None:
                raise AuthError(f"{forwarded_email} 는 콘솔 사용자로 등록되어 있지 않습니다.")
            return user

        if not session:
            raise AuthError("로그인이 필요합니다.")
        return self.verify(session)


def _cli() -> int:
    """비밀번호 해시 생성기: `python -m tybot.console.auth <비밀번호>`"""
    if len(sys.argv) != 2:
        print("사용법: python -m tybot.console.auth <비밀번호>", file=sys.stderr)
        print("출력된 해시를 CONSOLE_ACCOUNTS 의 두 번째 칸에 넣습니다.", file=sys.stderr)
        return 1
    print(hash_password(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
