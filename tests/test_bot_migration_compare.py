"""Legacy/new connection parity must be useful without revealing token masks."""
from __future__ import annotations

from tybot.console.bot_migration_compare import compare_rows


def _old(service="hermes_direct", **over):
    return {
        "workspace": "pf", "service": service, "state": "enabled",
        "team_id": "T1", "bot_user_id": "U1", "identity_ok": True,
        "bot_mask": "xoxb-old-mask", "app_mask": "xapp-old-mask",
        "token_count": 2, **over,
    }


def _new(bot_key="hermes", **over):
    return {
        "workspace": "pf", "bot_key": bot_key, "state": "enabled",
        "team_id": "T1", "bot_user_id": "U1", "identity_ok": True,
        "bot_mask": "xoxb-old-mask", "app_mask": "xapp-old-mask",
        "token_count": 2, **over,
    }


def test_hermes_direct_maps_to_one_unified_hermes_connection():
    report = compare_rows([_old()], [_new()])

    assert report["matchCount"] == 1
    assert report["mismatchCount"] == 0
    assert report["rows"][0]["botKey"] == "hermes"
    assert report["rows"][0]["status"] == "match"


def test_mismatch_lists_fields_but_never_token_masks():
    report = compare_rows([_old()], [_new(app_mask="different-mask")])
    serialized = str(report)

    assert report["mismatchCount"] == 1
    assert report["rows"][0]["differences"] == ["app_mask"]
    assert "xoxb-old-mask" not in serialized
    assert "xapp-old-mask" not in serialized
    assert "different-mask" not in serialized


def test_missing_unified_row_is_a_blocking_mismatch():
    report = compare_rows([_old()], [])

    assert report["mismatchCount"] == 1
    assert report["rows"][0]["status"] == "legacy_only"
    assert report["rows"][0]["differences"] == ["missing_new_connection"]


def test_new_only_rows_are_reported_without_failing_legacy_parity():
    report = compare_rows([], [_new()])

    assert report["newOnlyCount"] == 1
    assert report["mismatchCount"] == 0
    assert report["rows"][0]["status"] == "new_only"


def test_master_and_archiver_keep_their_own_keys():
    report = compare_rows(
        [_old("master"), _old("archiver")],
        [_new("master"), _new("archiver")],
    )

    assert report["matchCount"] == 2
    assert {row["botKey"] for row in report["rows"]} == {"master", "archiver"}
