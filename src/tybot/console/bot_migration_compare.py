"""Compare legacy and unified bot connection metadata without exposing secrets."""
from __future__ import annotations

LEGACY_BOT_KEYS = {
    "master": "master",
    "archiver": "archiver",
    "hermes_direct": "hermes",
}

COMPARE_FIELDS = (
    ("state", "state"),
    ("team_id", "team_id"),
    ("bot_user_id", "bot_user_id"),
    ("identity_ok", "identity_ok"),
    ("bot_mask", "bot_mask"),
    ("app_mask", "app_mask"),
    ("token_count", "token_count"),
)


def compare_rows(legacy_rows: list[dict], new_rows: list[dict]) -> dict:
    """Return a redacted parity report for legacy and unified connections.

    Token masks are compared in memory but never included in the report. The only
    per-row identifiers emitted are workspace and bot key.
    """
    legacy = {
        (str(row["workspace"]), LEGACY_BOT_KEYS.get(str(row["service"]),
                                                    str(row["service"]))): row
        for row in legacy_rows
    }
    unified = {
        (str(row["workspace"]), str(row["bot_key"])): row
        for row in new_rows
    }
    rows: list[dict] = []

    for key in sorted(legacy.keys() | unified.keys()):
        old = legacy.get(key)
        new = unified.get(key)
        if old is None:
            rows.append({
                "workspace": key[0], "botKey": key[1],
                "status": "new_only", "differences": [],
            })
            continue
        if new is None:
            rows.append({
                "workspace": key[0], "botKey": key[1],
                "status": "legacy_only", "differences": ["missing_new_connection"],
            })
            continue

        differences = [
            old_field for old_field, new_field in COMPARE_FIELDS
            if old.get(old_field) != new.get(new_field)
        ]
        rows.append({
            "workspace": key[0], "botKey": key[1],
            "status": "mismatch" if differences else "match",
            "differences": differences,
        })

    mismatch_count = sum(row["status"] in {"mismatch", "legacy_only"} for row in rows)
    return {
        "legacyCount": len(legacy),
        "unifiedCount": len(unified),
        "matchCount": sum(row["status"] == "match" for row in rows),
        "mismatchCount": mismatch_count,
        "newOnlyCount": sum(row["status"] == "new_only" for row in rows),
        "rows": rows,
    }
