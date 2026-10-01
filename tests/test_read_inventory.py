import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.inspect_archiver_read_coverage import main
from tybot.archive import shadow_paths, writer
from tybot.archive.read_inventory import inventory


def _write(root, workspace, channel_id, ts, *, new=False, text="sensitive conversation body"):
    destination = (
        shadow_paths.archive_dir(root, workspace, channel_id, "team") if new else None
    )
    writer.ingest(
        root, workspace=workspace, channel="#team", channel_id=channel_id,
        messages=[writer.IncomingMessage(
            ts=datetime.fromtimestamp(float(ts), UTC), speaker="Alice",
            text=text, source_ts=ts,
        )],
        acl=["#team"], channel_directory=destination,
    )


def test_inventory_counts_coordinates_per_workspace_without_content(tmp_path):
    old = tmp_path / "legacy"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write(old, "tyit", "C12345678", "1790800000.000001")
    _write(old, "mgmt", "C12345678", "1790800000.000001")
    _write(new, "tyit", "C12345678", "1790800000.000001", new=True)
    _write(new, "tyit", "C12345678", "1790800001.000001", new=True,
           text="another sensitive conversation body")
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*.md")}

    result = inventory(old, new)

    assert result.legacy.coordinates == 2
    assert result.archiver.coordinates == 2
    assert (result.shared_coordinates, result.legacy_only_coordinates,
            result.archiver_only_coordinates) == (1, 1, 1)
    assert "sensitive conversation body" not in str(result.to_json())
    assert before == {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*.md")}


def test_inventory_never_opens_private_dm_by_default(tmp_path, monkeypatch):
    old = tmp_path / "legacy"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    dm_dir = shadow_paths.dm_archive_dir(new, "tyit", "U12345678", "D12345678")
    writer.ingest(
        new, workspace="tyit", channel=writer.dm_channel("U12345678"),
        channel_id="D12345678", dm_user="U12345678", dm_directory=dm_dir,
        messages=[writer.IncomingMessage(
            ts=datetime.fromtimestamp(1790800000, UTC), speaker="Alice",
            text="private DM content", source_ts="1790800000.000001",
        )],
    )
    original = Path.read_text

    def refuse_dm_read(path, *args, **kwargs):
        if "dm" in path.parts:
            raise AssertionError("inventory opened a private DM")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", refuse_dm_read)
    result = inventory(old, new)
    assert result.archiver.documents == 0
    assert result.archiver.coordinates == 0


def test_inventory_rejects_missing_or_overlapping_roots(tmp_path):
    old = tmp_path / "legacy"
    old.mkdir()
    with pytest.raises(ValueError, match="separate existing"):
        inventory(old, old)
    with pytest.raises(ValueError, match="separate existing"):
        inventory(old, old / "missing")


def test_inventory_reports_unidentified_and_broken_without_body(tmp_path, monkeypatch, capsys):
    old = tmp_path / "legacy"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    writer.ingest(
        old, workspace="tyit", channel="#unknown", channel_id=None,
        messages=[writer.IncomingMessage(
            ts=datetime.fromtimestamp(1790800000, UTC), speaker="Alice",
            text="private meeting content", source_ts="1790800000.000001",
        )],
    )
    bad = new / "tyit" / "C12345678__team" / "archive" / "raw" / "bad.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("invalid document with secret body", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "inspect_archiver_read_coverage", "--legacy-root", str(old),
        "--archiver-root", str(new),
    ])
    assert main() == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["legacy"]["unidentified_channel_documents"] == 1
    assert report["legacy"]["uncoordinated_lines"] == 1
    assert report["archiver"]["broken_documents"] == 1
    assert "private meeting content" not in output
    assert "secret body" not in output
