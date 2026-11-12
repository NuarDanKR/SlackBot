"""Vision worker 전용 provider API 자격 저장소.

암호문은 Vision DB에, Fernet 키는 DB 밖의 서버 자격 파일에 둔다. 이 경로는
일반 TYBot ``llm_secret``이나 환경변수로 내려가지 않는다. 이미지 원문을 외부로
보내는 worker가 어느 자격을 썼는지 scope를 흐리면 권한·비용 주인이 섞이기 때문이다.

## 전용 Fernet 키 (2026-11-12 보안 리뷰 반영)

예전에는 워크스페이스 Slack 토큰과 **같은 키**(`WORKSPACE_SECRET_KEY_FILE`)로 암호화했다.
그러면 Vision worker 가 Slack 토큰 암호문까지 풀 수 있는 키를 들고 돈다. 이제
`VISION_PROVIDER_SECRET_KEY_FILE`(기본 `/etc/tybot/vision-provider-secret.key`, 0400)만
쓴다. 이 키가 없으면 워크스페이스 키로 내려가지 **않고** 실패한다.

## 마스크

Slack 토큰용 마스크는 앞 9자·끝 4자를 보였다. provider 키는 접두사(`AIza…`)가 고정이라
앞 글자가 정보가 아니고, 길이가 짧으면 보이는 비율이 커진다. 그래서 **끝 4자와 SHA-256
지문 8자**만 보인다 — 교체 여부를 사람이 대조하기에는 충분하다.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
#: provider 키 최소 길이. Google API 키는 39자다. 짧은 값은 마스크가 보이는 비율이 커진다.
_MIN_KEY_LENGTH = 20

KEY_FILE_ENV = "VISION_PROVIDER_SECRET_KEY_FILE"
DEFAULT_KEY_FILE = Path("/etc/tybot/vision-provider-secret.key")


class ImageProviderSecretError(RuntimeError):
    """로그와 API에 본문 없이 전달할 수 있는 고정 코드 오류."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _key_path() -> Path:
    explicit = os.getenv(KEY_FILE_ENV, "").strip()
    return Path(explicit) if explicit else DEFAULT_KEY_FILE


def _fernet():
    """Vision provider 자격 **전용** Fernet. 워크스페이스 키로 내려가지 않는다."""
    path = _key_path()
    try:
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ImageProviderSecretError("provider_secret_key_permissions")
        raw = path.read_text(encoding="ascii").strip()
    except ImageProviderSecretError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ImageProviderSecretError("provider_secret_key_unavailable") from exc
    if not raw:
        raise ImageProviderSecretError("provider_secret_key_unavailable")
    try:
        from cryptography.fernet import Fernet

        return Fernet(raw.encode("ascii"))
    except (ImportError, ValueError) as exc:
        raise ImageProviderSecretError("provider_secret_key_unavailable") from exc


def provider_mask(value: str) -> str:
    """끝 4자와 SHA-256 지문 8자만. 앞 글자는 보이지 않는다."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"…{value[-4:]} (sha256:{digest})"


def _provider(value: str) -> str:
    provider = (value or "").strip().lower()
    if not _PROVIDER.fullmatch(provider):
        raise ImageProviderSecretError("provider_invalid")
    return provider


def _key(value: str) -> str:
    key = (value or "").strip()
    if len(key) < _MIN_KEY_LENGTH or any(char.isspace() for char in key):
        raise ImageProviderSecretError("provider_key_invalid")
    return key


def save_provider_key(connection, provider: str, key: str, *, actor: str) -> None:
    """암호화한 키를 교체한다. 평문이나 복호화 API 응답은 저장하지 않는다."""
    name = _provider(provider)
    value = _key(key)
    if not (actor or "").strip():
        raise ImageProviderSecretError("actor_required")
    ciphertext = _fernet().encrypt(value.encode("utf-8"))
    with connection.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_provider_secret
               (provider,ciphertext,mask,enabled,updated_by)
               VALUES (%s,%s,%s,true,%s)
               ON CONFLICT (provider) DO UPDATE SET
                 ciphertext=EXCLUDED.ciphertext,mask=EXCLUDED.mask,enabled=true,
                 updated_by=EXCLUDED.updated_by,updated_at=now()""",
            (name, ciphertext, provider_mask(value), actor.strip()),
        )


def set_provider_key_enabled(
    connection, provider: str, enabled: bool, *, actor: str
) -> None:
    name = _provider(provider)
    if type(enabled) is not bool:
        raise ImageProviderSecretError("enabled_invalid")
    if not (actor or "").strip():
        raise ImageProviderSecretError("actor_required")
    with connection.cursor() as cur:
        cur.execute(
            """UPDATE archiver_image_provider_secret
               SET enabled=%s,updated_by=%s,updated_at=now() WHERE provider=%s""",
            (enabled, actor.strip(), name),
        )
        if cur.rowcount != 1:
            raise ImageProviderSecretError("provider_secret_missing")


def list_provider_keys(connection) -> tuple[dict, ...]:
    """콘솔용 mask와 상태만 반환한다. ciphertext도 평문도 반환하지 않는다."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT provider,mask,enabled,updated_by,updated_at
               FROM archiver_image_provider_secret ORDER BY provider"""
        )
        return tuple(dict(row) for row in cur.fetchall())


def resolve_provider_key(connection, provider: str) -> str:
    """worker 내부에서만 정확한 provider 키를 복호화한다. fallback은 없다."""
    name = _provider(provider)
    with connection.cursor() as cur:
        cur.execute(
            """SELECT ciphertext FROM archiver_image_provider_secret
               WHERE provider=%s AND enabled""",
            (name,),
        )
        row = cur.fetchone()
    if not row:
        raise ImageProviderSecretError("provider_secret_unavailable")
    cipher = _fernet()
    try:
        value = cipher.decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except Exception as exc:
        # 예전 워크스페이스 키로 암호화된 행도 여기로 온다. 콘솔에서 키를 다시 넣어야 한다.
        raise ImageProviderSecretError("provider_secret_unreadable") from exc
    return _key(value)


__all__ = [
    "ImageProviderSecretError",
    "list_provider_keys",
    "provider_mask",
    "resolve_provider_key",
    "save_provider_key",
    "set_provider_key_enabled",
]
