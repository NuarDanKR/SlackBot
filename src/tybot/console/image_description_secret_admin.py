"""B-81 Vision provider API 자격의 콘솔 경계."""
from __future__ import annotations

from ..archive.image_description_secret import (
    ImageProviderSecretError,
    list_provider_keys,
    save_provider_key,
    set_provider_key_enabled,
)
from .image_description_admin import ImageDescriptionAdminError, _connect


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
