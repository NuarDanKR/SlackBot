import json

import pytest

from tybot.console import audit_store, specialist_store


def test_audit_metadata_drops_sensitive_fields():
    clean = audit_store._safe_metadata({
        "state": "enabled", "token": "xoxb-secret", "question": "업무 질문",
        "changed": ["REPLY_IN_THREAD"],
    })
    assert clean == {"state": "enabled", "changed": ["REPLY_IN_THREAD"]}
    assert "secret" not in json.dumps(clean)


def test_specialist_request_only_accepts_code_registered_adapter():
    with pytest.raises(specialist_store.SpecialistStoreError, match="코드에 등록"):
        specialist_store._validate_proposal({
            "key": "unknown", "name": "임의 봇", "domain": "임의",
            "adapter": "https://untrusted.example", "state": "draft", "workspaces": [],
        })


def test_specialist_request_normalizes_workspace_scope():
    value = specialist_store._validate_proposal({
        "key": "hermes", "name": "Hermes", "domain": "내부 문서",
        "adapter": "hermes", "state": "draft", "workspaces": ["TYIT", "tyit", "mgmt"],
    })
    assert value["workspaces"] == ["mgmt", "tyit"]


def test_specialist_request_rejects_unverified_contract_version():
    with pytest.raises(specialist_store.SpecialistStoreError, match="계약 검사"):
        specialist_store._validate_proposal({
            "key": "hermes", "name": "Hermes", "domain": "내부 문서",
            "adapter": "hermes", "state": "draft", "contractVersion": "v-next",
            "workspaces": ["tyit"],
        })
