"""B-81 Vision provider API 자격의 콘솔 경계.

## 콘솔과 worker 는 다른 DB 역할로 이 표에 닿는다 (2026-11-12 보안 리뷰 반영)

예전에는 콘솔도 Vision 전용 DSN(`tybot_vision`)으로 키를 쓰고 읽었다. 그러면 worker 역할이
자격 표에 INSERT·UPDATE 까지 갖는다 — worker 가 털리면 키를 바꿔치기할 수 있다.

이제 둘을 가른다.

| 누가 | 역할 | 이 표에 할 수 있는 것 |
|---|---|---|
| Vision worker | `tybot_vision` | `provider·ciphertext·enabled` 열 SELECT **만** |
| 관리 콘솔 | 콘솔 DB 연결(`tyslackai`) | 마스크·상태 열 SELECT, INSERT·UPDATE. **암호문 열은 못 읽는다** |

그래서 이 모듈은 Vision DSN 이 아니라 콘솔의 `DATABASE_URL` 로 연다.
"""
from __future__ import annotations

import os

from ..archive.image_description_secret import (
    ImageProviderSecretError,
    list_provider_keys,
    save_provider_key,
    set_provider_key_enabled,
)
from .image_description_admin import ImageDescriptionAdminError


def _connect():
    """콘솔 DB 연결. Vision 전용 DSN 으로 내려가지 않는다 — 그 역할은 쓰기 권한이 없다."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise ImageDescriptionAdminError("vision_database_unavailable")
    try:
        import psycopg

        return psycopg.connect(url, autocommit=True, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise ImageDescriptionAdminError("vision_database_unavailable") from exc


def list_secrets(*, connect=None) -> tuple[dict, ...]:
    connector = connect or _connect
    try:
        with connector() as connection:
            return list_provider_keys(connection)
    except ImageDescriptionAdminError:
        raise
    except ImageProviderSecretError as exc:
        raise ImageDescriptionAdminError(exc.code) from None
    except Exception as exc:
        raise ImageDescriptionAdminError("vision_database_unavailable") from exc


def save_secret(
    provider: str,
    *,
    key: str | None,
    enabled: bool | None,
    actor: str,
    connect=None,
) -> tuple[dict, ...]:
    if key is None and enabled is None:
        raise ImageDescriptionAdminError("provider_secret_change_required")
    connector = connect or _connect
    try:
        with connector() as connection:
            if key is not None:
                save_provider_key(connection, provider, key, actor=actor)
            if enabled is not None:
                set_provider_key_enabled(connection, provider, enabled, actor=actor)
            return list_provider_keys(connection)
    except ImageDescriptionAdminError:
        raise
    except ImageProviderSecretError as exc:
        raise ImageDescriptionAdminError(exc.code) from None
    except Exception as exc:
        raise ImageDescriptionAdminError("vision_database_unavailable") from exc


__all__ = ["list_secrets", "save_secret"]
