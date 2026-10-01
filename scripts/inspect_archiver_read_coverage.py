"""Print aggregate read coverage before changing TYBot's archive reader."""

from __future__ import annotations

import argparse
import json

from tybot.archive.read_inventory import inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--archiver-root", required=True)
    args = parser.parse_args()
    report = inventory(args.legacy_root, args.archiver_root)
    print(json.dumps(report.to_json(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
