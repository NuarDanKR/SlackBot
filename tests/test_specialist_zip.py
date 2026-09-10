from __future__ import annotations

import io
import json
import zipfile

import pytest

from tybot.console import specialist_zip


def _bundle(*, extra: dict[str, bytes] | None = None, root: str = "hermes-v1.0.0/") -> bytes:
    manifest = b"""schema = \"tybot-specialist/v1\"
key = \"hermes\"
name = \"Hermes\"
domain = \"internal documents\"
release_type = \"prompt-contract\"
contract_version = \"v1\"
version = \"1.0.0\"
artifacts = [\"contract/prompts/system.md\", \"contract/tests/cases.json\"]
"""
    cases = [{
        "id": "grounded", "question": "status?", "evidence": ["reviewing"],
        "must_include": ["reviewing"], "must_not_include": ["출처:"],
    }]
    files = {
        "tybot-specialist.toml": manifest,
        "contract/prompts/system.md": b"Answer only from supplied evidence.",
        "contract/tests/cases.json": json.dumps(cases).encode(),
        **(extra or {}),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(f"{root}{path}", content)
    return output.getvalue()


def test_contract_zip_accepts_one_optional_root_folder():
    content = _bundle()

    result = specialist_zip.import_bundle(content, "Hermes%20v1.0.0.zip")

    assert result["key"] == "hermes"
    assert result["sourceType"] == "zip"
    assert result["sourceName"] == "Hermes v1.0.0.zip"
    assert result["bundleSha256"]
    assert set(result["artifactHashes"]) == {
        "contract/prompts/system.md", "contract/tests/cases.json",
    }


def test_contract_zip_rejects_source_code_and_path_traversal():
    with pytest.raises(specialist_zip.SpecialistZipError, match="무관한 파일"):
        specialist_zip.import_bundle(_bundle(extra={"src/index.js": b"run()"}), "hermes.zip")

    content = _bundle(extra={"../outside.txt": b"bad"}, root="")
    with pytest.raises(specialist_zip.SpecialistZipError, match="묶음 밖"):
        specialist_zip.import_bundle(content, "hermes.zip")


def test_contract_zip_rejects_undeclared_contract_file():
    with pytest.raises(specialist_zip.SpecialistZipError, match="선언하지 않은"):
        specialist_zip.import_bundle(
            _bundle(extra={"contract/private-notes.txt": b"do not import"}),
            "hermes.zip",
        )


def test_upload_receipt_is_bound_to_actor_and_contract(monkeypatch):
    monkeypatch.setenv("CONSOLE_SECRET", "test-console-secret")
    imported = specialist_zip.import_bundle(_bundle(), "hermes.zip")
    receipt = specialist_zip.issue_receipt(imported, "developer@taeyoung.com")

    assert specialist_zip.verify_receipt(imported, "developer@taeyoung.com", receipt)
    assert not specialist_zip.verify_receipt(imported, "other@taeyoung.com", receipt)
    changed = {**imported, "rules": "changed"}
    assert not specialist_zip.verify_receipt(changed, "developer@taeyoung.com", receipt)
