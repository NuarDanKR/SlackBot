"""Vision worker 전용 provider API 자격 저장소.

암호문은 Vision DB에, Fernet 키는 DB 밖의 서버 자격 파일에 둔다. 이 경로는
일반 TYBot ``llm_secret``이나 환경변수로 내려가지 않는다. 이미지 원문을 외부로
보내는 worker가 어느 자격을 썼는지 scope를 흐리면 권한·비용 주인이 섞이기 때문이다.
"""
from __future__ import annotations

import re

from tybot.console.workspace_store import WorkspaceStoreError, _fernet, _mask

_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_MIN_KEY_LENGTH = 16


class ImageProviderSecretError(RuntimeError):
    """로그와 API에 본문 없이 전달할 수 있는 고정 코드 오류."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
    try:
        ciphertext = _fernet().encrypt(value.encode("utf-8"))
    except WorkspaceStoreError as exc:
        raise ImageProviderSecretError("provider_secret_key_unavailable") from exc
    with connection.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_provider_secret
               (provider,ciphertext,mask,enabled,updated_by)
               VALUES (%s,%s,%s,true,%s)
               ON CONFLICT (provider) DO UPDATE SET
                 ciphertext=EXCLUDED.ciphertext,mask=EXCLUDED.mask,enabled=true,
                 updated_by=EXCLUDED.updated_by,updated_at=now()""",
            (name, ciphertext, _mask(value), actor.strip()),
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
    try:
        value = _fernet().decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except (WorkspaceStoreError, ValueError, UnicodeError) as exc:
        raise ImageProviderSecretError("provider_secret_unreadable") from exc
    return _key(value)


__all__ = [
    "ImageProviderSecretError",
    "list_provider_keys",
    "resolve_provider_key",
    "save_provider_key",
    "set_provider_key_enabled",
]
