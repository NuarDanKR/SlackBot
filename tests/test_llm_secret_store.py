"""LLM API 키 저장소.

핵심 성질 세 가지를 지킨다.
  1. 평문으로 저장하지 않는다
  2. 복호화해서 돌려주는 API 가 없다 — 봇이 쓸 때만 푼다
  3. DB 가 흔들려도 봇이 답을 멈추지 않는다(환경변수로 되돌아간다)
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from tybot.console import llm_secret_store as store
from tybot.console.workspace_store import WorkspaceStoreError


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))


def test_short_keys_are_refused():
    """잘려 붙여진 값이 저장되면 다음 질문에서야 알게 된다."""
    with pytest.raises(WorkspaceStoreError, match="짧"):
        store.save_secret("anthropic", "sk-ant-1", actor="dan@taeyoung.com")


def test_prefix_is_checked_per_provider():
    with pytest.raises(WorkspaceStoreError, match="sk-ant-"):
        store.save_secret("anthropic", "sk-" + "9" * 40, actor="dan@taeyoung.com")


def test_unknown_provider_is_refused():
    with pytest.raises(WorkspaceStoreError, match="지원하지 않는"):
        store.save_secret("gemini", "sk-" + "9" * 40, actor="dan@taeyoung.com")


def test_resolve_falls_back_to_env_without_a_database(monkeypatch):
    """DB 가 없는 설치에서도 봇은 그대로 떠야 한다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "1" * 30)

    assert store.resolve_key("anthropic") == "sk-ant-" + "1" * 30


def test_resolve_falls_back_when_the_database_fails(monkeypatch):
    """키 조회 실패가 답변 경로를 끊으면 안 된다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "2" * 30)
    monkeypatch.setattr(
        store, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("연결 실패"))
    )

    assert store.resolve_key("openai") == "sk-" + "2" * 30


def test_resolve_returns_none_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert store.resolve_key("anthropic") is None


def test_store_never_exposes_a_plaintext_read():
    """복호화해 돌려주는 조회 함수를 만들지 않는다. 만들면 콘솔이 그걸 쓴다."""
    public = {name for name in dir(store) if not name.startswith("_")}

    assert "resolve_key" in public, "봇이 쓸 경로 하나만 있다"
    assert not (public & {"read_secret", "get_secret", "plaintext"})


def test_provider_env_names_are_defined_once():
    """두 군데 적으면 한쪽만 고쳐져 조용히 어긋난다."""
    assert store.PROVIDERS["anthropic"] == "ANTHROPIC_API_KEY"
    assert store.PROVIDERS["openai"] == "OPENAI_API_KEY"
