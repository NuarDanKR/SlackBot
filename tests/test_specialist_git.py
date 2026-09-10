from __future__ import annotations

import json

import pytest

from tybot.console import specialist_git


def test_repository_url_only_accepts_public_github_https():
    assert specialist_git.normalize_repository_url(
        "https://github.com/wkimclementia/hermes"
    ) == ("https://github.com/wkimclementia/hermes.git", "wkimclementia", "hermes")

    for value in (
        "http://github.com/org/repo",
        "https://user:secret@github.com/org/repo",
        "https://github.com/org/repo?token=secret",
        "https://git.example.com/org/repo",
        "https://github.com/org/repo/extra",
    ):
        with pytest.raises(specialist_git.SpecialistGitError):
            specialist_git.normalize_repository_url(value)


def test_import_release_reads_only_declared_contract_blobs(monkeypatch):
    commit = "a" * 40
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
    blobs = {
        "tybot-specialist.toml": manifest,
        "contract/prompts/system.md": b"Answer only from supplied evidence.",
        "contract/tests/cases.json": json.dumps(cases).encode(),
    }
    commands: list[list[str]] = []

    def fake_git(args, **_kwargs):
        commands.append(args)
        return commit if "rev-parse" in args else ""

    monkeypatch.setattr(specialist_git, "_run_git", fake_git)
    monkeypatch.setattr(
        specialist_git,
        "_read_blob",
        lambda _git_dir, _commit, path, **_kwargs: blobs[path],
    )

    result = specialist_git.import_release(
        "https://github.com/wkimclementia/hermes", "v1.0.0"
    )

    assert result["key"] == "hermes"
    assert result["releaseRef"] == "v1.0.0"
    assert result["sourceCommit"] == commit
    assert set(result["artifactHashes"]) == {
        "contract/prompts/system.md", "contract/tests/cases.json",
    }
    clone = next(command for command in commands if "clone" in command)
    assert "--bare" in clone
    assert "--branch" in clone
    assert "v1.0.0" in clone


def test_contract_cases_require_exact_nonempty_fields():
    with pytest.raises(specialist_git.SpecialistGitError, match="필드가 계약과"):
        specialist_git._validate_cases([{"id": "incomplete"}])
    with pytest.raises(specialist_git.SpecialistGitError, match="근거가 하나 이상"):
        specialist_git._validate_cases([{
            "id": "empty", "question": "question", "evidence": [],
            "must_include": [], "must_not_include": [],
        }])
