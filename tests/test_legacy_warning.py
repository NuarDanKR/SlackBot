from tybot.archive.store import _legacy_changed


def test_warning_tracks_root_and_file_count(tmp_path):
    root = tmp_path / "channels"
    assert _legacy_changed(root, 5)
    assert not _legacy_changed(root, 5)
    assert _legacy_changed(root, 4)
    assert not _legacy_changed(root, 0)
    assert _legacy_changed(root, 5)
