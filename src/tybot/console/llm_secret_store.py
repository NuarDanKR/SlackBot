"""LLM API 키 저장소.

`.env` 는 평문이다. 서버에 들어갈 수 있는 사람은 누구나 읽고, 백업·복사본에
그대로 따라다니며, 누가 언제 바꿨는지 남지 않는다.

여기서는 암호화해서 넣는다. 암호화 키는 **DB 밖의 파일**에 둔다
(`/etc/tybot/workspace-secret.key`, 0400). DB 백업만으로는 풀 수 없어야 이 저장이
평문보다 나아진다 — 그래서 키를 같은 DB 에 넣지 않는다.

Slack 토큰(`workspace_store`)과 같은 규칙을 따른다:

- 평문으로 저장하지 않는다
- 복호화해서 돌려주는 API 를 만들지 않는다. 콘솔은 가린 값(mask)만 읽는다
- 삭제 대신 사용 중지. 지우면 언제 무엇을 쓰고 있었는지가 사라진다

`.env` 는 **되돌아갈 자리로 남긴다.** DB 를 못 읽는 것 때문에 봇이 아예 답을
못 하면 안 된다 — 워크스페이스 토큰이 이미 그 구조다.
"""
from __future__ import annotations

import logging
import os

from .workspace_store import WorkspaceStoreError, _connect, _fernet, _mask

logger = logging.getLogger("tybot.console.llm_secret")

# 어떤 프로바이더의 키를 다루는지. 환경변수 이름도 여기서 정한다 —
# 두 군데 적으면 한쪽만 고쳐져 조용히 어긋난다.
PROVIDERS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# 키 모양 검사. 오타로 붙여 넣은 값이 저장되면 다음 질문에서야 알게 된다.
PREFIXES: dict[str, str] = {
    "anthropic": "sk-ant-",
    "openai": "sk-",
}
MIN_LENGTH = 20


def _validate(provider: str, key: str) -> str:
    value = (key or "").strip()
    if not value:
        raise WorkspaceStoreError("키가 비어 있습니다.")
    if len(value) < MIN_LENGTH:
        raise WorkspaceStoreError("키가 너무 짧습니다. 잘려 붙여진 값이 아닌지 확인하세요.")
    prefix = PREFIXES.get(provider, "")
    if prefix and not value.startswith(prefix):
        raise WorkspaceStoreError(f"{provider} 키는 {prefix} 로 시작해야 합니다.")
    return value


def list_secrets() -> list[dict]:
    """가린 값과 교체 기록만. **복호화하지 않는다.**"""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, mask, enabled, updated_at, updated_by
                  FROM llm_secret
                 ORDER BY provider
                """
            )
            rows = {str(r["provider"]): dict(r) for r in cur.fetchall()}
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"LLM 키 조회 실패: {exc}") from exc

    out = []
    for provider, env_name in PROVIDERS.items():
        row = rows.get(provider)
        out.append({
            "provider": provider,
            "envName": env_name,
            "mask": (row or {}).get("mask") or "",
            "enabled": bool((row or {}).get("enabled", False)),
            "updatedAt": (row or {}).get("updated_at"),
            "updatedBy": (row or {}).get("updated_by") or "",
            # DB 에 없으면 아직 환경변수를 쓰고 있다. 그 사실을 화면이 알아야
            # "등록 안 됨" 을 고장으로 읽지 않는다.
            "inEnv": bool(os.getenv(env_name, "").strip()),
        })
    return out


def save_secret(provider: str, key: str, *, actor: str) -> None:
    if provider not in PROVIDERS:
        raise WorkspaceStoreError(f"지원하지 않는 프로바이더입니다: {provider}")
    value = _validate(provider, key)
    cipher = _fernet()
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_secret (provider, ciphertext, mask, enabled, updated_by)
                VALUES (%(provider)s, %(ciphertext)s, %(mask)s, true, %(actor)s)
                ON CONFLICT (provider) DO UPDATE
                   SET ciphertext = excluded.ciphertext,
                       mask       = excluded.mask,
                       enabled    = true,
                       updated_at = now(),
                       updated_by = excluded.updated_by
                """,
                {
                    "provider": provider,
                    "ciphertext": cipher.encrypt(value.encode("utf-8")),
                    "mask": _mask(value),
                    "actor": actor,
                },
            )
            conn.commit()
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"LLM 키 저장 실패: {exc}") from exc


def set_enabled(provider: str, enabled: bool, *, actor: str) -> None:
    """사용 중지/재개. 삭제는 제공하지 않는다."""
    if provider not in PROVIDERS:
        raise WorkspaceStoreError(f"지원하지 않는 프로바이더입니다: {provider}")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE llm_secret
                   SET enabled = %(enabled)s, updated_at = now(), updated_by = %(actor)s
                 WHERE provider = %(provider)s
                """,
                {"provider": provider, "enabled": enabled, "actor": actor},
            )
            if cur.rowcount == 0:
                raise WorkspaceStoreError("등록되지 않은 키입니다.")
            conn.commit()
    except WorkspaceStoreError:
        raise
    except Exception as exc:
        raise WorkspaceStoreError(f"LLM 키 상태 변경 실패: {exc}") from exc


def resolve_key(provider: str) -> str | None:
    """봇이 실제로 쓸 키. **이 함수만 복호화한다.**

    DB 를 먼저 보고, 없으면 환경변수로 되돌아간다. 되돌아갈 자리를 없애면
    DB 하나가 흔들릴 때 봇이 아무 답도 못 한다.

    실패를 예외로 올리지 않는다 — 키 조회가 답변 경로를 끊으면 안 된다.
    """
    env_name = PROVIDERS.get(provider, "")
    env_key = os.getenv(env_name, "").strip() if env_name else ""
    if not os.getenv("DATABASE_URL", "").strip():
        return env_key or None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ciphertext FROM llm_secret WHERE provider = %s AND enabled",
                (provider,),
            )
            row = cur.fetchone()
        if row:
            return _fernet().decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 키 조회 실패가 답변을 끊으면 안 된다
        logger.error("LLM 키를 DB 에서 읽지 못해 환경변수를 씁니다: %s", exc)
    return env_key or None
