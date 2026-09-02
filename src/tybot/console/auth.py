"""콘솔 로그인.

접속 경로와 무관하게 **누가 승인했고 누가 원문을 열었는지 남겨야 하므로** 사용자 식별은 한다.
공용 계정을 쓰면 승인 기록에 실제 사용자 이름을 남길 수 없다.

## 흐름
1. 회사 이메일·비밀번호로 `POST /api/login`
2. 서버가 **서명된 세션 값**을 HttpOnly 쿠키로 내려준다
3. 이후 요청은 그 쿠키로 사용자를 식별한다

세션은 서버에 저장하지 않는다(서명만 검증). 프로세스를 재시작하면
`CONSOLE_SECRET` 이 고정돼 있는 한 로그인이 유지된다.

## 계정
PostgreSQL `console_user`가 원본이다. 로그인 ID는 회사 이메일이며 비밀번호는
`scrypt$salt$digest` 형식으로만 저장한다. 계정이 없거나 DB를 읽지 못하면 콘솔 기동을
막는다. 최초 관리자 생성과 비상 비밀번호 재설정은 이 모듈의 `set-password` 명령을 사용하고,
기동 뒤의 일반 계정 관리는 관리자 전용 화면에서 한다.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
from argparse import ArgumentParser
from dataclasses import dataclass, field
from pathlib import Path

from ..envfile import load_env_file

logger = logging.getLogger("tybot.console.auth")

GUEST = "guest"
DEVELOPER = "developer"
ADMIN = "admin"
ROLES = (GUEST, DEVELOPER, ADMIN)

SESSION_COOKIE = "tybot_console"
# 세션 유효 시간. 업무 중 다시 로그인하지 않을 만큼 길고, 자리를 비운 사이에는 끊길 만큼 짧게.
SESSION_HOURS = 12

# scrypt 파라미터. 표준 라이브러리만 쓴다.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthError(RuntimeError):
    """인증 실패. 라우터가 401 로 바꾼다."""


class AuthConfigurationError(RuntimeError):
    """DB 계정 설정이 없어 콘솔을 안전하게 열 수 없다."""


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


# 없는 계정도 실제 계정과 같은 횟수로 scrypt를 계산해 계정 존재 여부를 숨긴다.
_DUMMY_PASSWORD_HASH = hash_password("존재하지 않는 계정")


# ---------------------------------------------------------------------------
# 계정
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConsoleUser:
    email: str
    name: str
    role: str
    workspaces: frozenset[str] = field(default_factory=frozenset)
    # 전체 워크스페이스를 다룰 수 있는가 (관리자 계정)
    all_workspaces: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @property
    def may_manage_bot(self) -> bool:
        return self.role in (DEVELOPER, ADMIN)

    def may_see(self, workspace: str) -> bool:
        return self.all_workspaces or workspace in self.workspaces

    def display(self) -> str:
        return self.name or self.email.split("@")[0]


@dataclass(frozen=True)
class Account:
    email: str
    password_hash: str
    user: ConsoleUser


def account(
    email: str,
    password_hash: str,
    name: str,
    role: str,
    workspaces: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (),
) -> Account:
    """DB 행 또는 테스트 픽스처를 인증 계정으로 바꾼다."""
    normalized_email = email.strip().lower()
    allowed = frozenset(str(ws).strip() for ws in workspaces if str(ws).strip())
    return Account(
        email=normalized_email,
        password_hash=password_hash,
        user=ConsoleUser(
            email=normalized_email,
            name=name.strip() or normalized_email.split("@")[0],
            role=role,
            workspaces=allowed,
            all_workspaces=role == ADMIN,
        ),
    )


def load_accounts() -> list[Account]:
    """활성 DB 계정과 워크스페이스 권한을 읽는다. 실패 시 열어 두지 않는다."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise AuthConfigurationError("DATABASE_URL이 없어 콘솔 계정 DB를 읽을 수 없습니다.")
    try:
        import psycopg

        with psycopg.connect(
            url,
            autocommit=True,
            connect_timeout=3,
            row_factory=psycopg.rows.dict_row,
        ) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.email, u.name, u.role, u.password_hash,
                       coalesce(array_agg(cuw.workspace) FILTER (
                           WHERE cuw.workspace IS NOT NULL
                       ), '{}') AS workspaces
                  FROM console_user u
                  LEFT JOIN console_user_workspace cuw ON cuw.email = u.email
                 WHERE u.active
                   AND u.password_hash IS NOT NULL
                   AND u.password_hash <> ''
                 GROUP BY u.email, u.name, u.role, u.password_hash
                 ORDER BY u.email
                """
            )
            rows = cur.fetchall()
    except Exception as e:
        raise AuthConfigurationError(f"콘솔 계정 DB 조회 실패: {e}") from e
    return [
        account(
            str(row["email"]),
            str(row["password_hash"]),
            str(row["name"]),
            str(row["role"]),
            row.get("workspaces") or (),
        )
        for row in rows
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
        accounts: list[Account] | None = None,
        secret: str | None = None,
        mode: str | None = None,
    ) -> None:
        self._db_backed = accounts is None
        self.accounts = list(accounts) if accounts is not None else load_accounts()
        if not self.accounts:
            raise AuthConfigurationError(
                "활성 콘솔 계정이 없습니다. set-password 명령으로 DB 계정을 먼저 만드세요."
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

    def reload(self) -> None:
        """DB 계정 변경을 현재 프로세스에 반영한다."""
        if self._db_backed:
            accounts = load_accounts()
            if not accounts:
                raise AuthConfigurationError("활성 콘솔 계정이 없습니다.")
            self.accounts = accounts

    # --- 계정 조회 --------------------------------------------------------
    def account_by_email(self, email: str) -> Account | None:
        for a in self.accounts:
            if a.email == email.strip().lower():
                return a
        return None

    def by_email(self, email: str) -> ConsoleUser | None:
        found = self.account_by_email(email)
        return found.user if found else None

    # --- 로그인 -----------------------------------------------------------
    def login(self, email: str, password: str) -> str:
        """성공하면 세션 값을 돌려준다. 실패 사유는 구분해 알려 주지 않는다.

        "그런 이메일 없음"과 "비밀번호 틀림"을 구분하면 계정 존재 여부가 노출된다.
        """
        found = self.account_by_email(email)
        if found is None:
            # 이메일이 없어도 같은 시간이 걸리게 해 존재 여부를 유추하지 못하게 한다.
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise AuthError("이메일 또는 비밀번호가 맞지 않습니다.")
        if not verify_password(password, found.password_hash):
            raise AuthError("이메일 또는 비밀번호가 맞지 않습니다.")
        return self.issue(found.email)

    def issue(self, email: str, *, now: float | None = None) -> str:
        """`이메일|만료시각`에 서명해 붙인다."""
        exp = int((now or time.time()) + SESSION_HOURS * 3600)
        payload = f"{email.strip().lower()}|{exp}".encode()
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
            email, exp_text = payload.decode().rsplit("|", 1)
            exp = int(exp_text)
        except (ValueError, UnicodeDecodeError):
            raise AuthError("세션 값이 올바르지 않습니다. 다시 로그인해 주세요.") from None

        if (now or time.time()) > exp:
            raise AuthError("로그인이 만료되었습니다. 다시 로그인해 주세요.")

        found = self.account_by_email(email)
        if found is None:
            # DB에서 비활성화하거나 지운 계정의 기존 세션도 무효다.
            raise AuthError("사용할 수 없는 계정입니다. 관리자에게 문의해 주세요.")
        return found.user

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


def _set_password(
    email: str,
    *,
    name: str,
    role: str | None,
    workspaces: list[str] | None,
) -> int:
    email = email.strip().lower()
    if "@" not in email:
        print("회사 이메일 형식이 아닙니다.", file=sys.stderr)
        return 2
    password = getpass.getpass("새 콘솔 비밀번호: ")
    confirm = getpass.getpass("비밀번호 확인: ")
    if password != confirm:
        print("두 비밀번호가 다릅니다.", file=sys.stderr)
        return 2
    if len(password) < 12:
        print("비밀번호는 12자 이상이어야 합니다.", file=sys.stderr)
        return 2
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL이 없습니다.", file=sys.stderr)
        return 2
    try:
        import psycopg

        with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('console_user_admin_guard'))")
            cur.execute(
                "SELECT name, role, active FROM console_user WHERE email = %s FOR UPDATE",
                (email,),
            )
            current = cur.fetchone()
            if current is None and role is None:
                print("새 계정은 --role guest|developer|admin 을 지정해야 합니다.", file=sys.stderr)
                return 2

            resolved_name = name.strip() or (
                str(current["name"]) if current else email.split("@")[0]
            )
            resolved_role = role or str(current["role"])

            if (
                current
                and current["role"] == ADMIN
                and current["active"]
                and resolved_role != ADMIN
            ):
                cur.execute(
                    "SELECT count(*) AS count FROM console_user WHERE role = 'admin' AND active"
                )
                if int(cur.fetchone()["count"]) <= 1:
                    print("마지막 활성 관리자는 강등할 수 없습니다.", file=sys.stderr)
                    return 2

            cur.execute(
                """
                INSERT INTO console_user (email, name, password_hash, role, active)
                VALUES (%s, %s, %s, %s, true)
                ON CONFLICT (email) DO UPDATE SET
                    name = excluded.name,
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    active = true
                """,
                (email, resolved_name, hash_password(password), resolved_role),
            )
            # 비밀번호만 재설정할 때는 기존 담당 범위를 보존한다. 역할이나 범위를
            # 명시한 경우에만 매핑을 다시 만든다.
            if resolved_role == ADMIN:
                cur.execute("DELETE FROM console_user_workspace WHERE email = %s", (email,))
            elif current is None or role is not None or workspaces is not None:
                cur.execute("DELETE FROM console_user_workspace WHERE email = %s", (email,))
                for workspace in sorted(set(workspaces or [])):
                    cur.execute(
                        "INSERT INTO console_user_workspace (email, workspace) VALUES (%s, %s)",
                        (email, workspace),
                    )
    except Exception as e:  # noqa: BLE001 - CLI는 오류를 보여 주고 실패 코드로 끝낸다.
        print(f"계정 저장 실패: {e}", file=sys.stderr)
        return 1
    print(f"콘솔 계정을 저장했습니다: {email} ({resolved_role})")
    return 0


def _init_schema() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL이 없습니다.", file=sys.stderr)
        return 2
    schema = Path(__file__).resolve().parents[3] / "deploy" / "sql" / "console_schema.sql"
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(schema.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - CLI는 오류를 보여 주고 실패 코드로 끝낸다.
        print(f"콘솔 스키마 적용 실패: {e}", file=sys.stderr)
        return 1
    print(f"콘솔 스키마를 적용했습니다: {schema}")
    return 0


def _cli() -> int:
    load_env_file()
    parser = ArgumentParser(description="TYBot 콘솔 DB 계정 관리")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-schema", help="console_schema.sql을 DATABASE_URL에 적용")
    set_password = commands.add_parser("set-password", help="계정 생성 또는 비밀번호 변경")
    set_password.add_argument("email", help="로그인에 사용할 회사 이메일")
    set_password.add_argument("--name", default="", help="화면에 표시할 이름")
    set_password.add_argument("--role", choices=ROLES)
    set_password.add_argument(
        "--workspace", action="append", help="게스트·개발자가 접근할 워크스페이스 키"
    )
    args = parser.parse_args()
    if args.command == "init-schema":
        return _init_schema()
    if args.command == "set-password":
        return _set_password(
            args.email,
            name=args.name,
            role=args.role,
            workspaces=args.workspace,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
