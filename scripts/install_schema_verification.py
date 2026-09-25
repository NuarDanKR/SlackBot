#!/usr/bin/env python3
"""Install an isolated-schema verification artifact on the deployed host."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from tybot.console.release_gate import GateClosed, install_verified_artifact

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--installed-by",
        default=os.getenv("SUDO_USER") or os.getenv("USER") or "unknown",
    )
    args = parser.parse_args()

    try:
        target = install_verified_artifact(
            args.artifact.resolve(), installed_by=args.installed_by
        )
    except GateClosed as exc:
        print(f"설치 거부: {exc}", file=sys.stderr)
        return 2
    print(f"검증 artifact 설치 완료: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
