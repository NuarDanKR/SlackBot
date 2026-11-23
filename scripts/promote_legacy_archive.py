"""Plan or apply promotion of a quarantined legacy archive into the live root."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tybot.archive.legacy_promote import LegacyPromotionError, promote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-workspaces", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to the destination; without this flag the command is read-only",
    )
    args = parser.parse_args(argv)
    try:
        report = promote(
            args.source_workspaces, args.destination, apply=args.apply
        )
    except LegacyPromotionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report.as_json(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
