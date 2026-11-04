"""Print a body-free, read-only preview of the canonical embedding inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tybot.archive.embedding_source import iter_index_sources
from tybot.archive.store import ArchiveStore
from tybot.embedding_index_inventory import inventory
from tybot.envfile import load_env_file
from tybot.paths import archive_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--archive-root", default="")
    args = parser.parse_args()

    load_env_file()
    try:
        root = Path(args.archive_root or archive_dir())
        report = inventory(
            iter_index_sources(ArchiveStore(root)),
            model_revision=args.model_revision,
            workspace=args.workspace,
        )
    except Exception as exc:  # noqa: BLE001 - source and DB details must not escape
        print(
            json.dumps(
                {"readOnly": True, "error": type(exc).__name__},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report.to_json(), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
