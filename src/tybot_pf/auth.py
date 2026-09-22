"""PF 콘솔 로그인과 서비스 권한.

## 왜 TYBot 인증을 안 쓰나
`tybot.console.auth` 를 임포트하면 그 모듈이 `DATABASE_URL`(TYBot 것)로 `console_user`
전체를 읽는다. PF 프로세스가 그 연결을 갖는 순간 **분리해 둔 DB role 이 의미를 잃는다.**

그래서 여기서는 `PF_DATABASE_URL` 로만 붙고, 계정 확인에 필요한 네 열만 읽는다
(`email`·`name`·`password_hash`·`active`). PF DB role 은 그 네 열 말고는 `console_user`
에 권한이 없다(`deploy/sql/pf_console_schema.sql`).

해시·서명 방식은 TYBot 쪽과 같다. **의도한 중복**이다 — 코드를 나눠 갖지 않는 것이
이 화면의 요구사항이고, 시험이 그 규칙을 고정한다.

## 「로그인됨」 과 「볼 수 있음」 은 다르다
회사 계정이 있으면 로그인은 된다. 그러나 `console_user_service` 에 행이 없으면
**아무 서비스도 보이지 않는다.** TYBot 관리자도 마찬가지다(§8.2). 권한을 역할이
아니라 행으로 두는 이유는, 「TYBot 에서 admin 이니 PF 도 되겠지」 가 자동으로
성립하지 않게 하기 위해서다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field

from .config import SESSION_HOURS, PFConfigError, database_url, session_secret

logger = logging.getLogger("tybot_pf.auth")

VIEWER = "viewer"
OPERATOR = "operator"
DEVELOPER = "developer"
APPROVER = "approver"
ROLES = (VIEWER, OPERATOR, DEVELOPER, APPROVER)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class PFAuthError(RuntimeError):
    """인증 실패. 라우터가 401 로 바꾼다."""


# ---------------------------------------------------------------------------
# 비밀번호
# ---------------------------------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


# 없는 계정도 같은 시간을 쓰게 해 계정 존재 여부를 숨긴다.
_DUMMY_HASH = hash_password("존재하지 않는 계정")


# ---------------------------------------------------------------------------
# 사용자
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PFUser:
    email: str
    name: str
    # {서비스 키: {역할, ...}}. 비어 있으면 아무것도 못 본다.
    grants: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def services(self) -> list[str]:
        return sorted(self.grants)

    def roles_on(self, service: str) -> frozenset[str]:
        return self.grants.get(service.strip().lower(), frozenset())

    def may_view(self, service: str) -> bool:
        """조회 권한. **역할 넷 모두 조회는 된다** — 넷은 그 위에서 갈린다."""
        return bool(self.roles_on(service))

    def display(self) -> str:
        return self.name or self.email.split("@")[0]

    def to_json(self) -> dict:
        return {
            "email": self.email,
            "name": self.display(),
            "services": [
                {"key": key, "roles": sorted(roles)}
                for key, roles in sorted(self.grants.items())
            ],
        }


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _connect():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 배포 환경에는 항상 있다
        raise PFConfigError("psycopg 가 없습니다. PF 콘솔 의존성을 설치하세요.") from exc
    return psycopg.connect(
        database_url(),
        autocommit=True,
        connect_timeout=3,
        row_factory=psycopg.rows.dict_row,
    )


def load_account(email: str) -> tuple[str, str, str] | None:
    """(이메일, 이름, 비밀번호 해시). 없거나 비활성이면 None.

    **한 사람만 읽는다.** 전체 목록을 메모리에 들고 있지 않는 이유: PF 프로세스가
    회사 계정 명부를 통째로 갖고 있을 이유가 없고, 갖고 있으면 그것 자체가 유출
    대상이 된다.
    """
    wanted = email.strip().lower()
    if not wanted:
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT email, name, password_hash
              FROM console_user
             WHERE lower(email) = %s
               AND active
               AND password_hash IS NOT NULL
               AND password_hash <> ''
            """,
            (wanted,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return str(row["email"]).lower(), str(row["name"] or ""), str(row["password_hash"])


def load_grants(email: str) -> dict[str, frozenset[str]]:
    """이 사람이 볼 수 있는 서비스와 역할.

    `managed_service` 와 조인해 **allowlist 에 있는 서비스만** 돌려준다. 권한 행이
    남아 있는데 서비스가 지워진 경우를 조용히 통과시키지 않기 위해서다.
    사용 중지(`disabled`) 서비스는 목록에서 뺀다 — 볼 것이 없다.
    """
    wanted = email.strip().lower()
    if not wanted:
        return {}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.key AS service, us.role
              FROM console_user_service us
              JOIN managed_service s ON s.key = us.service
             WHERE lower(us.email) = %s
               AND s.state <> 'disabled'
            """,
            (wanted,),
        )
        rows = cur.fetchall()
    grants: dict[str, set[str]] = {}
    for row in rows:
        role = str(row["role"]).strip().lower()
        if role in ROLES:
            grants.setdefault(str(row["service"]).lower(), set()).add(role)
    return {key: frozenset(value) for key, value in grants.items()}


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PFAuthenticator:
    """로그인과 세션 검증.

    세션 값에는 **이메일과 만료 시각만** 담는다. 권한은 담지 않는다 — 담으면 권한을
    회수해도 그 사람의 쿠키가 만료될 때까지 계속 보인다. 요청마다 DB 에서 다시 읽는
    쪽이 느리지만, 이 화면의 요청 수는 사람이 누르는 만큼뿐이다.
    """

    def __init__(self, *, secret: str | None = None) -> None:
        self.secret = (secret if secret is not None else session_secret()).strip()
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
            logger.warning(
                "PF_CONSOLE_SECRET 이 없어 임시 키로 세션을 서명합니다."
                " 재시작하면 모두 다시 로그인해야 합니다."
            )

    def login(self, email: str, password: str) -> str:
        """성공하면 세션 값. 실패 사유는 구분해 알려 주지 않는다.

        **권한이 없어도 로그인 자체는 된다.** 「계정은 있는데 PF 권한이 없다」 와
        「그런 계정이 없다」 를 로그인 단계에서 가르면, 로그인 화면이 회사 계정
        존재 여부를 알려 주는 도구가 된다. 권한 없음은 로그인 뒤에 화면이 말한다.
        """
        found = load_account(email)
        if found is None:
            verify_password(password, _DUMMY_HASH)
            raise PFAuthError("이메일 또는 비밀번호가 맞지 않습니다.")
        stored_email, _name, password_hash = found
        if not verify_password(password, password_hash):
            raise PFAuthError("이메일 또는 비밀번호가 맞지 않습니다.")
        return self.issue(stored_email)

    def issue(self, email: str, *, now: float | None = None) -> str:
        exp = int((now or time.time()) + SESSION_HOURS * 3600)
        payload = f"{email.strip().lower()}|{exp}".encode()
        sig = hmac.new(self.secret.encode(), payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify(self, session: str, *, now: float | None = None) -> str:
        """세션 값에서 이메일을 꺼낸다. 권한은 여기서 보지 않는다."""
        try:
            payload_b64, sig_b64 = session.split(".", 1)
            payload = _unb64(payload_b64)
            sig = _unb64(sig_b64)
        except (ValueError, TypeError):
            raise PFAuthError("세션 값이 올바르지 않습니다. 다시 로그인해 주세요.") from None
        expected = hmac.new(self.secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise PFAuthError("세션 서명이 맞지 않습니다. 다시 로그인해 주세요.")
        try:
            email, exp_text = payload.decode().rsplit("|", 1)
            exp = int(exp_text)
        except (ValueError, UnicodeDecodeError):
            raise PFAuthError("세션 값이 올바르지 않습니다. 다시 로그인해 주세요.") from None
        if (now or time.time()) > exp:
            raise PFAuthError("세션이 만료됐습니다. 다시 로그인해 주세요.")
        return email

    def user(self, session: str, *, now: float | None = None) -> PFUser:
        """세션에서 사용자를 만든다. **권한은 매 요청 DB 에서 다시 읽는다.**"""
        email = self.verify(session, now=now)
        found = load_account(email)
        if found is None:
            # 계정이 비활성화됐거나 지워졌다. 쿠키가 남아 있어도 통과시키지 않는다.
            raise PFAuthError("계정을 사용할 수 없습니다. 관리자에게 문의하세요.")
        stored_email, name, _hash = found
        return PFUser(email=stored_email, name=name, grants=load_grants(stored_email))
