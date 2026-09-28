#!/usr/bin/env python3
"""Read-only parity check for legacy and unified Slack bot connection records."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.console.bot_migration_compare import compare_rows  # noqa: E402
from tybot.console.bot_repo import default_repo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default="", help="limit the comparison to one workspace key"
    )
    args = parser.parse_args()

    from tybot.envfile import load_env_file

    load_env_file()
    repo = default_repo()
    report = compare_rows(
        repo.legacy_services(args.workspace),
        repo.connections(args.workspace),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["mismatchCount"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
