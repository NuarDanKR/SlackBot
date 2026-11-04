from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from tybot.embedding_index_inventory import EmbeddingInventory

SCRIPT = Path(__file__).parents[1] / "scripts" / "preview_embedding_index.py"


def _module():
    spec = importlib.util.spec_from_file_location("preview_embedding_index", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_is_read_only_and_emits_no_source_coordinates(monkeypatch, capsys):
    module = _module()
    calls: list[object] = []
    report = EmbeddingInventory(
        model_revision="embed-v1",
        chunker_revision="source-v1:chunk-v1:max=1200:ov=120",
        documents=0,
        source_chars=0,
        planned_documents=0,
        planned_chars=0,
        planned_chunks=0,
        minimum_batches=0,
        outcomes=(),
        workspaces=(),
    )
    monkeypatch.setattr(module, "load_env_file", lambda: calls.append("env"))
    monkeypatch.setattr(module, "ArchiveStore", lambda root: ("store", root))
    monkeypatch.setattr(
        module,
        "iter_index_sources",
        lambda store: calls.append(store) or iter(()),
    )
    monkeypatch.setattr(
        module,
        "inventory",
        lambda sources, **kwargs: calls.append((list(sources), kwargs)) or report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--model-revision",
            "embed-v1",
            "--workspace",
            "alpha",
            "--archive-root",
            "archive-fixture",
        ],
    )

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["readOnly"] is True
    assert payload["modelCalls"] == 0
    assert payload["databaseWrites"] == 0
    assert calls[-1] == (
        [],
        {"model_revision": "embed-v1", "workspace": "alpha"},
    )


def test_cli_has_no_apply_or_revision_bypass_switch():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--apply" not in source
    assert "require_revision_db=False" not in source
    assert "allow-missing" not in source


def test_cli_does_not_expose_source_or_database_exception_text(monkeypatch, capsys):
    module = _module()
    secret = "private archive path and database detail"
    monkeypatch.setattr(module, "load_env_file", lambda: None)
    monkeypatch.setattr(module, "ArchiveStore", lambda root: root)
    monkeypatch.setattr(
        module,
        "iter_index_sources",
        lambda _store: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--model-revision", "embed-v1"],
    )

    assert module.main() == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"readOnly": True, "error": "RuntimeError"}
    assert secret not in output
